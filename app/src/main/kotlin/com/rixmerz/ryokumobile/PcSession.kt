package com.rixmerz.ryokumobile

import android.content.Context
import android.media.AudioManager
import android.media.session.MediaController
import android.media.session.PlaybackState
import android.os.Handler
import android.os.Looper

/**
 * The stretch after a video was sent to the computer, during which the phone's volume buttons turn the
 * computer's volume instead of the phone's (CastService.onKeyEvent asks [volumeKey]). It ends when:
 *  - the person long-presses the PC button (the button then reads VOL while a session runs),
 *  - YouTube plays on the phone again (the phone's video was paused by the send; see [playbackChanged]),
 *  - the computer cannot be reached or cannot set its volume, so a dead link never swallows the buttons, or
 *  - the accessibility service stops.
 * Outside a session every key goes to the phone as usual.
 *
 * The session keeps the link the send used open, so a button press is one message instead of a new connection.
 */
object PcSession {
    const val LEVEL_MS = 1_500L

    @Volatile private var link: DesktopLink? = null
    private var audio: AudioManager? = null
    private var watched: MediaController? = null
    // The send pauses the phone's video a moment after it asks the computer: a PLAYING report only counts as
    // the person resuming once the session has seen the video not playing.
    @Volatile private var seenPaused = false
    @Volatile private var levelShown = false

    /** Where a line for the person goes (the accessibility service's overlay); set while that service runs. */
    @Volatile var showLevel: ((String) -> Unit)? = null
    /** Told whenever a session starts or ends, so the button can change its label. */
    @Volatile var onChange: (() -> Unit)? = null

    val active: Boolean get() = link != null

    private val playbackChanged = object : MediaController.Callback() {
        override fun onPlaybackStateChanged(state: PlaybackState?) {
            if (state?.state != PlaybackState.STATE_PLAYING) seenPaused = true
            else if (seenPaused) end("YouTube is playing on the phone: volume buttons control the phone again")
        }
    }

    /** Starts a session on [link], the one a successful send used; any session already running ends first. */
    @Synchronized fun begin(context: Context, controller: MediaController?, link: DesktopLink) {
        end(null)
        audio = context.getSystemService(AudioManager::class.java)
        seenPaused = controller?.playbackState?.state != PlaybackState.STATE_PLAYING
        levelShown = false
        link.onVolume = ::onVolume
        controller?.registerCallback(playbackChanged, Handler(Looper.getMainLooper()))
        watched = controller
        this.link = link
        onChange?.invoke()
    }

    /** Ends the session, if there is one, and tells the person [message] when it is given. */
    @Synchronized fun end(message: String?) {
        val ended = link ?: return
        link = null
        ended.onVolume = null
        watched?.unregisterCallback(playbackChanged)
        watched = null
        ended.stop()
        if (message != null) PcHandoff.say?.invoke(message)
        onChange?.invoke()
    }

    /**
     * A volume button, [step] 1 for up and -1 for down; [down] is false for the release. True when the computer
     * takes the key (the phone's volume must not change), false when it is the phone's: no session, a call or the
     * ringer (the buttons silence it), or a computer that cannot take it, which also ends the session.
     */
    fun volumeKey(step: Int, down: Boolean): Boolean {
        val session = link ?: return false
        if (audio?.mode != AudioManager.MODE_NORMAL) return false
        if (!session.connected || !session.canSetVolume) {
            end(if (session.connected) "The PC bridge is out of date: restart ryoku-link. Volume buttons control the phone again"
                else "The PC is not reachable: volume buttons control the phone again")
            return false
        }
        if (down) session.volume(step)
        return true
    }

    private fun onVolume(reply: VolumeReply) {
        val level = reply.level ?: return end("The PC volume cannot be changed: ${reply.error}. Volume buttons control the phone again")
        val hint = if (levelShown) "" else " · long-press the button to end"
        levelShown = true
        showLevel?.invoke("PC volume $level%${if (reply.muted) " (muted)" else ""}$hint")
    }
}
