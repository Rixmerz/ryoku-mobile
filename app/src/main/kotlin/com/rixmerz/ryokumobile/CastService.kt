package com.rixmerz.ryokumobile

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.media.MediaMetadata
import android.media.session.MediaController
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityWindowInfo
import android.widget.TextView
import androidx.compose.ui.graphics.toArgb
import kotlin.concurrent.thread

/**
 * "Send to PC" over every app, without the Ryoku app open. An accessibility service is the one
 * component that can both draw over other apps (TYPE_ACCESSIBILITY_OVERLAY, no overlay permission)
 * and be told which app is in front. A three-finger gesture is not offered: the framework only
 * delivers multi-finger gestures in touch-exploration mode, where every single tap turns into
 * explore-by-touch for the whole phone.
 *
 * The button sits on the edge of the screen only while a sendable app (YouTube for now) is in front; it
 * is hidden on the home screen, in this app and in every other app. Tapping it hands the video to the
 * computer (see PcHandoff). Drag it to move it.
 */
class CastService : AccessibilityService() {
    private val ui = Handler(Looper.getMainLooper())
    private var front: String? = null
    private var bubble: View? = null
    private var params: WindowManager.LayoutParams? = null
    @Volatile private var sending = false
    private var notice: View? = null

    // ponytail: the position is kept for this run only; persist it if people move the button a lot.
    private var savedX = -1
    private var savedY = -1

    override fun onAccessibilityEvent(event: AccessibilityEvent) {
        // Coming back to an app (the share sheet closed, BACK to the home screen) does not always raise a
        // window-state change: only the window list changes. Re-read what is in front.
        if (event.eventType == AccessibilityEvent.TYPE_WINDOWS_CHANGED) {
            val pkg = phoneFrontApp() ?: return
            front = if (pkg in CASTABLE) pkg else null
            refreshBubble()
            return
        }
        if (event.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) return
        val pkg = event.packageName?.toString() ?: return
        when {
            // The button's own overlay windows report themselves as this package too (a plain view class,
            // never an activity): they must not hide the button.
            pkg == packageName && event.className?.startsWith("android.") == true -> return
            // Only a sendable app in front shows the button. The home screen, this app and every other
            // app with a launcher entry hide it.
            pkg in CASTABLE -> front = pkg
            pkg == packageName || pkg in homePackages() -> front = null
            // The shade, the keyboard and system dialogs pass through here too, and the app underneath
            // stays the one in front; anything with a launcher entry is a different app in front.
            packageManager.getLaunchIntentForPackage(pkg) != null -> front = null
            else -> return
        }
        refreshBubble()
    }

    /** The package of the active application window, or null when unknown. */
    private fun phoneFrontApp(): String? {
        val apps = windows.filter { it.type == AccessibilityWindowInfo.TYPE_APPLICATION }
        return (apps.firstOrNull { it.isActive } ?: apps.firstOrNull())?.root?.packageName?.toString()
    }

    override fun onInterrupt() = Unit

    override fun onServiceConnected() {
        PcHandoff.say = ::say
    }

    override fun onUnbind(intent: Intent?): Boolean {
        PcHandoff.say = null
        removeBubble()
        removeNotice()
        unwatchVideo()
        return super.onUnbind(intent)
    }

    private fun homePackages(): Set<String> =
        packageManager.queryIntentActivities(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME), 0)
            .mapTo(HashSet()) { it.activityInfo.packageName }

    private fun refreshBubble() {
        if (front == null) removeBubble() else if (bubble == null) addBubble()
        if (front == null) unwatchVideo() else watchVideo()
    }

    private var watched: MediaController? = null
    private val videoChanged = object : MediaController.Callback() {
        override fun onMetadataChanged(metadata: MediaMetadata?) = warmSoon()
    }
    private val warm = Runnable { thread(name = "warm-pc") { PcHandoff.warm(applicationContext) } }

    /**
     * While YouTube is in front, the computer is told which video it is playing (and each time it changes), so it
     * has looked the video up before the button is pressed. YouTube sets title and length a moment apart, hence
     * the short wait before telling.
     */
    private fun watchVideo() {
        val session = PcHandoff.youtubeSession(applicationContext)
        if (session?.sessionToken != watched?.sessionToken) {
            unwatchVideo()
            session?.registerCallback(videoChanged)
            watched = session
        }
        if (session != null) warmSoon()
    }

    private fun unwatchVideo() {
        ui.removeCallbacks(warm)
        watched?.unregisterCallback(videoChanged)
        watched = null
    }

    private fun warmSoon() { ui.removeCallbacks(warm); ui.postDelayed(warm, 1_500) }

    private fun addBubble() {
        val t = Tokens(signature)
        val density = resources.displayMetrics.density
        val size = (44 * density).toInt()
        val view = TextView(this).apply {
            text = "PC"
            textSize = 12f
            gravity = Gravity.CENTER
            setTextColor(t.ink.toArgb())
            contentDescription = "Send this app to the PC"
            background = GradientDrawable().apply {
                shape = GradientDrawable.OVAL
                setColor(t.paper.toArgb())
                setStroke((1 * density).toInt().coerceAtLeast(1), t.ink.toArgb())
            }
            alpha = 0.85f
        }
        val lp = WindowManager.LayoutParams(
            size, size, WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCH_MODAL,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            val screen = resources.displayMetrics
            x = if (savedX >= 0) savedX else screen.widthPixels - size
            y = if (savedY >= 0) savedY else screen.heightPixels / 3
        }
        view.setOnTouchListener(dragOrTap(lp))
        getSystemService(WindowManager::class.java).addView(view, lp)
        bubble = view
        params = lp
    }

    private fun removeBubble() {
        val view = bubble ?: return
        runCatching { getSystemService(WindowManager::class.java).removeView(view) }
        bubble = null
        params = null
    }

    /** A touch that stays within the slop is a tap and sends; anything further drags the button. */
    private fun dragOrTap(lp: WindowManager.LayoutParams): View.OnTouchListener {
        val slop = ViewConfiguration.get(this).scaledTouchSlop
        var downX = 0f; var downY = 0f; var startX = 0; var startY = 0; var dragged = false
        return View.OnTouchListener { v, e ->
            when (e.actionMasked) {
                MotionEvent.ACTION_DOWN -> { downX = e.rawX; downY = e.rawY; startX = lp.x; startY = lp.y; dragged = false }
                MotionEvent.ACTION_MOVE -> {
                    val dx = e.rawX - downX; val dy = e.rawY - downY
                    if (dragged || dx * dx + dy * dy > slop * slop) {
                        dragged = true
                        lp.x = startX + dx.toInt(); lp.y = startY + dy.toInt()
                        savedX = lp.x; savedY = lp.y
                        getSystemService(WindowManager::class.java).updateViewLayout(v, lp)
                    }
                }
                MotionEvent.ACTION_UP -> if (!dragged) front?.let { sendToPc(it) }
            }
            true
        }
    }

    /**
     * The button: the video's title, channel, length and minute come from its media session (nothing is
     * driven on screen), the computer finds and opens it, and the phone's copy is paused. Nothing stays
     * connected between taps.
     */
    private fun sendToPc(pkg: String) {
        if (sending) return
        if (pkg !in CASTABLE) return say("Only YouTube can be sent to the PC for now")
        sending = true
        val tappedAt = SystemClock.elapsedRealtime()
        say("Sending to the PC…")
        thread(name = "send-to-pc") {
            try {
                PcHandoff.sendPlaying(applicationContext, tappedAt)
            } finally {
                sending = false
            }
        }
    }

    /**
     * A line over every app for a few seconds. A Toast is not used: Android drops the toasts of an app
     * whose notifications are switched off (logcat: "Suppressing toast ... by user request"), and the
     * button then looks like it does nothing. An accessibility overlay needs no permission.
     */
    private fun say(text: String) { ui.post {
        removeNotice()
        val t = Tokens(signature)
        val density = resources.displayMetrics.density
        val view = TextView(this).apply {
            this.text = text
            textSize = 14f
            gravity = Gravity.CENTER
            setTextColor(t.ink.toArgb())
            setPadding((16 * density).toInt(), (10 * density).toInt(), (16 * density).toInt(), (10 * density).toInt())
            background = GradientDrawable().apply {
                cornerRadius = 12 * density
                setColor(t.paper.toArgb())
                setStroke((1 * density).toInt().coerceAtLeast(1), t.ink.toArgb())
            }
        }
        val lp = WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT, WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.BOTTOM or Gravity.CENTER_HORIZONTAL
            y = (96 * density).toInt()
        }
        runCatching { getSystemService(WindowManager::class.java).addView(view, lp) }.onSuccess {
            notice = view
            ui.postDelayed({ if (notice === view) removeNotice() }, NOTICE_MS)
        }
    } }

    private fun removeNotice() {
        val view = notice ?: return
        runCatching { getSystemService(WindowManager::class.java).removeView(view) }
        notice = null
    }

    private companion object {
        const val NOTICE_MS = 5_000L
        // Apps that may be sent for now: YouTube, whose links the bridge opens (see PcHandoff).
        val CASTABLE = setOf(PcHandoff.YOUTUBE)
    }
}
