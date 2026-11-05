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
data class Workspace(val id: Int, val name: String, val active: Boolean, val windows: List<DesktopWindow>)

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

    fun switchTo(id: Int) = send(JSONObject().put("cmd", "workspace").put("id", id))

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
            if (running) Thread.sleep(3000)
        }
    }

    private fun parse(line: String) {
        val o = runCatching { JSONObject(line) }.getOrNull() ?: return
        if (o.optString("type") != "state") return
        val arr = o.optJSONArray("workspaces") ?: return
        workspaces = List(arr.length()) { i ->
            val w = arr.getJSONObject(i)
            val wins = w.optJSONArray("windows")
            Workspace(w.getInt("id"), w.optString("name"), w.optBoolean("active"),
                List(wins?.length() ?: 0) { j -> wins!!.getJSONObject(j).let { DesktopWindow(it.optString("class"), it.optString("title")) } })
        }
        host = o.optString("host")
        connected = true
    }
}
