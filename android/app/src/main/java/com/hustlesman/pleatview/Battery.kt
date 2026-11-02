package com.hustlesman.pleatview

import android.app.UiModeManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.res.Configuration
import android.os.BatteryManager
import androidx.core.content.ContextCompat
import org.json.JSONObject

/** Battery level and power, for tablets on a wall; `charging` means plugged in, also when full. */
data class BatteryInfo(val level: Int, val charging: Boolean) {
    fun toJson(): JSONObject = JSONObject().put("level", level).put("charging", charging)

    companion object {
        /** null when there's no battery to report: none fitted, or a TV (some claim a full one). */
        fun of(present: Boolean, level: Int, scale: Int, plugged: Int, tv: Boolean): BatteryInfo? {
            if (!present || tv || level < 0 || scale <= 0) return null
            return BatteryInfo((level * 100 / scale).coerceIn(0, 100), plugged != 0)
        }
    }
}

/** Follows the battery and reports changes on the main thread. */
object Battery {
    fun watch(context: Context, onChange: (BatteryInfo?) -> Unit) {
        val modes = context.getSystemService(Context.UI_MODE_SERVICE) as UiModeManager
        val tv = modes.currentModeType == Configuration.UI_MODE_TYPE_TELEVISION
        fun read(intent: Intent?): BatteryInfo? = intent?.let {
            BatteryInfo.of(
                present = it.getBooleanExtra(BatteryManager.EXTRA_PRESENT, false),
                level = it.getIntExtra(BatteryManager.EXTRA_LEVEL, -1),
                scale = it.getIntExtra(BatteryManager.EXTRA_SCALE, -1),
                plugged = it.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0),
                tv = tv,
            )
        }
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(context: Context, intent: Intent) = onChange(read(intent))
        }
        // The system's sticky broadcast: registering returns the current state straight away.
        val current = ContextCompat.registerReceiver(
            context, receiver, IntentFilter(Intent.ACTION_BATTERY_CHANGED), ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        onChange(read(current))
    }
}
