package com.rixmerz.ryokumobile

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.Executors

data class DesktopWindow(val cls: String, val title: String)
/** [busy]: the PC monitor is showing it, so the live view cannot (see PhoneView in ryoku_link.py). */
data class Workspace(val id: Int, val name: String, val active: Boolean, val busy: Boolean, val windows: List<DesktopWindow>)

/** A live view the computer agreed to serve: the stream's size and the port its video is on. */
data class LiveInfo(val workspace: Int, val width: Int, val height: Int, val port: Int)

/**
 * Link to the Ryoku desktop (pc/ryoku_link.py). Over USB, `adb reverse` maps this phone's
 * 127.0.0.1:7466 to the computer, so the app only ever dials localhost and never needs to know
 * the computer's address. It retries quietly while nothing answers; the UI shows the desktop
 * page only while connected.
 */
class DesktopLink(private val port: Int = 7466) {
    var connected by mutableStateOf(false); private set
    var host by mutableStateOf(""); private set
    var workspaces by mutableStateOf<List<Workspace>>(emptyList()); private set
    var live by mutableStateOf<LiveInfo?>(null); private set
    /** The workspace the computer last refused to show because its monitor is on it. */
    var refused by mutableStateOf<Int?>(null); private set
    /** True when the computer could not open the last view it was asked for. */
    var openFailed by mutableStateOf(false); private set

    private val io = Executors.newSingleThreadExecutor()
    private val sender = Executors.newSingleThreadExecutor()
    @Volatile private var socket: Socket? = null
    @Volatile private var running = false

    fun start() {
        if (running) return
        running = true
        io.execute { loop() }
    }

    fun stop() {
        running = false
        runCatching { socket?.close() }
    }

    /** Switches the computer's own monitor; the live view never does that. */
    fun switchTo(id: Int) = send(JSONObject().put("cmd", "workspace").put("id", id))

    /** Asks for workspace [id] on the phone's headless output, in the aspect of this screen (the computer picks the exact size). */
    fun openView(id: Int, width: Int, height: Int) {
        openFailed = false
        send(JSONObject().put("cmd", "view").put("id", id).put("width", width).put("height", height))
    }

    fun closeView() {
        live = null
        send(JSONObject().put("cmd", "unview"))
    }

    fun clearRefusal() { refused = null }

    /** [x] and [y] are fractions of the view, so the computer never needs this screen's size. */
    fun pointer(action: String, x: Float, y: Float, button: String = "left") =
        send(JSONObject().put("cmd", "pointer").put("action", action).put("x", x.toDouble()).put("y", y.toDouble()).put("button", button))

    /** Scrolls by wheel lines; positive [dy] moves the content up, like a wheel turned down. */
    fun scroll(dx: Float, dy: Float, end: Boolean = false) =
        send(JSONObject().put("cmd", "scroll").put("dx", dx.toDouble()).put("dy", dy.toDouble()).put("end", end))

    fun typeText(text: String) = send(JSONObject().put("cmd", "key").put("text", text))

    fun pressKey(name: String) = send(JSONObject().put("cmd", "key").put("key", name))

    private fun send(msg: JSONObject) = sender.execute {
        runCatching {
            val s = socket ?: return@runCatching
            OutputStreamWriter(s.getOutputStream()).apply { write(msg.toString() + "\n"); flush() }
        }
    }

    private fun loop() {
        while (running) {
            try {
                Socket().use { s ->
                    s.connect(InetSocketAddress("127.0.0.1", port), 1500)
                    socket = s
                    val reader = BufferedReader(InputStreamReader(s.getInputStream()))
                    while (running) {
                        val line = reader.readLine() ?: break
                        parse(line)
                    }
                }
            } catch (_: Exception) {
                // Nothing listening yet (no cable, no daemon): expected, retry below.
            }
            socket = null
            connected = false
            live = null
            if (running) Thread.sleep(3000)
        }
    }

    private fun parse(line: String) {
        val o = runCatching { JSONObject(line) }.getOrNull() ?: return
        when (o.optString("type")) {
            "state" -> parseState(o)
            "view" -> parseView(o)
        }
    }

    private fun parseView(o: JSONObject) {
        if (o.has("error")) {
            openFailed = true
        } else if (o.optBoolean("busy")) {
            refused = o.optInt("workspace")
        } else if (o.has("port")) {
            live = LiveInfo(o.getInt("workspace"), o.getInt("width"), o.getInt("height"), o.getInt("port"))
        }
    }

    private fun parseState(o: JSONObject) {
        val arr = o.optJSONArray("workspaces") ?: return
        workspaces = List(arr.length()) { i ->
            val w = arr.getJSONObject(i)
            val wins = w.optJSONArray("windows")
            Workspace(w.getInt("id"), w.optString("name"), w.optBoolean("active"), w.optBoolean("busy"),
                List(wins?.length() ?: 0) { j -> wins!!.getJSONObject(j).let { DesktopWindow(it.optString("class"), it.optString("title")) } })
        }
        host = o.optString("host")
        connected = true
    }
}
