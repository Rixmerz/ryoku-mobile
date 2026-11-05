package com.rixmerz.ryokumobile

import android.content.ComponentName
import android.content.Context
import android.media.session.MediaController
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.widget.Toast
import kotlin.concurrent.thread

/**
 * Hands the YouTube video on screen over to the computer: its link and the minute it is at go to the bridge
 * (pc/ryoku_link.py), which opens it in the PC's browser; the phone's video is paused so it is not
 * playing in two places. The link arrives from [CastService] (read off the share sheet) or from
 * [SendToPcActivity] (YouTube's Share -> Send to PC); both end up in [deliver].
 */
object PcHandoff {
    const val YOUTUBE = "com.google.android.youtube"

    /** Where messages for the person go: the accessibility service's overlay while it runs, else a toast. */
    @Volatile var say: ((String) -> Unit)? = null

    /** The minute taken when the button was pressed, used by the [deliver] that follows it. */
    private class Held(val seconds: Long?, val at: Long)
    @Volatile private var held: Held? = null

    private val main = Handler(Looper.getMainLooper())

    /**
     * Reads the video's minute and pauses it, remembering the minute for the next [deliver]. Needs the
     * notification access that lets this app see media sessions; without it the minute is unknown (null)
     * and the video is not paused.
     */
    fun hold(context: Context) {
        val controller = youtubeSession(context)
        held = Held(controller?.let(::positionSeconds), SystemClock.elapsedRealtime())
        controller?.transportControls?.pause()
    }

    /** Starts the video again after a handoff that did not happen. */
    fun resume(context: Context) {
        held = null
        youtubeSession(context)?.transportControls?.play()
    }

    /** The shared text's link goes to the computer; blocking, run it on a worker thread. */
    fun deliver(context: Context, url: String) {
        val tell = messenger(context)
        val pressed = held?.takeIf { SystemClock.elapsedRealtime() - it.at < HOLD_MS }
        held = null
        // Shared by hand (YouTube's Share -> Send to PC) nobody pressed the button: read and pause now.
        val seconds = if (pressed != null) pressed.seconds else {
            hold(context)
            held?.seconds.also { held = null }
        }
        val link = DesktopLink()
        try {
            link.start(context)
            if (!waitFor(8_000) { link.connected }) return tell("No computer found: plug the cable in")
            // A bridge from before this feature never answers `open_url`; say so now instead of after a long wait.
            if (!link.canOpenUrl) return tell("The PC bridge is out of date: restart ryoku-link on the computer")
            link.openUrl(url, seconds)
            if (!waitFor(25_000) { link.openUrlReply != null }) return tell("The computer did not answer")
            val reply = link.openUrlReply!!
            tell(when {
                reply.workspace == null -> "Could not send: ${reply.error}"
                seconds == null -> "Opened on the PC, desktop ${reply.workspace}. Allow notification access for the minute"
                else -> "Opened on the PC, desktop ${reply.workspace}"
            })
        } finally {
            link.stop()
        }
    }

    /** [deliver] for the text YouTube's share sheet hands over; only its first link is used. */
    fun deliverShared(context: Context, text: String) {
        val url = LINK.find(text)?.value ?: return messenger(context)("No link to send")
        thread(name = "send-to-pc") { deliver(context.applicationContext, url) }
    }

    private fun messenger(context: Context): (String) -> Unit =
        say ?: { text -> main.post { Toast.makeText(context, text, Toast.LENGTH_LONG).show() } }

    private fun youtubeSession(context: Context): MediaController? = try {
        context.getSystemService(MediaSessionManager::class.java)
            .getActiveSessions(ComponentName(context, MediaListener::class.java))
            .firstOrNull { it.packageName == YOUTUBE }
    } catch (_: SecurityException) {
        null // notification access not granted: Android refuses the session list
    }

    /** Whole seconds into the video: the position last reported, moved on by the time since (a playing session reports it once). */
    private fun positionSeconds(c: MediaController): Long? {
        val s = c.playbackState ?: return null
        if (s.position == PlaybackState.PLAYBACK_POSITION_UNKNOWN) return null
        val since = if (s.state == PlaybackState.STATE_PLAYING) (SystemClock.elapsedRealtime() - s.lastPositionUpdateTime) * s.playbackSpeed else 0f
        return ((s.position + since) / 1000).toLong().coerceAtLeast(0)
    }

    fun waitFor(timeoutMs: Long, condition: () -> Boolean): Boolean {
        val deadline = System.nanoTime() + timeoutMs * 1_000_000
        while (!condition()) {
            if (System.nanoTime() >= deadline) return false
            Thread.sleep(100)
        }
        return true
    }

    private const val HOLD_MS = 90_000L
    private val LINK = Regex("https?://\\S+")
}
