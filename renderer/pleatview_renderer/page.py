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


# Notifications and cameras drawn into the page, for screens that only play the stream
# (PleatView Light): items is the list of what shows now. It draws in a shadow root on top of
# everything, with text as text, and redraws only when the list changes.
OVERLAY = """
(function (items) {
  var host = document.getElementById("__pleatview_overlays");
  if (!host) {
    host = document.createElement("div");
    host.id = "__pleatview_overlays";
    host.style.cssText = "all:initial;position:fixed;inset:0;z-index:2147483647;pointer-events:none";
    (document.body || document.documentElement).appendChild(host);
    host.attachShadow({ mode: "open" });
  }
  var key = JSON.stringify(items);
  if (host.dataset.key === key) return;
  host.dataset.key = key;
  var root = host.shadowRoot;
  while (root.firstChild) root.removeChild(root.firstChild);
  var style = document.createElement("style");
  style.textContent = ".item{position:absolute;margin:3vh 2vw}" +
    ".top-left{top:0;left:0}.top-right{top:0;right:0}.bottom-left{bottom:0;left:0}.bottom-right{bottom:0;right:0}" +
    ".center{top:50%;left:50%;transform:translate(-50%,-50%)}" +
    ".notify{box-sizing:border-box;max-width:36vw;padding:2.4vh 2vw;border-radius:1.2vw;background:rgba(23,32,37,.96);" +
    "color:#edf3f6;font:400 2.4vh system-ui,sans-serif;box-shadow:0 1vh 4vh rgba(0,0,0,.45)}" +
    ".notify img{display:block;max-width:100%;max-height:40vh;border-radius:.6vw;margin-bottom:1.6vh}" +
    ".notify .title{font-weight:700;font-size:2.8vh;margin-bottom:.6vh}" +
    ".image img{display:block;width:100%;border-radius:1vw;box-shadow:0 1vh 4vh rgba(0,0,0,.5);background:#000}";
  root.appendChild(style);
  items.forEach(function (it) {
    var el = document.createElement("div");
    el.className = "item " + it.kind + " " + it.position;
    if (it.kind === "image") {
      el.style.width = it.size + "vw";
      var img = document.createElement("img");
      img.src = it.url;
      el.appendChild(img);
    } else {
      if (it.image_url) {
        var pic = document.createElement("img");
        pic.src = it.image_url;
        el.appendChild(pic);
      }
      if (it.title) {
        var title = document.createElement("div");
        title.className = "title";
        title.textContent = it.title;
        el.appendChild(title);
      }
      var message = document.createElement("div");
      message.textContent = it.message;
      el.appendChild(message);
    }
    root.appendChild(el);
  });
})
"""


def overlay_script(items: list[dict]) -> str:
    return f"({OVERLAY.strip()})({json.dumps(items)})"
