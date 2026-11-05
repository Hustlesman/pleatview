package com.hustlesman.pleatview

import android.content.Context
import android.content.res.Resources
import android.graphics.BitmapFactory
import android.graphics.Color
import android.graphics.ImageDecoder
import android.graphics.PixelFormat
import android.graphics.drawable.Animatable
import android.graphics.drawable.BitmapDrawable
import android.graphics.drawable.Drawable
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import android.view.ContextThemeWrapper
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import java.io.ByteArrayOutputStream
import java.net.HttpURLConnection
import java.net.Inet4Address
import java.net.NetworkInterface
import java.net.URL
import java.nio.ByteBuffer
import java.util.concurrent.Executors

/** Builds notification cards, used both inside the app and as system overlays. */
object ToastViews {
    fun create(context: Context, parent: ViewGroup?, notify: Command.Notify): View {
        val view = LayoutInflater.from(context).inflate(R.layout.item_toast, parent, false)
        view.layoutParams?.let { it.width = minOf(it.width, maxWidth(context)) }
        view.findViewById<TextView>(R.id.toast_title).apply {
            text = notify.title
            visibility = if (notify.title.isNullOrEmpty()) View.GONE else View.VISIBLE
        }
        view.findViewById<TextView>(R.id.toast_message).text = notify.message
        notify.imageUrl?.let { url ->
            val image = view.findViewById<ImageView>(R.id.toast_image)
            ImageLoader.load(url, image, maxSize = 960)
        }
        return view
    }

    /** The widest a card fits on this screen (narrower than 440 dp on a phone), with 24 dp margins. */
    fun maxWidth(context: Context): Int {
        val resources = context.resources
        return ((resources.configuration.screenWidthDp - 48) * resources.displayMetrics.density).toInt()
    }

    fun gravity(position: Command.Position): Int = when (position) {
        Command.Position.TOP_LEFT -> Gravity.TOP or Gravity.START
        Command.Position.TOP_RIGHT -> Gravity.TOP or Gravity.END
        Command.Position.BOTTOM_LEFT -> Gravity.BOTTOM or Gravity.START
        Command.Position.BOTTOM_RIGHT -> Gravity.BOTTOM or Gravity.END
        Command.Position.CENTER -> Gravity.CENTER
    }
}

/** In-app notification stacks, one per screen position. */
class Toasts(private val layer: FrameLayout) {
    private val stacks = HashMap<Command.Position, LinearLayout>()

    fun show(notify: Command.Notify) {
        val stack = stacks.getOrPut(notify.position) { createStack(notify.position) }
        val view = ToastViews.create(layer.context, stack, notify)
        val atTop = notify.position == Command.Position.TOP_LEFT || notify.position == Command.Position.TOP_RIGHT
        stack.addView(view, if (atTop) stack.childCount else 0)
        while (stack.childCount > MAX_VISIBLE) stack.removeViewAt(if (atTop) 0 else stack.childCount - 1)
        view.alpha = 0f
        view.animate().alpha(1f).setDuration(180).start()
        view.postDelayed({
            view.animate().alpha(0f).setDuration(250).withEndAction { stack.removeView(view) }.start()
        }, notify.duration * 1000L)
    }

    fun clear() = stacks.values.forEach { it.removeAllViews() }

    private fun createStack(position: Command.Position): LinearLayout {
        val margin = (24 * layer.resources.displayMetrics.density).toInt()
        val stack = LinearLayout(layer.context).apply { orientation = LinearLayout.VERTICAL }
        val params = FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT, ToastViews.gravity(position),
        ).apply { setMargins(margin, margin, margin, margin) }
        layer.addView(stack, params)
        return stack
    }

    companion object {
        private const val MAX_VISIBLE = 3
    }
}

/**
 * Shows notifications on top of other apps while PleatView is in the background.
 * Requires the "Display over other apps" permission, which Android TV only grants
 * through adb or a settings screen on some devices.
 */
class SystemOverlay(private val context: Context) {
    private val main = Handler(Looper.getMainLooper())

    fun isAllowed(): Boolean = Settings.canDrawOverlays(context)

    fun show(notify: Command.Notify): Boolean {
        if (!isAllowed()) return false
        val windows = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val themed = ContextThemeWrapper(context, R.style.Theme_PleatView)
        val view = ToastViews.create(themed, null, notify)
        val density = context.resources.displayMetrics.density
        @Suppress("DEPRECATION")
        val type = if (Build.VERSION.SDK_INT >= 26) {
            WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        } else {
            WindowManager.LayoutParams.TYPE_PHONE
        }
        val params = WindowManager.LayoutParams(
            minOf((440 * density).toInt(), ToastViews.maxWidth(context)),
            WindowManager.LayoutParams.WRAP_CONTENT,
            type,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE or WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = ToastViews.gravity(notify.position)
            x = (24 * density).toInt()
            y = (24 * density).toInt()
        }
        return try {
            windows.addView(view, params)
            main.postDelayed({ runCatching { windows.removeView(view) } }, notify.duration * 1000L)
            true
        } catch (e: Exception) {
            Log.w("PleatView", "Could not show overlay notification", e)
            false
        }
    }
}

/** Positions a layer full-screen or as a picture-in-picture box. */
object Layouts {
    fun apply(view: View, layout: Command.Layout) {
        val params = view.layoutParams as FrameLayout.LayoutParams
        if (layout.mode == Command.Mode.PIP) {
            val metrics = view.resources.displayMetrics
            val parentWidth = (view.parent as? View)?.width?.takeIf { it > 0 } ?: metrics.widthPixels
            val margin = (24 * metrics.density).toInt()
            params.width = parentWidth * layout.size / 100
            params.height = params.width * 9 / 16
            params.gravity = ToastViews.gravity(layout.position)
            params.setMargins(margin, margin, margin, margin)
            view.setBackgroundResource(R.drawable.pip_frame)
            view.setPadding(2, 2, 2, 2)
        } else {
            params.width = FrameLayout.LayoutParams.MATCH_PARENT
            params.height = FrameLayout.LayoutParams.MATCH_PARENT
            params.gravity = Gravity.NO_GRAVITY
            params.setMargins(0, 0, 0, 0)
            // No frame: on a portrait phone the video's black bars would show it.
            view.setBackgroundColor(Color.BLACK)
            view.setPadding(0, 0, 0, 0)
        }
        view.layoutParams = params
    }
}

/** Minimal image loader for notification pictures and cover art. GIFs animate on Android 10 and later. */
object ImageLoader {
    private val executor = Executors.newFixedThreadPool(2)
    private val main = Handler(Looper.getMainLooper())

    fun load(url: String, target: ImageView, maxSize: Int) {
        target.tag = url
        val resources = target.resources
        executor.execute {
            val image = try {
                fetch(url, maxSize, resources)
            } catch (e: Exception) {
                Log.w("PleatView", "Image load failed: ${e.message}")
                null
            }
            main.post {
                if (target.tag == url && image != null) {
                    (target.drawable as? Animatable)?.stop()
                    target.setImageDrawable(image)
                    if (image is Animatable) {
                        image.start()
                        target.addOnAttachStateChangeListener(object : View.OnAttachStateChangeListener {
                            override fun onViewAttachedToWindow(view: View) = Unit
                            override fun onViewDetachedFromWindow(view: View) {
                                image.stop() // the notification closed
                                view.removeOnAttachStateChangeListener(this)
                            }
                        })
                    }
                    target.visibility = View.VISIBLE
                }
            }
        }
    }

    private fun fetch(url: String, maxSize: Int, resources: Resources): Drawable? {
        val connection = URL(url).openConnection() as HttpURLConnection
        val bytes = try {
            connection.connectTimeout = 8_000
            connection.readTimeout = 10_000
            if (connection.responseCode !in 200..299) return null
            connection.inputStream.use { input ->
                val out = ByteArrayOutputStream()
                val buffer = ByteArray(16 * 1024)
                while (true) {
                    val n = input.read(buffer)
                    if (n < 0) break
                    out.write(buffer, 0, n)
                    if (out.size() > MAX_BYTES) return null
                }
                out.toByteArray()
            }
        } finally {
            connection.disconnect()
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            // Animated GIF and WebP come back as an AnimatedImageDrawable. Android 9's animated
            // decoder crashes natively on some GIFs (seen on Fire OS 7), so it gets the first frame.
            return ImageDecoder.decodeDrawable(ImageDecoder.createSource(ByteBuffer.wrap(bytes))) { decoder, info, _ ->
                decoder.setTargetSampleSize(sampleSize(info.size.width, info.size.height, maxSize))
            }
        }
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        val options = BitmapFactory.Options().apply { inSampleSize = sampleSize(bounds.outWidth, bounds.outHeight, maxSize) }
        val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options) ?: return null
        return BitmapDrawable(resources, bitmap)
    }

    private fun sampleSize(width: Int, height: Int, maxSize: Int): Int {
        var sample = 1
        while (width / (sample * 2) >= maxSize || height / (sample * 2) >= maxSize) sample *= 2
        return sample
    }

    private const val MAX_BYTES = 15 * 1024 * 1024
}

object NetUtil {
    /** Best guess at the LAN address Home Assistant can reach us on. */
    fun localIpv4(): String? = try {
        NetworkInterface.getNetworkInterfaces().toList()
            .filter { it.isUp && !it.isLoopback && !it.isVirtual }
            .sortedBy { if (it.name.startsWith("eth")) 0 else if (it.name.startsWith("wlan")) 1 else 2 }
            .flatMap { it.inetAddresses.toList() }
            .filterIsInstance<Inet4Address>()
            .firstOrNull { !it.isLinkLocalAddress && !it.isLoopbackAddress }
            ?.hostAddress
    } catch (e: Exception) {
        null
    }
}
