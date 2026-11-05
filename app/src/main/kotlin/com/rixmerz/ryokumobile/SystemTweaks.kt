package com.rixmerz.ryokumobile

import android.Manifest
import android.app.AppOpsManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Process
import android.provider.Settings
import android.telecom.TelecomManager
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import rikka.shizuku.Shizuku
import rikka.shizuku.ShizukuRemoteProcess

/**
 * Shell-level powers through Shizuku. Shizuku dies on every reboot, so it is used for two things:
 * once, to grant this app permissions that then persist (secure settings, usage stats, the
 * notification listener); and for the few actions that genuinely need a shell (force stop,
 * freeze, screen off), which are only offered while Shizuku is running.
 */
class SystemTweaks(private val context: Context) {
    enum class State { NotInstalled, NotRunning, NoPermission, Ready }

    private val pm = context.packageManager
    private val self = context.packageName

    var state by mutableStateOf(State.NotInstalled); private set
    var granted by mutableStateOf(false); private set
    var frozen by mutableStateOf<List<String>>(emptyList()); private set
    var lastError by mutableStateOf<String?>(null); private set

    private val onBinder = Shizuku.OnBinderReceivedListener { refresh() }
    private val onDead = Shizuku.OnBinderDeadListener { refresh() }
    private val onResult = Shizuku.OnRequestPermissionResultListener { _, _ -> refresh() }

    fun attach() {
        Shizuku.addBinderReceivedListenerSticky(onBinder)
        Shizuku.addBinderDeadListener(onDead)
        Shizuku.addRequestPermissionResultListener(onResult)
        refresh()
    }

    fun detach() {
        Shizuku.removeBinderReceivedListener(onBinder)
        Shizuku.removeBinderDeadListener(onDead)
        Shizuku.removeRequestPermissionResultListener(onResult)
    }

    fun refresh() {
        state = when {
            !installed(SHIZUKU_PACKAGE) -> State.NotInstalled
            !Shizuku.pingBinder() -> State.NotRunning
            Shizuku.isPreV11() || Shizuku.checkSelfPermission() != PackageManager.PERMISSION_GRANTED -> State.NoPermission
            else -> State.Ready
        }
        granted = hasSecureSettings() && hasUsageStats() && hasNotificationAccess()
        if (state == State.Ready) frozen = output("pm list packages -d --user 0")
            ?.lines()?.mapNotNull { it.removePrefix("package:").trim().ifEmpty { null } }?.sorted() ?: frozen
    }

    fun requestPermission() = Shizuku.requestPermission(REQUEST_CODE)

    private fun installed(pkg: String) = try { pm.getPackageInfo(pkg, 0); true } catch (_: PackageManager.NameNotFoundException) { false }

    // ---- One-time grants: they survive reboots and reinstalls over the top, not an uninstall. ----

    fun grantAll(): Boolean {
        val ok = run("pm grant $self ${Manifest.permission.WRITE_SECURE_SETTINGS}") &&
            run("appops set $self GET_USAGE_STATS allow") &&
            run("cmd notification allow_listener $self/${MediaListener::class.java.name}")
        refresh()
        return ok
    }

    private fun hasSecureSettings() =
        context.checkSelfPermission(Manifest.permission.WRITE_SECURE_SETTINGS) == PackageManager.PERMISSION_GRANTED

    private fun hasUsageStats(): Boolean {
        val ops = context.getSystemService(AppOpsManager::class.java)
        return ops.unsafeCheckOpNoThrow(AppOpsManager.OPSTR_GET_USAGE_STATS, Process.myUid(), self) == AppOpsManager.MODE_ALLOWED
    }

    private fun hasNotificationAccess() =
        androidx.core.app.NotificationManagerCompat.getEnabledListenerPackages(context).contains(self)

    // ---- Animation speed: a global setting, written directly once the grant is held. ----

    val animationScale: Float
        get() = Settings.Global.getFloat(context.contentResolver, Settings.Global.ANIMATOR_DURATION_SCALE, 1f)

    fun setAnimationScale(scale: Float) {
        if (!hasSecureSettings()) { lastError = "Grant permissions first"; return }
        listOf(Settings.Global.WINDOW_ANIMATION_SCALE, Settings.Global.TRANSITION_ANIMATION_SCALE, Settings.Global.ANIMATOR_DURATION_SCALE)
            .forEach { Settings.Global.putFloat(context.contentResolver, it, scale) }
        lastError = null
    }

    // ---- Actions that need Shizuku running. ----

    fun sleep() = run("input keyevent KEYCODE_SLEEP")

    fun forceStop(pkg: String) = run("am force-stop $pkg")

    /** False for anything whose loss would strand the user or break the phone. */
    fun canFreeze(pkg: String): Boolean {
        val telecom = context.getSystemService(TelecomManager::class.java)
        // Every installed home app stays, so the stock launcher is always there as a way out.
        val homes = pm.queryIntentActivities(android.content.Intent(android.content.Intent.ACTION_MAIN)
            .addCategory(android.content.Intent.CATEGORY_HOME), 0).map { it.activityInfo.packageName }
        val protectedPkgs = setOf(self, SHIZUKU_PACKAGE, "com.android.systemui", "com.android.settings",
            telecom?.defaultDialerPackage, android.provider.Telephony.Sms.getDefaultSmsPackage(context)) + homes
        return pkg !in protectedPkgs
    }

    fun freeze(pkg: String): Boolean {
        if (!canFreeze(pkg)) { lastError = "$pkg is protected"; return false }
        return run("pm disable-user --user 0 $pkg").also { refresh() }
    }

    fun unfreeze(pkg: String) = run("pm enable --user 0 $pkg").also { refresh() }

    fun labelOf(pkg: String): String = try {
        pm.getApplicationLabel(pm.getApplicationInfo(pkg, PackageManager.MATCH_DISABLED_COMPONENTS)).toString()
    } catch (_: PackageManager.NameNotFoundException) { pkg }

    private fun run(cmd: String): Boolean = output(cmd) != null

    /** Runs one shell command through Shizuku; null (and lastError set) when it fails. */
    private fun output(cmd: String): String? {
        if (!Shizuku.pingBinder() || Shizuku.checkSelfPermission() != PackageManager.PERMISSION_GRANTED) {
            lastError = "Shizuku is not ready"; return null
        }
        return try {
            val p = newProcess(arrayOf("sh", "-c", cmd))
            val out = p.inputStream.bufferedReader().readText()
            val err = p.errorStream.bufferedReader().readText()
            if (p.waitFor() != 0) { lastError = err.ifBlank { "exit ${p.exitValue()}" }.trim(); null }
            else { lastError = null; out }
        } catch (e: Exception) {
            lastError = e.message ?: e.javaClass.simpleName
            null
        }
    }

    companion object {
        const val SHIZUKU_PACKAGE = "moe.shizuku.privileged.api"
        private const val REQUEST_CODE = 41

        // Shizuku 13 keeps newProcess off its public API in favour of user services; for one-shot
        // shell commands the process call is still the right tool, so it is reached reflectively.
        private val newProcessMethod by lazy {
            Shizuku::class.java.getDeclaredMethod("newProcess", Array<String>::class.java, Array<String>::class.java, String::class.java)
                .apply { isAccessible = true }
        }

        private fun newProcess(cmd: Array<String>): ShizukuRemoteProcess =
            newProcessMethod.invoke(null, cmd, null, null) as ShizukuRemoteProcess
    }
}
