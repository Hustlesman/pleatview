package com.hustlesman.pleatview

/** Standard Base64 with padding, the same on every Android version and in unit tests. */
object B64 {
    private const val ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

    fun encode(data: ByteArray): String {
        val out = StringBuilder((data.size + 2) / 3 * 4)
        for (i in data.indices step 3) {
            val n = ((data[i].toInt() and 0xff) shl 16) or
                ((if (i + 1 < data.size) data[i + 1].toInt() and 0xff else 0) shl 8) or
                (if (i + 2 < data.size) data[i + 2].toInt() and 0xff else 0)
            out.append(ALPHABET[n shr 18 and 63]).append(ALPHABET[n shr 12 and 63])
            out.append(if (i + 1 < data.size) ALPHABET[n shr 6 and 63] else '=')
            out.append(if (i + 2 < data.size) ALPHABET[n and 63] else '=')
        }
        return out.toString()
    }

    /** @throws IllegalArgumentException for anything that isn't Base64. */
    fun decode(text: String): ByteArray {
        val clean = text.trim()
        require(clean.length % 4 == 0 && clean.isNotEmpty()) { "not Base64" }
        val out = java.io.ByteArrayOutputStream(clean.length / 4 * 3)
        for (i in clean.indices step 4) {
            val values = IntArray(4) { j ->
                val c = clean[i + j]
                if (c == '=') -1 else ALPHABET.indexOf(c).also { require(it >= 0) { "not Base64" } }
            }
            val n = (values[0] shl 18) or (values[1] shl 12) or ((values[2].coerceAtLeast(0)) shl 6) or values[3].coerceAtLeast(0)
            out.write(n shr 16 and 0xff)
            if (values[2] >= 0) out.write(n shr 8 and 0xff)
            if (values[3] >= 0) out.write(n and 0xff)
        }
        return out.toByteArray()
    }
}
