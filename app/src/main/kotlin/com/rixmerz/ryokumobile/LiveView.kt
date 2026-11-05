package com.rixmerz.ryokumobile

import android.app.Activity
import android.content.pm.ActivityInfo
import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import android.view.Surface
import android.view.SurfaceHolder
import android.view.SurfaceView
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Text
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.focus.FocusRequester
import androidx.compose.ui.focus.focusRequester
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.AwaitPointerEventScope
import androidx.compose.ui.input.pointer.PointerEvent
import androidx.compose.ui.input.pointer.PointerInputScope
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.TextRange
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.TextFieldValue
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import kotlinx.coroutines.delay
import kotlinx.coroutines.withTimeoutOrNull
import java.io.IOException
import java.io.InputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.math.abs

/**
 * One desktop, live: the computer's headless output (pc/ryoku_link.py) as an H.264 stream on a
 * SurfaceView, with the touchscreen driving its pointer. Desktops are wide, so the screen turns
 * to landscape while this is open and the picture is letterboxed to the stream's own aspect;
 * touches are mapped onto the picture, never onto the bars.
 *
 * Gestures, chosen so none collides with another:
 *  - one finger: tap clicks, hold (long press) right-clicks, moving drags with the left button;
 *  - two fingers, vertical: scroll;
 *  - two fingers, horizontal, a quarter of the width: previous / next desktop.
 * The workspace switch is a two-finger swipe because an edge swipe is the system's back gesture
 * and one finger is the drag; the price is that a two-finger *horizontal* scroll does not exist.
 */
@Composable
fun LiveView(link: DesktopLink, workspace: Int, onClose: () -> Unit) {
    val density = LocalDensity.current
    val activity = LocalContext.current as? Activity
    var target by remember { mutableIntStateOf(workspace) }
    var hint by remember { mutableStateOf<String?>(null) }
    var keys by remember { mutableStateOf(false) }

    BoxWithConstraints(Modifier.fillMaxSize().background(Color.Black).testTag("live")) {
        val widthPx = with(density) { maxWidth.roundToPx() }
        val heightPx = with(density) { maxHeight.roundToPx() }

        // Also re-asks after a reconnect: the computer drops the view with the connection.
        LaunchedEffect(link.connected) { if (link.connected) link.openView(target, widthPx, heightPx) }
        DisposableEffect(Unit) {
            activity?.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
            onDispose {
                link.closeView()
                activity?.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
            }
        }
        LaunchedEffect(link.live?.workspace) { link.live?.workspace?.let { target = it } }
        LaunchedEffect(link.refused) {
            link.refused?.let { hint = "Desktop $it is on the PC screen" }
            link.clearRefusal()
        }
        LaunchedEffect(hint) { if (hint != null) { delay(1800); hint = null } }

        // Opening can take a while: the computer waits out its own display hotplug handling first.
        if (link.live == null) Text("OPENING DESKTOP…", color = T.inkFaint, fontFamily = Type.mono, fontSize = Type.fSmall,
            letterSpacing = Type.trackMark, modifier = Modifier.align(Alignment.Center).testTag("live-opening"))
        link.live?.let { info ->
            Box(Modifier.align(Alignment.Center).aspectRatio(info.width.toFloat() / info.height)) {
                key(info.port, info.width, info.height) { LiveSurface(info) }
                Box(Modifier.fillMaxSize().pointerInput(info.width) {
                    desktopGestures(link, info.width) { step ->
                        // Busy desktops (on the PC monitor) are skipped; the ends do not wrap.
                        val free = link.workspaces.filter { !it.busy || it.id == target }
                        val at = free.indexOfFirst { it.id == target }
                        free.getOrNull(at + step)?.let {
                            target = it.id
                            hint = "Desktop ${it.name}"
                            link.openView(it.id, widthPx, heightPx)
                        }
                    }
                }.testTag("live-surface"))
            }
        }

        Row(Modifier.align(Alignment.TopStart).systemBarsPadding().padding(Geo.s3)) {
            Pill("‹  CLOSE", Modifier.testTag("live-close"), onClose)
        }
        Row(Modifier.align(Alignment.TopEnd).systemBarsPadding().padding(Geo.s3)) {
            Pill(if (keys) "KEYS ·" else "KEYS", Modifier.testTag("live-keys")) { keys = !keys }
        }
        hint?.let {
            Box(Modifier.align(Alignment.BottomCenter).systemBarsPadding().padding(Geo.s6)) { Pill(it.uppercase(), Modifier) {} }
        }
        if (keys) KeyboardInput(link, Modifier.align(Alignment.BottomCenter))
    }
}

@Composable
private fun Pill(label: String, modifier: Modifier, onClick: () -> Unit) {
    val shape = RoundedCornerShape(Geo.radius)
    Text(label, color = T.ink, fontFamily = Type.mono, fontSize = Type.fMicro, letterSpacing = Type.trackLabel,
        modifier = modifier.clip(shape).background(Color.Black.copy(alpha = 0.6f)).border(Geo.border, T.line, shape)
            .clickable(onClick = onClick).padding(horizontal = Geo.s3, vertical = Geo.s2))
}

@Composable
private fun LiveSurface(info: LiveInfo) {
    AndroidView(factory = { context ->
        SurfaceView(context).apply {
            var stream: VideoStream? = null
            holder.addCallback(object : SurfaceHolder.Callback {
                override fun surfaceCreated(h: SurfaceHolder) {
                    stream = VideoStream(info.port, info.width, info.height, h.surface).also { it.start() }
                }
                override fun surfaceChanged(h: SurfaceHolder, format: Int, width: Int, height: Int) {}
                override fun surfaceDestroyed(h: SurfaceHolder) {
                    stream?.stop()
                    stream = null
                }
            })
        }
    }, modifier = Modifier.fillMaxSize())
}

// ---- Soft keyboard ----

/** Keys a soft keyboard has no button for. */
private val extraKeys = listOf("Esc" to "Escape", "Tab" to "Tab", "←" to "Left", "↑" to "Up", "↓" to "Down", "→" to "Right")

/**
 * The soft keyboard types into an invisible field that always holds one zero-width space. Any
 * change is therefore either typed text (sent as is) or the space being deleted (BackSpace),
 * and the field is reset at once, so nothing is ever composed or autocorrected on the phone.
 */
@Composable
private fun KeyboardInput(link: DesktopLink, modifier: Modifier) {
    val seed = "​"
    var field by remember { mutableStateOf(TextFieldValue(seed, TextRange(seed.length))) }
    val focus = remember { FocusRequester() }
    val keyboard = LocalSoftwareKeyboardController.current
    LaunchedEffect(Unit) { focus.requestFocus(); keyboard?.show() }
    DisposableEffect(Unit) { onDispose { keyboard?.hide() } }

    Column(modifier.imePadding().background(Color.Black.copy(alpha = 0.6f))) {
        Row(Modifier.fillMaxWidth().padding(Geo.s2), horizontalArrangement = Arrangement.spacedBy(Geo.s2)) {
            extraKeys.forEach { (label, key) -> Pill(label, Modifier) { link.pressKey(key) } }
        }
        BasicTextField(
            value = field,
            onValueChange = { v ->
                if (v.text.isEmpty()) link.pressKey("BackSpace")
                else if (v.text != seed) link.typeText(v.text.replace(seed, ""))
                field = TextFieldValue(seed, TextRange(seed.length))
            },
            // Password keeps predictive text and autocorrect out of the way.
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password, imeAction = ImeAction.Send, autoCorrectEnabled = false),
            keyboardActions = KeyboardActions(onSend = { link.pressKey("Return") }),
            modifier = Modifier.size(1.dp).alpha(0f).focusRequester(focus).testTag("live-input"),
        )
    }
}

// ---- Touch -> pointer ----

private enum class FirstMove { Lifted, Dragged, SecondFinger }

private fun PointerEvent.fingers() = changes.count { it.pressed }

private fun PointerEvent.centroid(): Offset {
    val down = changes.filter { it.pressed }
    return Offset(down.map { it.position.x }.average().toFloat(), down.map { it.position.y }.average().toFloat())
}

/**
 * Turns touches into pointer commands. [streamWidth] converts finger travel into the desktop's
 * own pixels for scrolling; [onSwipe] gets -1 (previous) or +1 (next) once per two-finger swipe.
 */
private suspend fun PointerInputScope.desktopGestures(link: DesktopLink, streamWidth: Int, onSwipe: (Int) -> Unit) {
    val slop = viewConfiguration.touchSlop
    val w = size.width.toFloat()
    val h = size.height.toFloat()
    fun x(p: Offset) = (p.x / w).coerceIn(0f, 1f)
    fun y(p: Offset) = (p.y / h).coerceIn(0f, 1f)

    awaitEachGesture {
        val down = awaitFirstDown(requireUnconsumed = false)
        val origin = down.position
        link.pointer("move", x(origin), y(origin))

        val first = withTimeoutOrNull(viewConfiguration.longPressTimeoutMillis) { firstMove(origin, slop) }

        when (first) {
            null -> { // held still: right click
                link.pointer("down", x(origin), y(origin), "right")
                link.pointer("up", x(origin), y(origin), "right")
                drainUntilLifted()
            }
            FirstMove.Lifted -> {
                link.pointer("down", x(origin), y(origin))
                link.pointer("up", x(origin), y(origin))
            }
            FirstMove.Dragged -> {
                link.pointer("down", x(origin), y(origin))
                var at = origin
                while (true) {
                    val e = awaitPointerEvent()
                    if (e.fingers() != 1) break
                    at = e.changes.first().position
                    link.pointer("move", x(at), y(at))
                    e.changes.forEach { it.consume() }
                }
                link.pointer("up", x(at), y(at))
                drainUntilLifted()
            }
            FirstMove.SecondFinger -> twoFingers(link, streamWidth, w, slop, onSwipe)
        }
    }
}

/** What the first finger does: lifts, drifts past the touch slop, or is joined by a second one. */
private suspend fun AwaitPointerEventScope.firstMove(origin: Offset, slop: Float): FirstMove {
    while (true) {
        val e = awaitPointerEvent()
        if (e.fingers() >= 2) return FirstMove.SecondFinger
        if (e.fingers() == 0) return FirstMove.Lifted
        if ((e.changes.first().position - origin).getDistance() > slop) return FirstMove.Dragged
    }
}

private suspend fun AwaitPointerEventScope.drainUntilLifted() {
    do {
        val e = awaitPointerEvent()
        e.changes.forEach { it.consume() }
    } while (e.fingers() > 0)
}

/** The axis is decided by the first real movement, then only that gesture can happen. */
private suspend fun AwaitPointerEventScope.twoFingers(
    link: DesktopLink, streamWidth: Int, width: Float, slop: Float, onSwipe: (Int) -> Unit,
) {
    var start: Offset? = null
    var last = 0f
    var vertical: Boolean? = null
    var scrolled = false
    var swiped = false
    do {
        val e = awaitPointerEvent()
        if (e.fingers() == 2) {
            val c = e.centroid()
            val s = start ?: c.also { start = it; last = it.y }
            val dx = c.x - s.x
            val dy = c.y - s.y
            if (vertical == null && maxOf(abs(dx), abs(dy)) > slop * 1.5f) vertical = abs(dy) >= abs(dx)
            if (vertical == true) {
                // Content follows the finger: moving up scrolls the page down (positive lines).
                link.scroll(0f, -(c.y - last) * streamWidth / width / 15f)
                scrolled = true
            } else if (vertical == false && !swiped && abs(dx) > width / 4) {
                swiped = true
                onSwipe(if (dx > 0) -1 else 1)
            }
            last = c.y
        }
        e.changes.forEach { it.consume() }
    } while (e.fingers() > 0)
    if (scrolled) link.scroll(0f, 0f, end = true)
}

// ---- Video ----

/**
 * Reads the computer's H.264 stream (Annex-B, one access unit per AUD NAL) and decodes it
 * straight onto [surface]. Reconnects quietly while the computer is not serving yet.
 */
class VideoStream(private val port: Int, private val width: Int, private val height: Int, private val surface: Surface) {
    @Volatile private var running = false
    @Volatile private var socket: Socket? = null
    private var thread: Thread? = null

    fun start() {
        running = true
        thread = Thread({ loop() }, "ryoku-video").also { it.start() }
    }

    /** Returns once the decoder is released, because the surface may be destroyed right after. */
    fun stop() {
        running = false
        runCatching { socket?.close() }
        thread?.join(1500)
    }

    private fun loop() {
        while (running) {
            try {
                Socket().use { s ->
                    s.connect(InetSocketAddress("127.0.0.1", port), 1500)
                    socket = s
                    decode(s.getInputStream())
                }
            } catch (_: IOException) {
                // The computer is not serving video yet, or the cable moved: retry below.
            }
            socket = null
            if (running) Thread.sleep(1000)
        }
    }

    private fun decode(input: InputStream) {
        val units = AccessUnits()
        val chunk = ByteArray(1 shl 16)
        var codec: MediaCodec? = null
        var drain: Thread? = null
        val draining = AtomicBoolean(true)
        var pts = 0L
        try {
            while (running) {
                val n = input.read(chunk)
                if (n < 0) break
                units.feed(chunk, n) { data, len ->
                    val c = codec ?: openCodec(data, len).also {
                        codec = it
                        drain = Thread({ drainOutput(it) { draining.get() } }, "ryoku-video-out").also(Thread::start)
                    }
                    val i = c.dequeueInputBuffer(100_000)
                    // No free buffer: the decoder is behind, so this frame is dropped and the picture
                    // stays damaged until the next keyframe (at most two seconds away).
                    if (i >= 0) {
                        c.getInputBuffer(i)!!.apply { clear(); put(data, 0, len) }
                        c.queueInputBuffer(i, 0, len, pts, 0)
                        pts += 33_333
                    }
                }
            }
        } finally {
            draining.set(false)
            drain?.join(500)
            codec?.let { runCatching { it.stop() }; it.release() }
        }
    }

    private fun drainOutput(codec: MediaCodec, active: () -> Boolean) {
        val info = MediaCodec.BufferInfo()
        try {
            while (active()) {
                val i = codec.dequeueOutputBuffer(info, 20_000)
                if (i >= 0) codec.releaseOutputBuffer(i, true) // true: show it now, never wait for a timestamp
            }
        } catch (_: IllegalStateException) {
            // The codec was stopped under us: that is how this thread is told to end.
        }
    }

    private fun openCodec(firstUnit: ByteArray, len: Int): MediaCodec {
        val format = MediaFormat.createVideoFormat(MediaFormat.MIMETYPE_VIDEO_AVC, width, height)
        for ((range, type) in nalUnits(firstUnit, len)) {
            val csd = when (type) { 7 -> "csd-0"; 8 -> "csd-1"; else -> continue }
            format.setByteBuffer(csd, ByteBuffer.wrap(byteArrayOf(0, 0, 0, 1) + firstUnit.copyOfRange(range.first, range.last + 1)))
        }
        format.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 1 shl 21)
        format.setInteger(MediaFormat.KEY_PRIORITY, 0) // real time
        if (Build.VERSION.SDK_INT >= 30) format.setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
        return MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_VIDEO_AVC).apply {
            configure(format, surface, null, 0)
            start()
        }
    }
}

/** NAL unit payload ranges (after the start code, trailing zero bytes trimmed) paired with their type. */
internal fun nalUnits(data: ByteArray, len: Int): List<Pair<IntRange, Int>> {
    val starts = ArrayList<Int>()
    var i = 0
    while (i + 3 <= len) {
        if (data[i].toInt() == 0 && data[i + 1].toInt() == 0 && data[i + 2].toInt() == 1) { starts.add(i + 3); i += 3 } else i++
    }
    return starts.mapIndexed { n, begin ->
        var end = if (n + 1 < starts.size) starts[n + 1] - 3 else len
        while (end > begin && data[end - 1].toInt() == 0) end--
        (begin until end) to (data[begin].toInt() and 0x1f)
    }.filter { !it.first.isEmpty() }
}

/**
 * Cuts the stream into access units: each starts at an AUD NAL (type 9), which is how the
 * computer's encoder is asked to mark frames. A unit is complete when the next AUD arrives, so
 * a frame is shown one frame-time after it is sent; the computer encodes at a constant rate so
 * that wait is bounded.
 */
internal class AccessUnits {
    private var data = ByteArray(1 shl 18)
    private var size = 0
    private var scanFrom = 0
    private var synced = false

    fun feed(chunk: ByteArray, n: Int, emit: (ByteArray, Int) -> Unit) {
        if (size + n > data.size) data = data.copyOf(maxOf(data.size * 2, size + n))
        System.arraycopy(chunk, 0, data, size, n)
        size += n
        if (!synced) {
            val at = findAud(0)
            if (at < 0) { if (size > 4) drop(size - 4); return } // keep a tail: an AUD may straddle two reads
            drop(at)
            synced = true
        }
        while (true) {
            val next = findAud(maxOf(scanFrom, 5))
            if (next < 0) { scanFrom = maxOf(size - 4, 5); return }
            emit(data, next)
            drop(next)
            scanFrom = 5
        }
    }

    private fun drop(n: Int) {
        System.arraycopy(data, n, data, 0, size - n)
        size -= n
    }

    /** Index where the next AUD start code (00 00 01 09, with its leading zero if any) begins. */
    private fun findAud(from: Int): Int {
        var i = from
        while (i + 4 <= size) {
            if (data[i].toInt() == 0 && data[i + 1].toInt() == 0 && data[i + 2].toInt() == 1 && (data[i + 3].toInt() and 0x1f) == 9) {
                return if (i > 0 && data[i - 1].toInt() == 0) i - 1 else i
            }
            i++
        }
        return -1
    }
}
