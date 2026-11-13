package com.hustlesman.pleatview

import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.DataInputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.net.URI
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.X509Certificate
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.X509TrustManager

/**
 * A small WebSocket client (RFC 6455) for protocol v2 (docs/protocol-v2.md): JSON in text
 * frames, pings answered, messages up to 1 MiB, over TLS that either the system's authorities
 * or a pinned certificate vouch for. Blocking: one thread reads, any thread may send.
 */
class WebSocketClient private constructor(private val socket: Socket, rest: ByteArray) {
    private val input = DataInputStream(SequenceInput(rest, BufferedInputStream(socket.getInputStream())))
    private val output: OutputStream = socket.getOutputStream()

    @Volatile
    var closeCode: Int? = null
        private set

    class Closed(val code: Int?) : IOException("closed ($code)")

    @Synchronized
    fun send(message: JSONObject) {
        output.write(encode(TEXT, message.toString().toByteArray(), mask = true))
        output.flush()
    }

    /** The next JSON object; throws [Closed] when the connection ends. */
    fun receive(): JSONObject {
        val parts = java.io.ByteArrayOutputStream()
        while (true) {
            val frame = try {
                readFrame(input)
            } catch (e: IOException) {
                throw Closed(closeCode)
            }
            when (frame.opcode) {
                PING -> synchronized(this) {
                    output.write(encode(PONG, frame.payload, mask = true))
                    output.flush()
                }
                PONG -> Unit
                CLOSE -> {
                    closeCode = if (frame.payload.size >= 2) ((frame.payload[0].toInt() and 0xff) shl 8) or (frame.payload[1].toInt() and 0xff) else null
                    close(closeCode ?: 1000)
                    throw Closed(closeCode)
                }
                TEXT, BINARY, CONTINUATION -> {
                    parts.write(frame.payload)
                    if (parts.size() > MAX_MESSAGE) throw Closed(1009)
                    if (frame.final) return JSONObject(String(parts.toByteArray(), Charsets.UTF_8))
                }
                else -> throw Closed(1002)
            }
        }
    }

    fun close(code: Int = 1000) {
        try {
            synchronized(this) {
                output.write(encode(CLOSE, byteArrayOf((code shr 8).toByte(), code.toByte()), mask = true))
                output.flush()
            }
        } catch (_: IOException) {
            // gone already
        }
        try {
            socket.close()
        } catch (_: IOException) {
        }
    }

    class Frame(val final: Boolean, val opcode: Int, val payload: ByteArray)

    companion object {
        const val TEXT = 0x1
        const val BINARY = 0x2
        const val CONTINUATION = 0x0
        const val CLOSE = 0x8
        const val PING = 0x9
        const val PONG = 0xA
        const val MAX_MESSAGE = 1 shl 20
        private const val GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        private val random = SecureRandom()

        fun acceptKey(key: String): String = B64.encode(MessageDigest.getInstance("SHA-1").digest((key + GUID).toByteArray()))

        /** One final frame. Clients mask what they send; servers don't. */
        fun encode(opcode: Int, payload: ByteArray, mask: Boolean): ByteArray {
            val out = java.io.ByteArrayOutputStream()
            out.write(0x80 or opcode)
            val flag = if (mask) 0x80 else 0
            when {
                payload.size < 126 -> out.write(flag or payload.size)
                payload.size < 65536 -> {
                    out.write(flag or 126)
                    out.write(payload.size shr 8)
                    out.write(payload.size and 0xff)
                }
                else -> {
                    out.write(flag or 127)
                    for (shift in 56 downTo 0 step 8) out.write(((payload.size.toLong() shr shift) and 0xff).toInt())
                }
            }
            if (!mask) {
                out.write(payload)
                return out.toByteArray()
            }
            val key = ByteArray(4).also(random::nextBytes)
            out.write(key)
            out.write(ByteArray(payload.size) { (payload[it].toInt() xor key[it % 4].toInt()).toByte() })
            return out.toByteArray()
        }

        fun readFrame(input: DataInputStream): Frame {
            val first = input.readUnsignedByte()
            val second = input.readUnsignedByte()
            var length = (second and 0x7f).toLong()
            if (length == 126L) length = input.readUnsignedShort().toLong()
            else if (length == 127L) length = input.readLong()
            if (length < 0 || length > MAX_MESSAGE) throw IOException("message too big")
            val key = if (second and 0x80 != 0) ByteArray(4).also(input::readFully) else null
            val payload = ByteArray(length.toInt()).also(input::readFully)
            if (key != null) for (i in payload.indices) payload[i] = (payload[i].toInt() xor key[i % 4].toInt()).toByte()
            return Frame(first and 0x80 != 0, first and 0x0f, payload)
        }

        /**
         * Opens a wss:// connection. With [pin] (hex of the SHA-256 of the server's certificate),
         * only that certificate is accepted; otherwise the system's authorities and the host
         * name decide.
         */
        fun connect(url: String, subprotocol: String, pin: String?, timeoutMs: Int = 15_000): WebSocketClient {
            val uri = URI(url)
            require(uri.scheme == "wss" && !uri.host.isNullOrEmpty()) { "only wss:// addresses" }
            val host = uri.host.trim('[', ']')
            val port = if (uri.port > 0) uri.port else 443
            val context = SSLContext.getInstance("TLS")
            context.init(null, if (pin != null) arrayOf(PinnedTrust(pin)) else null, null)
            val socket = Socket()
            try {
                socket.connect(InetSocketAddress(host, port), timeoutMs)
                socket.soTimeout = timeoutMs
                val ssl = context.socketFactory.createSocket(socket, host, port, true) as SSLSocket
                ssl.startHandshake()
                if (pin == null && !HttpsURLConnection.getDefaultHostnameVerifier().verify(host, ssl.session)) {
                    throw IOException("the server's certificate isn't for $host")
                }
                return open(ssl, uri, subprotocol)
            } catch (e: Exception) {
                socket.close()
                throw e
            }
        }

        /** The HTTP upgrade on a connected socket; then reads block without a time limit. */
        internal fun open(socket: Socket, uri: URI, subprotocol: String): WebSocketClient {
            val key = B64.encode(ByteArray(16).also(random::nextBytes))
            val hostHeader = uri.rawAuthority
            socket.getOutputStream().apply {
                write(("GET ${uri.rawPath.ifEmpty { "/" }} HTTP/1.1\r\nHost: $hostHeader\r\nUpgrade: websocket\r\n" +
                    "Connection: Upgrade\r\nSec-WebSocket-Key: $key\r\nSec-WebSocket-Version: 13\r\n" +
                    "Sec-WebSocket-Protocol: $subprotocol\r\n\r\n").toByteArray(Charsets.ISO_8859_1))
                flush()
            }
            val raw = socket.getInputStream()
            val head = java.io.ByteArrayOutputStream()
            while (!head.toByteArray().endsWith("\r\n\r\n")) {
                val byte = raw.read()
                if (byte < 0 || head.size() > 16384) throw IOException("not a WebSocket")
                head.write(byte)
            }
            val lines = String(head.toByteArray(), Charsets.ISO_8859_1).split("\r\n")
            val headers = lines.drop(1).filter { ':' in it }
                .associate { it.substringBefore(':').trim().lowercase() to it.substringAfter(':').trim() }
            if (!lines[0].startsWith("HTTP/1.1 101") || headers["sec-websocket-accept"] != acceptKey(key)) {
                throw IOException("not a WebSocket: ${lines[0]}")
            }
            if (headers["sec-websocket-protocol"] != subprotocol) throw IOException("the server doesn't speak $subprotocol")
            socket.soTimeout = 0
            return WebSocketClient(socket, ByteArray(0))
        }

        private fun ByteArray.endsWith(text: String): Boolean {
            val tail = text.toByteArray()
            return size >= tail.size && copyOfRange(size - tail.size, size).contentEquals(tail)
        }
    }

    /** Accepts only the pinned certificate, whoever signed it. */
    private class PinnedTrust(pin: String) : X509TrustManager {
        private val pin = pin.lowercase()
        override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) = throw java.security.cert.CertificateException("not for clients")
        override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
            val leaf = chain.firstOrNull() ?: throw java.security.cert.CertificateException("no certificate")
            val hash = MessageDigest.getInstance("SHA-256").digest(leaf.encoded).joinToString("") { "%02x".format(it) }
            if (hash != pin) throw java.security.cert.CertificateException("the server's certificate isn't the one pinned")
        }
        override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
    }

    /** What was read past the HTTP head, then the socket. */
    private class SequenceInput(private val first: ByteArray, private val rest: InputStream) : InputStream() {
        private var at = 0
        override fun read(): Int = if (at < first.size) first[at++].toInt() and 0xff else rest.read()
        override fun read(b: ByteArray, off: Int, len: Int): Int {
            if (at < first.size) {
                val n = minOf(len, first.size - at)
                System.arraycopy(first, at, b, off, n)
                at += n
                return n
            }
            return rest.read(b, off, len)
        }
    }
}
