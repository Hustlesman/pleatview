package com.hustlesman.pleatview

import android.graphics.Bitmap
import android.graphics.Color
import android.net.Uri
import androidx.core.graphics.scale
import io.nayuki.qrcodegen.QrCode

/**
 * The QR code on the setup screen. It opens this TV's phone setup page with the PIN
 * filled in, so nothing has to be typed on the phone. The PIN travels in the URL
 * fragment, which browsers never send over the network.
 */
object SetupCode {
    fun url(ip: String, port: Int, pin: String): String = "http://$ip:$port/#pin=$pin"

    /**
     * The phone page's sign-in form for a dashboard, which shows the [address] the password
     * goes to. The PIN stands in for pairing.
     */
    fun signInUrl(ip: String, port: Int, pin: String, title: String, address: String): String =
        "http://$ip:$port/#signin=${Uri.encode(title)}&to=${Uri.encode(address)}&pin=$pin"

    /** Dark modules of the code, with a quiet zone of [border] modules, row by row. */
    fun modules(text: String, border: Int = 2): Array<BooleanArray> {
        val qr = QrCode.encodeText(text, QrCode.Ecc.MEDIUM)
        val size = qr.size + border * 2
        return Array(size) { y -> BooleanArray(size) { x -> qr.getModule(x - border, y - border) } }
    }

    /** A crisp bitmap about [sizePx] square: whole pixels per module, no smoothing. */
    fun bitmap(text: String, sizePx: Int): Bitmap {
        val rows = modules(text)
        val n = rows.size
        val pixels = IntArray(n * n) { i -> if (rows[i / n][i % n]) Color.BLACK else Color.WHITE }
        val scale = (sizePx / n).coerceAtLeast(1)
        return Bitmap.createBitmap(pixels, n, n, Bitmap.Config.ARGB_8888).scale(n * scale, n * scale, filter = false)
    }
}
