package com.rixmerz.ryokumobile

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.Handler
import android.os.Looper
import android.view.Display
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
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
 * is hidden on the home screen, in this app and in every other app. Tapping it asks the computer
 * (pc/ryoku_link.py) to take the app in front. Drag it to move it.
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
        if (event.eventType != AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) return
        val pkg = event.packageName?.toString() ?: return
        when {
            // The button's own overlay windows report themselves as this package too (a plain view class,
            // never an activity): they must not hide the button.
            pkg == packageName && event.className?.startsWith("android.") == true -> return
            // A cast app shown on the computer's virtual display reports its windows here as well; it is
            // not what the phone's screen shows, so it must not bring the button up over the home screen.
            !onPhoneScreen(event) -> return
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

    /** False when the event's window is on another display than the phone's own (a virtual display). */
    private fun onPhoneScreen(event: AccessibilityEvent): Boolean {
        val displays = windowsOnAllDisplays
        for (i in 0 until displays.size()) {
            if (displays.valueAt(i).any { it.id == event.windowId }) return displays.keyAt(i) == Display.DEFAULT_DISPLAY
        }
        return true // window already gone: assume the phone's screen, as the service always did
    }

    override fun onInterrupt() = Unit

    override fun onUnbind(intent: Intent?): Boolean {
        removeBubble()
        removeNotice()
        return super.onUnbind(intent)
    }

    private fun homePackages(): Set<String> =
        packageManager.queryIntentActivities(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME), 0)
            .mapTo(HashSet()) { it.activityInfo.packageName }

    private fun refreshBubble() {
        if (front == null) removeBubble() else if (bubble == null) addBubble()
    }

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

    /** A link made for this one request: the app need not be open, and nothing stays connected between taps. */
    private fun sendToPc(pkg: String) {
        if (sending) return
        if (pkg !in CASTABLE) return say("Only YouTube can be sent to the PC for now")
        sending = true
        say("Sending to the PC…")
        thread(name = "cast-to-pc") {
            val link = DesktopLink()
            try {
                link.start(applicationContext)
                if (!waitFor(8_000) { link.connected }) return@thread say("No computer found: plug the cable in")
                // A bridge from before casting never answers `cast`; say so now instead of after 35 s of silence.
                if (!link.castable) return@thread say("The PC bridge is out of date: restart ryoku-link on the computer")
                link.cast(pkg)
                // The computer waits up to ~25 s for scrcpy to open its window.
                if (!waitFor(45_000) { link.castReply != null }) return@thread say("The computer did not answer")
                val reply = link.castReply!!
                say(if (reply.workspace != null) "Sent to the PC, desktop ${reply.workspace}" else "Could not send: ${reply.error}")
            } finally {
                link.stop()
                sending = false
            }
        }
    }

    private fun waitFor(timeoutMs: Long, condition: () -> Boolean): Boolean {
        val deadline = System.nanoTime() + timeoutMs * 1_000_000
        while (!condition()) {
            if (System.nanoTime() >= deadline) return false
            Thread.sleep(100)
        }
        return true
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
        // Apps that may be sent for now; the bridge enforces the same list (CASTABLE_PACKAGES in pc/ryoku_link.py).
        val CASTABLE = setOf("com.google.android.youtube")
    }
}
