"""What the renderer's Chromium runs in a page: the TV app's own page helper.

pleatview.js is the TV app's file, unchanged (a test keeps the copies equal). It signs the
Home Assistant frontend in, applies kiosk mode and switches dashboards in place. In the
app it talks to Kotlin through ``window.pleatviewBridge``; here a small stand-in answers
instead, with the TV's access token.
"""

from __future__ import annotations

import json
from pathlib import Path

HELPER = (Path(__file__).parent / "pleatview.js").read_text()

# The access token lasts as long as the TV's; the frontend asks again after this.
TOKEN_LIFETIME_SECONDS = 1800

BRIDGE = """
window.pleatviewBridge = {
  onmessage: null,
  postMessage: function (raw) {
    var message = {};
    try { message = JSON.parse(raw); } catch (e) { return; }
    var payload = message.payload || {};
    var reply = null;
    if (message.type === "getExternalAuth") {
      reply = __TOKEN__
        ? { type: "auth", callback: payload.callback, success: true,
            data: { access_token: __TOKEN__, expires_in: __LIFETIME__ } }
        : { type: "auth", callback: payload.callback, success: false, data: { message: "auth_invalid" } };
    } else if (message.type === "revokeExternalAuth") {
      reply = { type: "auth", callback: payload.callback, success: true, data: null };
    }
    var bridge = this;
    if (reply && /^[A-Za-z_$][A-Za-z0-9_$]{0,63}$/.test(reply.callback || "")) {
      setTimeout(function () { if (bridge.onmessage) bridge.onmessage({ data: JSON.stringify(reply) }); }, 0);
    }
  }
};
"""


def script(ha_origin: str, token: str, kiosk: str) -> str:
    """The script for every new document: in Home Assistant's pages only, the bridge and helper."""
    config = {"externalAuth": bool(token), "kiosk": kiosk, "viewportWidth": 0}
    bridge = BRIDGE.replace("__LIFETIME__", str(TOKEN_LIFETIME_SECONDS)).replace("__TOKEN__", json.dumps(token))
    helper = HELPER.replace("__PLEATVIEW_CONFIG__", json.dumps(config))
    return (
        f"if ({json.dumps(ha_origin)} && location.origin === {json.dumps(ha_origin)} && !window.pleatviewBridge) {{\n"
        f"{bridge}\n{helper}\n}}"
    )
