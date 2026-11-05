package com.rixmerz.ryokumobile

import android.content.ComponentName
import android.content.Context
import android.media.MediaMetadata
import android.media.session.MediaController
import android.media.session.MediaSessionManager
import android.media.session.PlaybackState
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import android.widget.Toast
import kotlin.concurrent.thread

/**
 * What YouTube's media session reports at one instant. [positionMs] is the position as last reported and
 * [ageMs] the time since that report (zero when paused): a playing session reports its position once, so
 * the computer adds `ageMs * speed` to get the real minute (see playback_seconds in pc/ryoku_link.py).
 */
class NowPlaying(
    val title: String?, val channel: String?, val durationMs: Long,
    val positionMs: Long?, val ageMs: Long, val speed: Float, val playing: Boolean,
)

/**
 * Hands the YouTube video on screen over to the computer and pauses the phone's copy so it is not playing in two
 * places. The button ([sendPlaying]) sends what the media session knows about the video and the computer
 * finds it on YouTube; Share -> Send to PC ([deliverShared]) sends the link itself.
 */
object PcHandoff {
    const val YOUTUBE = "com.google.android.youtube"
    private const val TAG = "RyokuHandoff"

    /** Where messages for the person go: the accessibility service's overlay while it runs, else a toast. */
    @Volatile var say: ((String) -> Unit)? = null

    private val main = Handler(Looper.getMainLooper())

    /**
     * The button: read the video's identity and minute, then pause it and hand it over. Blocking, run it on a
     * worker thread. [tappedAt] is the [SystemClock.elapsedRealtime] of the tap, for the timing in the log.
     */
    fun sendPlaying(context: Context, tappedAt: Long) {
        val tell = messenger(context)
        val link = DesktopLink().also { it.start(context) } // dials while the session is read
        try {
            val controller = youtubeSession(context)
                ?: return tell("Cannot see what YouTube is playing: allow notification access, or use Share > Send to PC")
            val now = nowPlaying(controller) // before the pause: a paused session no longer moves its position on
            handOver(controller, link, tell, needsVideo = true, now.positionMs != null, tappedAt) { link.openVideo(now) }
        } finally {
            link.stop()
        }
    }

    /** The key of the video the computer was last told about, so a repeated event does not tell it twice. */
    @Volatile private var warmed: Triple<String?, String?, Long>? = null

    /**
     * Tells the computer which video YouTube is playing, so it has looked it up by the time the button is
     * pressed. Blocking, run it on a worker thread. A computer that cannot be reached is ignored: the button
     * then does the lookup itself.
     */
    fun warm(context: Context) {
        val now = youtubeSession(context)?.let(::nowPlaying) ?: return
        val key = Triple(now.title, now.channel, now.durationMs)
        if (now.title.isNullOrBlank() || now.durationMs <= 0 || key == warmed) return
        val link = DesktopLink().also { it.start(context) }
        try {
            if (!waitFor(3_000) { link.connected } || !link.canOpenVideo) return
            link.resolveVideo(now)
            warmed = key
            Thread.sleep(300) // the send is queued: closing the link right away would drop it
        } finally {
            link.stop()
        }
    }

    /** [sendPlaying] for the link YouTube's share sheet hands over; only its first link is used. */
    fun deliverShared(context: Context, text: String) {
        val url = LINK.find(text)?.value ?: return messenger(context)("No link to send")
        val app = context.applicationContext
        thread(name = "send-to-pc") {
            val tell = messenger(app)
            val tappedAt = SystemClock.elapsedRealtime()
            val link = DesktopLink().also { it.start(app) }
            try {
                val controller = youtubeSession(app)
                val now = controller?.let(::nowPlaying)
                handOver(controller, link, tell, needsVideo = false, now?.positionMs != null, tappedAt) { link.openUrl(url, now) }
            } finally {
                link.stop()
            }
        }
    }

    /**
     * Waits for the computer, runs [send], pauses the phone's video and tells the person the outcome. The video
     * is started again when the computer did not take it. Nothing from an exception is ever shown.
     */
    private fun handOver(controller: MediaController?, link: DesktopLink, tell: (String) -> Unit, needsVideo: Boolean, positionKnown: Boolean, tappedAt: Long, send: () -> Unit) {
        if (!waitFor(8_000) { link.connected }) return tell("No computer found: plug the cable in")
        // A bridge from before this feature never answers; say so now instead of after a long wait.
        if (!(if (needsVideo) link.canOpenVideo else link.canOpenUrl)) return tell("The PC bridge is out of date: restart ryoku-link on the computer")
        send()
        controller?.transportControls?.pause()
        if (!waitFor(25_000) { link.openUrlReply != null }) {
            controller?.transportControls?.play()
            return tell("The computer did not answer")
        }
        val reply = link.openUrlReply ?: return
        Log.w(TAG, "tap to reply: ${SystemClock.elapsedRealtime() - tappedAt} ms, workspace=${reply.workspace}, error=${reply.error}")
        if (reply.workspace == null) controller?.transportControls?.play()
        tell(when {
            reply.workspace == null -> "Could not send: ${reply.error}"
            !positionKnown -> "Opened on the PC, desktop ${reply.workspace}, from the start: YouTube did not report the minute"
            else -> "Opened on the PC, desktop ${reply.workspace}"
        })
    }

    private fun messenger(context: Context): (String) -> Unit =
        say ?: { text -> main.post { Toast.makeText(context, text, Toast.LENGTH_LONG).show() } }

    internal fun youtubeSession(context: Context): MediaController? = try {
        context.getSystemService(MediaSessionManager::class.java)
            .getActiveSessions(ComponentName(context, MediaListener::class.java))
            .firstOrNull { it.packageName == YOUTUBE }
    } catch (_: SecurityException) {
        null // notification access not granted: Android refuses the session list
    }

    private fun nowPlaying(c: MediaController): NowPlaying {
        val meta = c.metadata
        val state = c.playbackState
        val known = state != null && state.position != PlaybackState.PLAYBACK_POSITION_UNKNOWN
        val playing = state?.state == PlaybackState.STATE_PLAYING
        return NowPlaying(
            title = meta?.getString(MediaMetadata.METADATA_KEY_TITLE),
            channel = meta?.getString(MediaMetadata.METADATA_KEY_ARTIST) ?: meta?.getString(MediaMetadata.METADATA_KEY_ALBUM_ARTIST),
            durationMs = meta?.getLong(MediaMetadata.METADATA_KEY_DURATION) ?: 0,
            positionMs = if (known) state!!.position else null,
            ageMs = if (known && playing) (SystemClock.elapsedRealtime() - state!!.lastPositionUpdateTime).coerceAtLeast(0) else 0,
            speed = state?.playbackSpeed?.takeIf { it > 0f } ?: 1f,
            playing = playing,
        )
    }

    fun waitFor(timeoutMs: Long, condition: () -> Boolean): Boolean {
        val deadline = System.nanoTime() + timeoutMs * 1_000_000
        while (!condition()) {
            if (System.nanoTime() >= deadline) return false
            Thread.sleep(25)
        }
        return true
    }

    private val LINK = Regex("https?://\\S+")
}
