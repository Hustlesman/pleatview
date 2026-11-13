package com.hustlesman.pleatview

import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URLDecoder
import java.util.Collections
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.SynchronousQueue
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

class HttpRequest(
    val method: String,
    val path: String,
    val query: Map<String, String>,
    val headers: Map<String, String>,
    val body: ByteArray,
    val remote: InetAddress?,
) {
    fun header(name: String): String? = headers[name.lowercase()]

    fun bearerToken(): String? =
        header("authorization")?.takeIf { it.startsWith("Bearer ", ignoreCase = true) }?.substring(7)?.trim()

    fun jsonBody(): JSONObject {
        if (body.isEmpty()) return JSONObject()
        return try {
            JSONObject(String(body, Charsets.UTF_8))
        } catch (e: Exception) {
            throw HttpError(400, "body must be a JSON object")
        }
    }
}

class HttpResponse(
    val status: Int,
    val contentType: String? = null,
    val body: ByteArray = ByteArray(0),
    val headers: Map<String, String> = emptyMap(),
    /** When set, the response is streamed: headers are sent, then this runs until it returns. */
    val stream: ((OutputStream) -> Unit)? = null,
) {
    companion object {
        fun json(status: Int, json: JSONObject) =
            HttpResponse(status, "application/json; charset=utf-8", json.toString().toByteArray())

        fun json(status: Int, raw: String) =
            HttpResponse(status, "application/json; charset=utf-8", raw.toByteArray())

        fun error(status: Int, message: String, extra: Map<String, Any> = emptyMap()) =
            json(status, JSONObject().put("error", message).also { o -> extra.forEach { (k, v) -> o.put(k, v) } })

        fun html(bytes: ByteArray) = HttpResponse(200, "text/html; charset=utf-8", bytes)
    }
}

class HttpError(val status: Int, message: String) : Exception(message)

/**
 * A deliberately small HTTP/1.1 server: one request per connection, Content-Length
 * bodies only, and long-lived streamed responses for Server-Sent Events.
 */
class HttpServer(
    private val port: Int,
    private val handler: (HttpRequest) -> HttpResponse,
) {
    private val executor = ThreadPoolExecutor(
        2, MAX_THREADS, 30, TimeUnit.SECONDS, SynchronousQueue(),
    ) { r -> Thread(r, "pleatview-http-worker").apply { isDaemon = true } }
    private val clients: MutableSet<Socket> = Collections.newSetFromMap(ConcurrentHashMap())

    @Volatile
    private var serverSocket: ServerSocket? = null

    val localPort: Int get() = serverSocket?.localPort ?: -1

    @Throws(IOException::class)
    fun start() {
        val socket = ServerSocket()
        socket.reuseAddress = true
        socket.bind(InetSocketAddress(port), 50)
        serverSocket = socket
        thread(name = "pleatview-http", isDaemon = true) { acceptLoop(socket) }
    }

    fun stop() {
        runCatching { serverSocket?.close() }
        clients.forEach { runCatching { it.close() } }
        executor.shutdownNow()
    }

    private fun acceptLoop(socket: ServerSocket) {
        while (!socket.isClosed) {
            val client = try {
                socket.accept()
            } catch (e: IOException) {
                if (socket.isClosed) return else continue
            }
            try {
                executor.execute { serve(client) }
            } catch (e: RejectedExecutionException) {
                runCatching {
                    client.soTimeout = 2000
                    write(client.getOutputStream(), HttpResponse.error(503, "busy"), headOnly = false)
                }
                runCatching { client.close() }
            }
        }
    }

    private fun serve(client: Socket) {
        clients += client
        try {
            client.soTimeout = READ_TIMEOUT_MS
            client.tcpNoDelay = true
            val input = BufferedInputStream(client.getInputStream())
            val output = BufferedOutputStream(client.getOutputStream())
            val request = try {
                readRequest(input, output, client.inetAddress) ?: return
            } catch (e: HttpError) {
                write(output, HttpResponse.error(e.status, e.message ?: "bad request"), headOnly = false)
                return
            }
            val response = try {
                handler(request)
            } catch (e: HttpError) {
                HttpResponse.error(e.status, e.message ?: "error")
            } catch (e: Exception) {
                HttpResponse.error(500, e.message ?: e.javaClass.simpleName)
            }
            write(output, response, headOnly = request.method == "HEAD")
        } catch (_: IOException) {
            // Client went away.
        } finally {
            clients -= client
            runCatching { client.close() }
        }
    }

    private fun readRequest(input: InputStream, output: OutputStream, remote: InetAddress?): HttpRequest? {
        val requestLine = readLine(input) ?: return null
        val parts = requestLine.split(' ')
        if (parts.size != 3 || !parts[2].startsWith("HTTP/1.")) throw HttpError(400, "malformed request line")
        val headers = HashMap<String, String>()
        var headerBytes = 0
        while (true) {
            val line = readLine(input) ?: throw HttpError(400, "unexpected end of headers")
            if (line.isEmpty()) break
            headerBytes += line.length
            if (headerBytes > MAX_HEADER_BYTES || headers.size > 100) throw HttpError(431, "headers too large")
            val colon = line.indexOf(':')
            if (colon <= 0) throw HttpError(400, "malformed header")
            headers[line.substring(0, colon).trim().lowercase()] = line.substring(colon + 1).trim()
        }
        if (headers.containsKey("transfer-encoding")) throw HttpError(411, "Content-Length required")
        val length = headers["content-length"]?.let { it.toIntOrNull() ?: throw HttpError(400, "bad Content-Length") } ?: 0
        if (length < 0 || length > MAX_BODY_BYTES) throw HttpError(413, "body too large")
        if (length > 0 && headers["expect"].equals("100-continue", ignoreCase = true)) {
            output.write("HTTP/1.1 100 Continue\r\n\r\n".toByteArray())
            output.flush()
        }
        val body = ByteArray(length)
        var read = 0
        while (read < length) {
            val n = input.read(body, read, length - read)
            if (n < 0) throw HttpError(400, "truncated body")
            read += n
        }
        val target = parts[1]
        val path = decode(target.substringBefore('?'))
        val query = target.substringAfter('?', "").split('&').filter { it.isNotEmpty() }.associate {
            decode(it.substringBefore('=')) to decode(it.substringAfter('=', ""))
        }
        return HttpRequest(parts[0].uppercase(), path, query, headers, body, remote)
    }

    /** Reads one CRLF/LF-terminated line, or null on a clean end of stream. */
    private fun readLine(input: InputStream): String? {
        val buffer = ByteArrayOutputStream(128)
        while (true) {
            val b = input.read()
            if (b == -1) {
                if (buffer.size() == 0) return null
                throw HttpError(400, "unexpected end of stream")
            }
            if (b == '\n'.code) break
            if (b != '\r'.code) buffer.write(b)
            if (buffer.size() > MAX_LINE_BYTES) throw HttpError(431, "line too long")
        }
        return buffer.toString("UTF-8")
    }

    private fun decode(value: String): String =
        try {
            URLDecoder.decode(value.replace("+", "%2B"), "UTF-8")
        } catch (e: IllegalArgumentException) {
            throw HttpError(400, "bad percent-encoding")
        }

    private fun write(output: OutputStream, response: HttpResponse, headOnly: Boolean) {
        val head = StringBuilder()
        head.append("HTTP/1.1 ").append(response.status).append(' ').append(reason(response.status)).append("\r\n")
        response.contentType?.let { head.append("Content-Type: ").append(it).append("\r\n") }
        if (response.stream == null) head.append("Content-Length: ").append(response.body.size).append("\r\n")
        head.append("Cache-Control: no-store\r\n")
        head.append("X-Content-Type-Options: nosniff\r\n")
        head.append("Connection: close\r\n")
        response.headers.forEach { (k, v) -> head.append(k).append(": ").append(v).append("\r\n") }
        head.append("\r\n")
        output.write(head.toString().toByteArray(Charsets.UTF_8))
        if (!headOnly) {
            if (response.stream != null) {
                output.flush()
                response.stream.invoke(output)
            } else {
                output.write(response.body)
            }
        }
        output.flush()
    }

    private fun reason(status: Int) = when (status) {
        100 -> "Continue"
        200 -> "OK"
        204 -> "No Content"
        400 -> "Bad Request"
        401 -> "Unauthorized"
        403 -> "Forbidden"
        404 -> "Not Found"
        405 -> "Method Not Allowed"
        409 -> "Conflict"
        411 -> "Length Required"
        413 -> "Payload Too Large"
        421 -> "Misdirected Request"
        429 -> "Too Many Requests"
        431 -> "Request Header Fields Too Large"
        503 -> "Service Unavailable"
        else -> if (status >= 500) "Server Error" else "Status"
    }

    companion object {
        private const val MAX_THREADS = 24
        private const val READ_TIMEOUT_MS = 15_000
        private const val MAX_LINE_BYTES = 8 * 1024
        private const val MAX_HEADER_BYTES = 32 * 1024
        private const val MAX_BODY_BYTES = 256 * 1024
    }
}
