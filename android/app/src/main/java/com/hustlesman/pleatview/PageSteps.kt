package com.hustlesman.pleatview

import android.content.Context
import android.os.Build
import android.os.Handler
import android.os.SystemClock
import android.util.Log
import android.view.InputDevice
import android.view.MotionEvent
import android.view.ViewConfiguration
import android.webkit.WebView
import org.json.JSONArray
import org.json.JSONObject
import java.net.URLDecoder

/**
 * Steps taken on a page after it loads, written in its address after `#pleatview=`: waits,
 * clicks (on text, a CSS selector or a place), drags and scrolls, for cookie banners, pop-ups
 * and maps with nothing in their address. The same steps as the renderer's (actions.py) and
 * the Linux app's (steps.js); tests/fixtures/page-steps.json holds examples all three read.
 * Presses are real touch and mouse events on the WebView, which pages can't tell from a hand.
 */
object PageSteps {
    const val PARAM = "pleatview="
    const val MAX_STEPS = 30
    const val MAX_WAIT = 60.0

    @Volatile private var finder: String? = null

    /** The script that finds a click's target on the page (assets/find.js, the renderer's too), read once. */
    fun finder(context: Context): String =
        finder ?: context.assets.open("find.js").bufferedReader().use { it.readText() }.also { finder = it }

    data class Step(val kind: String, val text: String = "", val numbers: List<Double> = emptyList(), val css: String = "")

    /** The address to open, without its steps, and the steps; IllegalArgumentException for wrong ones. */
    fun split(url: String): Pair<String, List<Step>> {
        val at = url.indexOf('#')
        if (at < 0) return url to emptyList()
        val parts = url.substring(at + 1).split("&")
        val mine = parts.filter { it.startsWith(PARAM) }
        if (mine.isEmpty()) return url to emptyList()
        val rest = parts.filterNot { it.startsWith(PARAM) }.joinToString("&")
        // %XX escapes decoded as the renderer and the Linux app do: a '+' stays a '+'.
        val steps = parse(URLDecoder.decode(mine[0].substring(PARAM.length).replace("+", "%2B"), "UTF-8"))
        return url.substring(0, at) + (if (rest.isNotEmpty()) "#$rest" else "") to steps
    }

    fun parse(text: String): List<Step> {
        val steps = mutableListOf<Step>()
        for (raw in text.split(";")) {
            if (raw.isBlank()) continue
            val name = raw.substringBefore(':').trim().lowercase()
            val arg = if (':' in raw) raw.substringAfter(':').trim() else ""
            when (name) {
                "wait" -> steps += Step("wait", numbers = numbers(arg, 1, 0.0, MAX_WAIT, "wait takes seconds from 0 to 60"))
                "click" -> steps += when {
                    arg.lowercase().startsWith("css=") -> {
                        val css = arg.substring(4).trim()
                        require(css.isNotEmpty() && css.length <= 200) { "click:css= takes a CSS selector, such as #consent .accept" }
                        Step("click", css = css)
                    }
                    else -> runCatching { Step("click", numbers = numbers(arg, 2, 0.0, 100.0, "")) }.getOrElse {
                        require(arg.isNotEmpty() && arg.length <= 200) { "click takes the text of what to click, css= and a selector, or x,y in percent" }
                        Step("click", text = arg)
                    }
                }
                "drag", "rightdrag" -> steps += Step(name, numbers = numbers(arg, 4, 0.0, 100.0, "$name takes x1,y1,x2,y2 in percent"))
                "scroll" -> {
                    val values = numbers(arg, 3, -5000.0, 5000.0, "scroll takes x,y in percent and an amount in pixels")
                    require(values.take(2).all { it in 0.0..100.0 }) { "scroll takes x,y in percent and an amount in pixels" }
                    steps += Step("scroll", numbers = values)
                }
                else -> throw IllegalArgumentException("unknown step '$name': use wait, click, drag, rightdrag or scroll")
            }
        }
        require(steps.size <= MAX_STEPS) { "at most $MAX_STEPS steps" }
        return steps
    }

    private fun numbers(arg: String, count: Int, low: Double, high: Double, problem: String): List<Double> {
        val values = arg.split(",").map { it.trim().toDoubleOrNull() ?: throw IllegalArgumentException(problem) }
        require(values.size == count && values.all { it in low..high }) { problem }
        return values
    }

    /**
     * Takes steps on one WebView, from the start each time ([take]); a newer [take] or [stop]
     * ends the one before. [open] keeps a page's steps, which [loaded] takes after each load
     * of its site (a reload loses what they did). Main thread only.
     */
    class Runner(private val view: WebView, private val main: Handler) {
        private val find = finder(view.context)
        private var run = 0
        private var page = ""
        private var steps: List<Step> = emptyList()

        /** The address to load for [url], without its steps, which are kept for [loaded]. */
        fun open(url: String): String {
            stop()
            val (address, list) = runCatching { split(url) }.getOrElse {
                Log.w(TAG, "The page's steps (#pleatview=…): ${it.message}")
                url to emptyList()
            }
            page = address
            steps = list
            return address
        }

        /** A page finished loading: its site's steps, from the start. */
        fun loaded(url: String?) {
            if (steps.isNotEmpty() && url != null && Config.originOf(url) == Config.originOf(page)) take(steps)
        }

        fun stop() {
            run++
        }

        /** Forgets the page's steps too. */
        fun clear() {
            stop()
            steps = emptyList()
        }

        fun take(steps: List<Step>) {
            val mine = ++run
            view.evaluateJavascript("[innerWidth, innerHeight]") { size ->
                val css = runCatching { JSONArray(size) }.getOrNull()
                val scale = if (css != null && css.optDouble(0) > 0) view.width / css.getDouble(0) else 1.0
                next(steps, 0, mine, scale)
            }
        }

        private fun next(steps: List<Step>, index: Int, mine: Int, scale: Double) {
            if (mine != run || index >= steps.size) {
                if (index >= steps.size) Log.i(TAG, "Took the page's ${steps.size} steps")
                return
            }
            val step = steps[index]
            val then = { delay: Long -> main.postDelayed({ next(steps, index + 1, mine, scale) }, delay) }
            when (step.kind) {
                "wait" -> then((step.numbers[0] * 1000).toLong())
                "click" -> if (step.numbers.isNotEmpty()) {
                    tap(point(step.numbers[0], step.numbers[1]))
                    then(PAUSE_MS)
                } else {
                    look(step, mine, scale, SystemClock.uptimeMillis() + FIND_MS) { at ->
                        if (at != null) tap(at) else Log.i(TAG, "Nothing showing ${step.css.ifEmpty { step.text }} to click")
                        then(PAUSE_MS)
                    }
                }
                "drag", "rightdrag" -> drag(
                    point(step.numbers[0], step.numbers[1]), point(step.numbers[2], step.numbers[3]),
                    if (step.kind == "rightdrag") MotionEvent.BUTTON_SECONDARY else MotionEvent.BUTTON_PRIMARY,
                ) { then(PAUSE_MS) }
                "scroll" -> {
                    wheel(point(step.numbers[0], step.numbers[1]), step.numbers[2])
                    then(PAUSE_MS)
                }
            }
        }

        private fun point(x: Double, y: Double) = (view.width * x / 100).toFloat() to (view.height * y / 100).toFloat()

        /** Where the page shows the step's target, polling until it does (pages draw late). */
        private fun look(step: Step, mine: Int, scale: Double, until: Long, done: (Pair<Float, Float>?) -> Unit) {
            val wanted = if (step.css.isNotEmpty()) JSONObject().put("css", step.css) else JSONObject().put("text", step.text)
            view.evaluateJavascript("(${find.trim()})($wanted)") { result ->
                if (mine != run) return@evaluateJavascript
                val found = runCatching { JSONArray(result) }.getOrNull()
                when {
                    found != null -> done((found.getDouble(0) * scale).toFloat() to (found.getDouble(1) * scale).toFloat())
                    result == "\"bad selector\"" || SystemClock.uptimeMillis() > until -> done(null)
                    else -> main.postDelayed({ look(step, mine, scale, until, done) }, 500)
                }
            }
        }

        /** A finger down and up: what a click is on a touch screen. */
        private fun tap(at: Pair<Float, Float>) {
            val down = SystemClock.uptimeMillis()
            touch(down, down, MotionEvent.ACTION_DOWN, at)
            main.postDelayed({ touch(down, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, at) }, 60)
        }

        private fun touch(down: Long, time: Long, action: Int, at: Pair<Float, Float>) {
            val event = MotionEvent.obtain(down, time, action, at.first, at.second, 0)
            event.source = InputDevice.SOURCE_TOUCHSCREEN
            view.dispatchTouchEvent(event)
            event.recycle()
        }

        /** A mouse pressed at [start] and moved smoothly to [end] (maps follow the moves, not a jump). */
        private fun drag(start: Pair<Float, Float>, end: Pair<Float, Float>, button: Int, done: () -> Unit) {
            val down = SystemClock.uptimeMillis()
            // As a mouse sends them: Chromium takes the press and release from the button
            // events, and the down and up only frame the moves.
            mouse(down, MotionEvent.ACTION_DOWN, start, button)
            mouse(down, MotionEvent.ACTION_BUTTON_PRESS, start, button, changed = button)
            for (i in 1..DRAG_MOVES) {
                main.postDelayed({
                    val at = (start.first + (end.first - start.first) * i / DRAG_MOVES) to (start.second + (end.second - start.second) * i / DRAG_MOVES)
                    mouse(down, MotionEvent.ACTION_MOVE, at, button)
                    if (i == DRAG_MOVES) {
                        mouse(down, MotionEvent.ACTION_BUTTON_RELEASE, end, 0, changed = button)
                        mouse(down, MotionEvent.ACTION_UP, end, 0)
                        done()
                    }
                }, i * 20L)
            }
        }

        private fun mouse(down: Long, action: Int, at: Pair<Float, Float>, buttons: Int, axis: Float = 0f, changed: Int = 0) {
            val properties = arrayOf(MotionEvent.PointerProperties().apply { id = 0; toolType = MotionEvent.TOOL_TYPE_MOUSE })
            val coords = arrayOf(MotionEvent.PointerCoords().apply {
                x = at.first
                y = at.second
                pressure = 1f
                size = 1f
                if (action == MotionEvent.ACTION_SCROLL) setAxisValue(MotionEvent.AXIS_VSCROLL, axis)
            })
            val event = MotionEvent.obtain(down, SystemClock.uptimeMillis(), action, 1, properties, coords, 0, buttons,
                1f, 1f, 0, 0, InputDevice.SOURCE_MOUSE, 0)
            // Which button a press or release changed: only a hidden method sets it.
            if (changed != 0) runCatching { setActionButton?.invoke(event, changed) }
            when (action) {
                MotionEvent.ACTION_DOWN, MotionEvent.ACTION_MOVE, MotionEvent.ACTION_UP -> view.dispatchTouchEvent(event)
                else -> view.dispatchGenericMotionEvent(event) // the wheel, and a button's press and release
            }
            event.recycle()
        }

        /** A mouse wheel turned by [pixels] (negative is up, as the page sees it). */
        private fun wheel(at: Pair<Float, Float>, pixels: Double) {
            // Android 7 has no setting for it: 64 dp a notch, its default (a list item's height).
            val perNotch = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ViewConfiguration.get(view.context).scaledVerticalScrollFactor
            } else {
                64 * view.resources.displayMetrics.density
            }
            mouse(SystemClock.uptimeMillis(), MotionEvent.ACTION_SCROLL, at, 0, (-pixels / perNotch).toFloat())
        }
    }

    private const val TAG = "PleatView"

    @Suppress("DiscouragedPrivateApi", "SoonBlockedPrivateApi")
    private val setActionButton by lazy {
        runCatching { MotionEvent::class.java.getMethod("setActionButton", Int::class.javaPrimitiveType) }
            .onFailure { Log.i(TAG, "Drags without the button's name: ${it.message}") }
            .getOrNull()
    }
    private const val FIND_MS = 20_000L
    private const val PAUSE_MS = 400L
    private const val DRAG_MOVES = 20
}
