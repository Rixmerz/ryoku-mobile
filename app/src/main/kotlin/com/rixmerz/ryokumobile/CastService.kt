package com.rixmerz.ryokumobile

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.graphics.PixelFormat
import android.graphics.Rect
import android.graphics.drawable.GradientDrawable
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.WindowManager
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
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

    /**
     * The button: pause the video and note its minute, read the video's link off YouTube's own share
     * sheet, and hand both to the computer. Nothing stays connected between taps.
     */
    private fun sendToPc(pkg: String) {
        if (sending) return
        if (pkg !in CASTABLE) return say("Only YouTube can be sent to the PC for now")
        sending = true
        say("Sending to the PC…")
        thread(name = "send-to-pc") {
            try {
                PcHandoff.hold(applicationContext)
                val url = try {
                    readShareLink()
                } catch (e: ShareWalkFailed) {
                    PcHandoff.resume(applicationContext)
                    return@thread say("Could not read the video link (${e.message}): open Share in YouTube and pick Send to PC")
                }
                PcHandoff.deliver(applicationContext, url)
            } finally {
                sending = false
            }
        }
    }

    /** A step of the share-sheet walk that found nothing; the message says which, for the person to see. */
    private class ShareWalkFailed(message: String) : Exception(message)

    /**
     * YouTube's Share button opens its own sheet; the last entry of the app row there ("More") opens
     * Android's chooser, whose header shows the link as text. The sheet is closed again afterwards.
     * Throws [ShareWalkFailed] when a step is not found (another language for Share, a player without the button).
     */
    private fun readShareLink(): String {
        val share = awaitNode { root -> root.descendants().firstOrNull { it.contentDescription?.toString()?.lowercase() in SHARE_LABELS } }
            ?: throw ShareWalkFailed("no Share button")
        if (!tap(share)) throw ShareWalkFailed("Share button would not click")
        val row = awaitNode { root -> root.descendants().filter { it.isScrollable && it.childCount > 1 }.minByOrNull { it.area() } }
            ?: throw ShareWalkFailed("no app row")
        // The row only builds the entries on screen: scroll to its end, where "More" is the last one.
        var guard = 0
        while (row.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD) && guard++ < 20) { Thread.sleep(350); row.refresh() }
        // RecyclerView keeps recycled entries among its children: the one furthest right on screen is "More".
        val more = (0 until row.childCount).mapNotNull { row.getChild(it)?.let(::clickableIn) }
            .filter { it.isVisibleToUser }.maxByOrNull { Rect().also(it::getBoundsInScreen).left }
            ?: throw ShareWalkFailed("no clickable entry in the app row (${row.childCount} children, scrolled $guard times)")
        if (!more.performAction(AccessibilityNodeInfo.ACTION_CLICK)) throw ShareWalkFailed("More would not click (${more.className} ${more.contentDescription})")
        val text = awaitNode { root -> root.descendants().firstOrNull { it.text?.let(SHARED_LINK::matches) == true } }?.text?.toString()
            ?: throw ShareWalkFailed("no link in the chooser")
        // Close the chooser, then YouTube's own sheet when it is still up (a second BACK would leave the app).
        performGlobalAction(GLOBAL_ACTION_BACK)
        Thread.sleep(600)
        if (rootInActiveWindow?.descendants()?.any { it.viewIdResourceName?.endsWith(":id/bottom_sheet_list") == true } == true) performGlobalAction(GLOBAL_ACTION_BACK)
        return text
    }

    /** The first non-null `find` over the window in front, retried for a few seconds while the screen settles. */
    private fun awaitNode(find: (AccessibilityNodeInfo) -> AccessibilityNodeInfo?): AccessibilityNodeInfo? {
        var found: AccessibilityNodeInfo? = null
        PcHandoff.waitFor(5_000) { found = rootInActiveWindow?.let(find); found != null }
        return found
    }

    /** The node itself or the first node inside it that can be clicked (a list entry's wrapper is not). */
    private fun clickableIn(node: AccessibilityNodeInfo): AccessibilityNodeInfo? =
        (sequenceOf(node) + node.descendants()).firstOrNull { it.isClickable }

    /** Clicks the node, or the closest ancestor that can be clicked. */
    private fun tap(node: AccessibilityNodeInfo): Boolean {
        var at: AccessibilityNodeInfo? = node
        while (at != null && !at.isClickable) at = at.parent
        return at?.performAction(AccessibilityNodeInfo.ACTION_CLICK) == true
    }

    private fun AccessibilityNodeInfo.descendants(): Sequence<AccessibilityNodeInfo> = sequence {
        for (i in 0 until childCount) { val child = getChild(i) ?: continue; yield(child); yieldAll(child.descendants()) }
    }

    private fun AccessibilityNodeInfo.area(): Int = Rect().also { getBoundsInScreen(it) }.let { it.width() * it.height() }

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
        // YouTube labels its Share button in the phone's language. A language missing here makes the button
        // answer with how to share by hand; add the label to support it.
        // The chooser's header shows exactly the shared link; the bridge checks it again.
        val SHARED_LINK = Regex("https?://(www\\.|m\\.)?(youtube\\.com|youtu\\.be)/\\S+")
        val SHARE_LABELS = setOf("share", "compartir", "partager", "teilen", "condividi", "compartilhar", "partilhar",
            "поделиться", "共有", "分享", "공유", "delen", "paylaş", "udostępnij", "dela")
    }
}
