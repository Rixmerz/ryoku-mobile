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
import android.content.Context
import java.io.File
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.security.MessageDigest
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.X509TrustManager

data class DesktopWindow(val cls: String, val title: String)
/** [busy]: a PC monitor is showing it, so the live view mirrors that monitor instead of moving the desktop (see PhoneView in ryoku_link.py). */
data class Workspace(val id: Int, val name: String, val active: Boolean, val busy: Boolean, val windows: List<DesktopWindow>)

/** The computer's answer to a cast: [workspace] when it opened, [error] when it did not. */
data class CastReply(val pkg: String, val workspace: Int?, val error: String?)

/** A live view the computer agreed to serve: the stream's size and the port its video is on. */
data class LiveInfo(val workspace: Int, val width: Int, val height: Int, val port: Int)

/**
 * Link to the Ryoku desktop (pc/ryoku_link.py). Over USB, `adb reverse` maps this phone's
 * 127.0.0.1:7466 to the computer, so the app only ever dials localhost and never needs to know
 * the computer's address. It retries quietly while nothing answers; the UI shows the desktop
 * page only while connected.
 *
 * Without the cable it goes over Wi-Fi to pc/ryoku_link_wifi.py: found by a UDP broadcast, reached
 * over TLS pinned to the certificate fingerprint, and authenticated with a token. Both secrets are
 * written into files/link.json by the PC through adb while the cable is in, so plugging it in once
 * is the pairing and nothing secret ever crosses the air.
 */
class DesktopLink(private val port: Int = 7466) {
    var connected by mutableStateOf(false); private set
    /** The computer's address on Wi-Fi; null while the link runs over the USB cable. */
    var wifiHost by mutableStateOf<String?>(null); private set
    var host by mutableStateOf(""); private set
    var workspaces by mutableStateOf<List<Workspace>>(emptyList()); private set
    var live by mutableStateOf<LiveInfo?>(null); private set
    /** True when the computer could not open the last view it was asked for. */
    var openFailed by mutableStateOf(false); private set
    /** The computer's last answer to [cast]: the PC workspace it opened on, or why it could not. */
    var castReply by mutableStateOf<CastReply?>(null); private set

    private val io = Executors.newSingleThreadExecutor()
    private val sender = Executors.newSingleThreadExecutor()
    @Volatile private var socket: Socket? = null
    @Volatile private var running = false

    private var files: File? = null

    fun start(context: Context) {
        if (running) return
        files = context.filesDir
        running = true
        io.execute { loop() }
    }

    fun stop() {
        running = false
        runCatching { socket?.close() }
        // A link made for one request (see CastService) must not leave its two threads behind.
        io.shutdown()
        sender.shutdown()
    }

    /** Switches the computer's own monitor; the live view never does that. */
    fun switchTo(id: Int) = send(JSONObject().put("cmd", "workspace").put("id", id))

    /** Asks for workspace [id] on the phone's headless output, in the aspect of this screen (the computer picks the exact size). */
    fun openView(id: Int, width: Int, height: Int) {
        openFailed = false
        send(JSONObject().put("cmd", "view").put("id", id).put("width", width).put("height", height))
    }

    /** Sends the app [pkg], running on this phone, to a new workspace of the computer; the answer lands in [castReply]. */
    fun cast(pkg: String) {
        castReply = null
        send(JSONObject().put("cmd", "cast").put("package", pkg))
    }

    fun closeView() {
        live = null
        send(JSONObject().put("cmd", "unview"))
    }

    /** [x] and [y] are fractions of the view, so the computer never needs this screen's size. */
    fun pointer(action: String, x: Float, y: Float, button: String = "left") =
        send(JSONObject().put("cmd", "pointer").put("action", action).put("x", x.toDouble()).put("y", y.toDouble()).put("button", button))

    /** Scrolls by wheel lines; positive [dy] moves the content up, like a wheel turned down. */
    fun scroll(dx: Float, dy: Float, end: Boolean = false) =
        send(JSONObject().put("cmd", "scroll").put("dx", dx.toDouble()).put("dy", dy.toDouble()).put("end", end))

    /** Zooms the computer's screen by [factor] (1 = off) about ([x], [y]), fractions of the viewed output. */
    fun zoom(factor: Float, x: Float, y: Float) =
        send(JSONObject().put("cmd", "zoom").put("factor", factor.toDouble()).put("x", x.toDouble()).put("y", y.toDouble()))

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
                connectAny().use { s ->
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

    /** Cable first, since it needs nothing; otherwise the paired computer on Wi-Fi. */
    private fun connectAny(): Socket {
        runCatching { return dial(port, null).also { wifiHost = null } }
        // Discovery first; then the addresses the PC had when last on the cable, in case
        // broadcasts are filtered on this network.
        val known = runCatching { JSONObject(File(files, "link.json").readText()).optJSONArray("hosts") }.getOrNull()
        val candidates = listOfNotNull(discover(), lastHost) + List(known?.length() ?: 0) { known!!.getString(it) }
        for (host in candidates.distinct()) {
            runCatching { return dial(port, host).also { wifiHost = host; lastHost = host } }
        }
        throw java.io.IOException("no computer found")
    }

    @Volatile private var lastHost: String? = null

    /**
     * Opens [port] on the computer the control link uses right now, so the live video follows the
     * same path: plain localhost over USB, pinned TLS plus the token over Wi-Fi.
     */
    fun dialVideo(port: Int): Socket = dial(port, wifiHost)

    private fun dial(port: Int, host: String?): Socket {
        if (host == null) return Socket().apply { connect(InetSocketAddress("127.0.0.1", port), 1500) }
        val pair = pairing() ?: throw java.io.IOException("not paired: plug the cable in once")
        val raw = Socket().apply { connect(InetSocketAddress(host, port), 2500) }
        val tls = pinnedContext(pair.second).socketFactory.createSocket(raw, host, port, true) as SSLSocket
        tls.startHandshake()
        tls.outputStream.apply { write((JSONObject().put("auth", pair.first).toString() + "\n").toByteArray()); flush() }
        return tls
    }

    /** (token, certificate sha256), as the PC left them in files/link.json. */
    private fun pairing(): Pair<String, String>? = runCatching {
        val o = JSONObject(File(files, "link.json").readText())
        o.getString("token") to o.getString("fingerprint").lowercase()
    }.getOrNull()

    /** Asks the local network where ryoku-link is; the first answer wins. */
    private fun discover(): String? {
        if (pairing() == null) return null
        return runCatching {
            DatagramSocket().use { d ->
                d.broadcast = true
                d.soTimeout = 1200
                val ask = "ryoku-link?\n".toByteArray()
                // Each interface's own broadcast too: when the phone is the hotspot the PC is on,
                // 255.255.255.255 leaves through mobile data and never reaches the hotspot's clients.
                val targets = java.net.NetworkInterface.getNetworkInterfaces().toList()
                    .filter { it.isUp && !it.isLoopback }
                    .flatMap { it.interfaceAddresses.mapNotNull { a -> a.broadcast } } + InetAddress.getByName("255.255.255.255")
                targets.distinct().forEach { runCatching { d.send(DatagramPacket(ask, ask.size, it, DISCOVERY_PORT)) } }
                val buf = ByteArray(256)
                val p = DatagramPacket(buf, buf.size)
                d.receive(p)
                p.address.hostAddress
            }
        }.getOrNull()
    }

    /** Trusts exactly one certificate, the PC's; no CA and no host name are involved. */
    private fun pinnedContext(fingerprint: String): SSLContext {
        val tm = object : X509TrustManager {
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
            override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) =
                throw java.security.cert.CertificateException("server only")
            override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
                val got = MessageDigest.getInstance("SHA-256").digest(chain[0].encoded).joinToString("") { "%02x".format(it) }
                if (got != fingerprint) throw java.security.cert.CertificateException("not the paired computer")
            }
        }
        return SSLContext.getInstance("TLS").apply { init(null, arrayOf(tm), null) }
    }

    private companion object { const val DISCOVERY_PORT = 7470 }

    private fun parse(line: String) {
        val o = runCatching { JSONObject(line) }.getOrNull() ?: return
        when (o.optString("type")) {
            "state" -> parseState(o)
            "view" -> parseView(o)
            "cast" -> castReply = CastReply(o.optString("package"), if (o.has("workspace")) o.getInt("workspace") else null, o.optString("error").ifEmpty { null })
        }
    }

    private fun parseView(o: JSONObject) {
        if (o.has("error")) {
            openFailed = true
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
