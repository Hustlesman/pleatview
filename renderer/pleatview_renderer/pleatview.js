/*
 * PleatView page helper, injected by the TV app into Home Assistant pages only.
 *
 *  - Implements the "external auth" contract the Home Assistant frontend uses to get
 *    access tokens from a host app (frontend: src/external_app/external_auth.ts),
 *    so the TV never shows a login page.
 *  - Scales the viewport, hides the sidebar/header (kiosk mode), navigates without
 *    reloading, and reports a heartbeat that drives the app's watchdog.
 *
 * __PLEATVIEW_CONFIG__ is replaced by the app with a JSON object:
 *   { externalAuth: bool, kiosk: "off"|"sidebar"|"full", viewportWidth: int (0 = default) }
 */
(function () {
  "use strict";
  if (window.__pleatview) return;

  var cfg = __PLEATVIEW_CONFIG__;
  var pv = (window.__pleatview = { config: cfg });
  var STYLE_ID = "pleatview-kiosk";

  function channel() {
    // WebMessageListener object (modern WebView) or @JavascriptInterface fallback.
    return window.pleatviewBridge || window.pleatviewNative || null;
  }

  function send(message) {
    var ch = channel();
    if (!ch) return;
    try {
      ch.postMessage(JSON.stringify(message));
    } catch (e) {
      /* app side went away */
    }
  }

  function parse(value) {
    if (typeof value !== "string") return value || {};
    try {
      return JSON.parse(value);
    } catch (e) {
      return {};
    }
  }

  pv.receive = function (message) {
    if (!message) return;
    if (message.type === "auth") {
      var callback = window[message.callback];
      if (typeof callback === "function") callback(message.success, message.data);
    } else if (message.type === "navigate") {
      pv.navigate(message.path);
    }
  };

  if (window.pleatviewBridge) {
    window.pleatviewBridge.onmessage = function (event) {
      pv.receive(parse(event.data));
    };
  }

  if (cfg.externalAuth && window.pleatviewBridge && !window.externalApp) {
    window.externalApp = {
      getExternalAuth: function (payload) {
        send({ type: "getExternalAuth", payload: parse(payload) });
      },
      revokeExternalAuth: function (payload) {
        send({ type: "revokeExternalAuth", payload: parse(payload) });
      },
    };
  }

  // Same mechanism as the frontend's navigate(): update history, then tell the app shell.
  pv.navigate = function (path) {
    if (location.pathname + location.search + location.hash !== path) {
      history.replaceState(history.state, "", path);
      window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: true } }));
    }
    restartTicks();
    setTimeout(report, 400);
  };

  // ---- viewport scaling ----------------------------------------------------------

  function applyViewport() {
    if (!cfg.viewportWidth || !document.head) return false;
    var meta = document.head.querySelector('meta[name="viewport"]');
    var content = "width=" + cfg.viewportWidth;
    if (!meta) {
      meta = document.createElement("meta");
      meta.name = "viewport";
      document.head.appendChild(meta);
    }
    if (meta.getAttribute("content") !== content) meta.setAttribute("content", content);
    return true;
  }

  if (cfg.viewportWidth && !applyViewport()) {
    // Apply as soon as the parser creates <head>, before the frontend lays out.
    var observer = new MutationObserver(function () {
      if (document.head && document.head.querySelector('meta[name="viewport"]')) {
        applyViewport();
        observer.disconnect();
      }
    });
    observer.observe(document, { childList: true, subtree: true });
  }

  // ---- kiosk mode ---------------------------------------------------------------

  // Selectors cover both the 2026 layout (--ha-sidebar-width, .sidebar-shell) and the
  // older Material drawer (--mdc-drawer-width, .mdc-drawer).
  var CSS = {
    // <home-assistant-main>: collapse the sidebar column.
    main:
      ":host{--ha-sidebar-width:0px!important;--ha-top-app-bar-width:100%!important;" +
      "--mdc-drawer-width:0px!important;--mdc-top-app-bar-width:100%!important}" +
      "ha-sidebar{display:none!important}",
    // <ha-drawer>: hide the drawer itself and its reserved space.
    drawer:
      ".sidebar-shell,.mdc-drawer{display:none!important}" +
      ".app-content,.mdc-drawer-app-content{margin-left:0!important;padding-left:0!important;" +
      "padding-inline-start:0!important;width:100%!important}",
    // <hui-root>: hide the dashboard header and reclaim its height.
    header:
      ":host{--header-height:0px!important}" +
      ".header,app-header,app-toolbar{display:none!important}" +
      "#view{padding-top:0!important;min-height:100vh!important}",
    // <hui-root> with only the sidebar hidden: the menu button has nothing to open.
    menuButton: "ha-menu-button{display:none!important}",
  };

  // Shadow roots we never need to look inside (they hold the dashboard cards).
  var SKIP = ["hui-root", "ha-sidebar"];

  function findHosts(tags) {
    var found = {};
    var roots = [document];
    var visited = 0;
    while (roots.length && visited < 300) {
      var elements = roots.pop().querySelectorAll("*");
      for (var i = 0; i < elements.length; i++) {
        var el = elements[i];
        var tag = el.localName;
        if (tags.indexOf(tag) >= 0) (found[tag] = found[tag] || []).push(el);
        if (el.shadowRoot && SKIP.indexOf(tag) < 0) {
          roots.push(el.shadowRoot);
          visited++;
        }
      }
    }
    return found;
  }

  function setStyle(host, css) {
    var root = host.shadowRoot;
    if (!root) return;
    var style = root.querySelector("style#" + STYLE_ID);
    if (!css) {
      if (style) style.remove();
      return;
    }
    if (!style) {
      style = document.createElement("style");
      style.id = STYLE_ID;
      root.appendChild(style);
    }
    if (style.textContent !== css) style.textContent = css;
  }

  function each(list, fn) {
    for (var i = 0; list && i < list.length; i++) fn(list[i]);
  }

  function applyKiosk() {
    var mode = cfg.kiosk || "off";
    var hideSidebar = mode === "sidebar" || mode === "full";
    var hideHeader = mode === "full";
    if (!hideSidebar && !hideHeader && !pv.kioskApplied) return;
    pv.kioskApplied = hideSidebar || hideHeader;
    var hosts = findHosts(["home-assistant-main", "ha-drawer", "hui-root"]);
    each(hosts["home-assistant-main"], function (el) {
      setStyle(el, hideSidebar ? CSS.main : "");
    });
    each(hosts["ha-drawer"], function (el) {
      setStyle(el, hideSidebar ? CSS.drawer : "");
    });
    each(hosts["hui-root"], function (el) {
      setStyle(el, hideHeader ? CSS.header : hideSidebar ? CSS.menuButton : "");
    });
  }

  // ---- heartbeat ----------------------------------------------------------------

  function report() {
    var ha = document.querySelector("home-assistant");
    var hass = ha && ha.hass;
    send({
      type: "status",
      path: location.pathname + location.search,
      ready: !!hass,
      connected: hass ? !!hass.connected : null,
    });
  }

  // Re-apply often while the frontend renders, then settle down.
  var ticks = 0;
  var timer = null;

  function tick() {
    try {
      applyViewport();
      applyKiosk();
    } catch (e) {
      /* never break the page */
    }
    ticks++;
    timer = setTimeout(tick, ticks < 30 ? 500 : 3000);
  }

  function restartTicks() {
    ticks = 0;
    if (timer) clearTimeout(timer);
    tick();
  }

  window.addEventListener("location-changed", function () {
    restartTicks();
    setTimeout(report, 400);
  });
  window.addEventListener("popstate", function () {
    restartTicks();
    setTimeout(report, 400);
  });
  window.addEventListener("connection-status", function () {
    setTimeout(report, 100);
  });

  tick();
  report();
  setInterval(report, 15000);
})();
