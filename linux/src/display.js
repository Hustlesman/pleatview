// The screen: a full-screen window with the dashboard, a web page on top for show_url, and
// PleatView's own layer (setup screen, cards, media, banner, menu, standby curtain) on top
// of everything. Plays the part of MainActivity.kt and DashboardController.kt on Android.
import { BaseWindow, WebContentsView, powerSaveBlocker, screen } from "electron";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { needsDisplay } from "./command.js";
import { pathOf } from "./config.js";
import { displayPower } from "./power.js";

const here = dirname(fileURLToPath(import.meta.url));
const CALLBACK_NAME = /^[A-Za-z_$][A-Za-z0-9_$]{0,63}$/;
const TOKEN_LIFETIME_SECONDS = 1800;
const RETRY_DELAYS_SECONDS = [3, 5, 10, 20, 30, 60];
const HEARTBEAT_TIMEOUT_MS = 90_000;
const INPUT_GRACE_MS = 60_000;

export class Display {
  /**
   * @param {object} options
   * @param {import("./hub.js").Hub} options.hub
   * @param {() => import("./config.js").Config} options.config
   * @param {() => object} options.setup what the setup screen and menu show: name, address, port, PIN, version
   * @param {string} options.assets folder with pleatview.js
   * @param {() => void} options.quit
   */
  constructor({ hub, config, setup, assets, quit }) {
    this.hub = hub;
    this.config = config;
    this.setup = setup;
    this.quit = quit;
    this.pageScript = readFileSync(join(assets, "pleatview.js"), "utf8");
    this.uiRegions = { full: true, rects: [] };
    this.retryAttempt = 0;
    this.forcedAuth = [];
    this.lastInput = 0;
    this.lastHeartbeat = 0;
    this.holdUntil = 0;
    this.standby = false;
    this.menuOpen = false;
    this.timers = {};
    this.sleepBlocker = null;
  }

  open() {
    // The whole screen, also without a window manager to make it full screen (cage, Xvfb).
    const { bounds } = screen.getPrimaryDisplay();
    this.window = new BaseWindow({ ...bounds, frame: false, kiosk: true, fullscreen: true, backgroundColor: "#0f1417", title: "PleatView" });
    this.dashboard = this.view(join(here, "preload-dashboard.cjs"), "persist:dashboard");
    this.ui = this.view(join(here, "preload-ui.cjs"), undefined, { backgroundThrottling: false });
    this.ui.setBackgroundColor("#00000000");
    this.window.contentView.addChildView(this.dashboard);
    this.window.contentView.addChildView(this.ui);
    this.window.on("resize", () => this.layout());
    this.watchDashboard();
    this.watchUi();
    this.ui.webContents.loadFile(join(here, "ui", "index.html"));
    this.layout();
    this.applyConfig();
    setInterval(() => this.watchdog(), 30_000);
  }

  view(preload, partition, extra = {}) {
    return new WebContentsView({ webPreferences: { preload, partition, contextIsolation: true, sandbox: true, ...extra } });
  }

  get size() {
    const { width, height } = this.window.getContentBounds();
    return { width, height };
  }

  layout() {
    const { width, height } = this.size;
    this.dashboard.setBounds({ x: 0, y: 0, width, height });
    if (this.web) this.web.setBounds(this.webBounds(this.webLayout));
    this.placeUi();
    this.toUi({ type: "screen", width, height });
  }

  // ---- PleatView's layer --------------------------------------------------------------

  /**
   * The layer covers only what it shows, so touches elsewhere reach the dashboard: the whole
   * screen for the setup screen, menu, full-screen media and standby, else the area its
   * cards, banner and picture-in-picture take up. Its page is shifted to match.
   */
  placeUi() {
    const { width, height } = this.size;
    const { full, rects } = this.uiRegions;
    let box = { x: 0, y: 0, width, height };
    if (!full) {
      if (!rects.length) {
        this.ui.setVisible(false);
        return;
      }
      const left = Math.max(0, Math.min(...rects.map((r) => r.x)));
      const top = Math.max(0, Math.min(...rects.map((r) => r.y)));
      const right = Math.min(width, Math.max(...rects.map((r) => r.x + r.width)));
      const bottom = Math.min(height, Math.max(...rects.map((r) => r.y + r.height)));
      box = { x: Math.floor(left), y: Math.floor(top), width: Math.ceil(right - left), height: Math.ceil(bottom - top) };
    }
    this.ui.setBounds(box);
    this.ui.setVisible(true);
    this.toUi({ type: "offset", x: box.x, y: box.y });
  }

  toUi(message) {
    if (!this.ui.webContents.isDestroyed()) this.ui.webContents.send("ui", message);
  }

  watchUi() {
    const ipc = this.ui.webContents.ipc;
    ipc.on("ui", (event, message) => {
      switch (message?.type) {
        case "ready":
          this.layout();
          this.showSetupOrDashboard();
          break;
        case "regions":
          this.uiRegions = { full: Boolean(message.full), rects: Array.isArray(message.rects) ? message.rects : [] };
          this.placeUi();
          break;
        case "media":
          this.hub.update({ media: message.media ? { ...message.media, updated_at: Date.now() / 1000 } : null });
          break;
        case "volume":
          this.hub.update({ volume: message.volume, muted: message.muted });
          break;
        case "overlay-closed":
          this.hub.update({ overlay: null });
          break;
        case "menu":
          this.menuOpen = Boolean(message.open);
          (this.menuOpen ? this.ui : this.dashboard).webContents.focus();
          break;
        case "action":
          this.lastInput = Date.now();
          this.onMenuAction(message);
          break;
        case "wake":
          this.wake();
          break;
      }
    });
    this.ui.webContents.on("before-input-event", (event, input) => this.onKey(event, input));
  }

  onMenuAction({ action, path }) {
    if (action === "home") this.goHome();
    else if (action === "open" && typeof path === "string") this.showTarget(path, { browsing: true });
    else if (action === "reload") this.dashboard.webContents.reload();
    else if (action === "quit") this.quit();
  }

  openMenu() {
    const config = this.config();
    this.toUi({
      type: "menu",
      info: this.setup(),
      haUrl: config.haUrl,
      dashboards: config.dashboards,
      configured: config.isConfigured,
    });
  }

  onKey(event, input) {
    if (input.type !== "keyDown") return;
    this.lastInput = Date.now();
    if (this.standby) {
      event.preventDefault();
      this.wake();
      return;
    }
    if (["Escape", "ContextMenu", "F10", "BrowserBack", "GoBack"].includes(input.key)) {
      event.preventDefault();
      if (this.menuOpen) this.toUi({ type: "close-menu" });
      else this.openMenu();
    } else if (input.key === "MediaPlayPause" || input.key === "MediaStop") {
      this.toUi({ type: "media", command: { name: input.key === "MediaStop" ? "stop" : "toggle" } });
    }
  }

  // ---- Home Assistant's dashboard ------------------------------------------------------

  watchDashboard() {
    const wc = this.dashboard.webContents;
    // Asked by the preload: only Home Assistant's own pages get the bridge that hands out the token.
    wc.ipc.on("page", (event, origin) => {
      const config = this.config();
      event.returnValue = { externalAuth: Boolean(config.haToken) && origin === config.haOrigin };
    });
    wc.ipc.on("bridge", (event, raw) => this.onBridge(event, raw));
    wc.on("dom-ready", () => {
      const config = this.config();
      wc.setZoomFactor(config.zoom / 100);
      if (!config.isHaUrl(wc.getURL())) return;
      const pageConfig = { externalAuth: Boolean(config.haToken), kiosk: config.kiosk, viewportWidth: 0 };
      wc.executeJavaScript(this.pageScript.replaceAll("__PLEATVIEW_CONFIG__", JSON.stringify(pageConfig)))
        .catch((e) => console.warn("PleatView: page helper failed:", e.message));
    });
    wc.on("did-navigate", (event, url) => this.onLocation(url));
    wc.on("did-navigate-in-page", (event, url, isMainFrame) => isMainFrame && this.onLocation(url));
    wc.on("did-finish-load", () => {
      this.retryAttempt = 0;
      this.hub.update({ last_error: null });
      this.toUi({ type: "banner", text: null });
    });
    wc.on("did-fail-load", (event, code, description, url, isMainFrame) => {
      if (!isMainFrame || code === -3) return; // -3: replaced by another navigation
      this.onLoadFailed(url, description);
    });
    wc.on("render-process-gone", () => {
      console.warn("PleatView: the dashboard's renderer stopped; reloading");
      setTimeout(() => wc.reload(), 1000);
    });
    // Right-click, or a long press on a touch screen, opens the menu.
    wc.on("context-menu", (event) => {
      event.preventDefault();
      this.openMenu();
    });
    wc.on("before-input-event", (event, input) => this.onKey(event, input));
    // Links that would open new windows stay in the dashboard.
    wc.setWindowOpenHandler(({ url }) => {
      if (/^https?:/i.test(url)) wc.loadURL(url);
      return { action: "deny" };
    });
  }

  onLocation(url) {
    const config = this.config();
    this.hub.update({ url, dashboard: config.isHaUrl(url) ? pathOf(url) : null });
  }

  onLoadFailed(url, description) {
    const delay = RETRY_DELAYS_SECONDS[Math.min(this.retryAttempt++, RETRY_DELAYS_SECONDS.length - 1)];
    const where = new URL(url || this.config().haUrl || "http://unknown").host;
    this.hub.update({ last_error: description || "load failed" });
    this.toUi({ type: "banner", text: `Can't reach Home Assistant (${where}). Retrying in ${delay} s…` });
    clearTimeout(this.timers.retry);
    this.timers.retry = setTimeout(() => this.dashboard.webContents.reload(), delay * 1000);
  }

  onBridge(event, raw) {
    const config = this.config();
    const frame = event.senderFrame;
    if (!frame || frame !== this.dashboard.webContents.mainFrame || !config.isHaUrl(frame.url)) return;
    let message;
    try {
      message = JSON.parse(raw);
    } catch {
      return;
    }
    const callback = message?.payload?.callback;
    if (message?.type === "getExternalAuth" && CALLBACK_NAME.test(callback ?? "")) {
      this.answerAuth(callback, this.authReply(Boolean(message.payload.force)));
    } else if (message?.type === "revokeExternalAuth" && CALLBACK_NAME.test(callback ?? "")) {
      this.answerAuth(callback, { success: true, data: null });
    } else if (message?.type === "status") {
      this.lastHeartbeat = Date.now();
      const connected = typeof message.connected === "boolean" ? message.connected : null;
      this.hub.update({ ha_connected: connected, ...(connected ? { auth_error: false, last_error: null } : {}) });
      if (typeof message.path === "string" && message.path) this.hub.update({ dashboard: message.path });
    }
  }

  /** The frontend's token request. A forced refresh means Home Assistant rejected the token. */
  authReply(force) {
    const now = Date.now();
    if (force) this.forcedAuth.push(now);
    this.forcedAuth = this.forcedAuth.filter((t) => now - t <= 60_000);
    const token = this.config().haToken;
    if (!token || this.forcedAuth.length > 1) {
      console.warn("PleatView: Home Assistant rejected the access token");
      this.hub.update({ auth_error: true });
      this.toUi({ type: "banner", text: "Home Assistant rejected the access token. Re-provision it from Home Assistant or the setup page." });
      return { success: false, data: { message: "auth_invalid" } };
    }
    return { success: true, data: { access_token: token, expires_in: TOKEN_LIFETIME_SECONDS } };
  }

  /** Runs the callback in the page, but only if it's still Home Assistant's: the reply can carry the token. */
  answerAuth(callback, reply) {
    const origin = this.config().haOrigin;
    if (!origin) return;
    const js = `if(location.origin===${JSON.stringify(origin)}){var f=window[${JSON.stringify(callback)}];` +
      `if(typeof f==="function")f(${reply.success},${JSON.stringify(reply.data)});}`;
    this.dashboard.webContents.mainFrame.executeJavaScript(js).catch(() => {});
  }

  /** Reloads a Home Assistant page that stopped reporting in. */
  watchdog() {
    const wc = this.dashboard.webContents;
    if (this.standby || !this.config().isHaUrl(wc.getURL()) || wc.isLoading()) return;
    if (this.lastHeartbeat && Date.now() - this.lastHeartbeat > HEARTBEAT_TIMEOUT_MS) {
      console.warn("PleatView: the dashboard stopped reporting; reloading");
      this.lastHeartbeat = 0;
      wc.reload();
    }
  }

  // ---- Configuration ---------------------------------------------------------------------

  /** After a config change: what to show, zoom, timers, keeping the screen awake. */
  applyConfig(previous = null) {
    const config = this.config();
    this.hub.update({ config_rev: config.rev, view: config.isConfigured ? "dashboard" : "setup" });
    if (!this.window) return;
    this.dashboard.webContents.setZoomFactor(config.zoom / 100);
    const moved = !previous || previous.haUrl !== config.haUrl || previous.haToken !== config.haToken || previous.kiosk !== config.kiosk;
    if (moved || previous.homePath !== config.homePath) this.showSetupOrDashboard();
    this.restartTimers();
    this.keepAwake();
  }

  showSetupOrDashboard() {
    const config = this.config();
    this.toUi({ type: "setup", visible: !config.isConfigured, info: this.setup() });
    if (config.isConfigured) this.goHome();
    else this.dashboard.webContents.loadURL("about:blank");
  }

  /** The setup screen's PIN, name or address changed. */
  refreshSetup() {
    this.toUi({ type: "setup-info", info: this.setup() });
  }

  restartTimers() {
    const config = this.config();
    clearInterval(this.timers.rotate);
    clearInterval(this.timers.reload);
    if (config.rotateSeconds > 0 && config.dashboards.length > 1) {
      this.timers.rotate = setInterval(() => this.rotate(), config.rotateSeconds * 1000);
    }
    if (config.reloadMinutes > 0) {
      this.timers.reload = setInterval(() => this.dashboard.webContents.reload(), config.reloadMinutes * 60_000);
    }
  }

  /** Next dashboard in the rotation, unless someone is using the screen or Home Assistant asked for one. */
  rotate() {
    const now = Date.now();
    if (this.standby || this.menuOpen || now < this.holdUntil || now - this.lastInput < INPUT_GRACE_MS) return;
    this.cycle(1);
  }

  cycle(step) {
    const { dashboards } = this.config();
    if (!dashboards.length) return;
    const current = this.hub.state.dashboard ?? "";
    const index = dashboards.findIndex((d) => current.startsWith(d.path));
    const next = dashboards[(index + step + dashboards.length) % dashboards.length];
    this.navigate(this.config().resolve(next.path));
  }

  keepAwake() {
    const wanted = this.config().keepScreenOn && !this.standby;
    if (wanted && this.sleepBlocker === null) this.sleepBlocker = powerSaveBlocker.start("prevent-display-sleep");
    if (!wanted && this.sleepBlocker !== null) {
      powerSaveBlocker.stop(this.sleepBlocker);
      this.sleepBlocker = null;
    }
  }

  // ---- Commands -----------------------------------------------------------------------

  handle(command) {
    if (needsDisplay(command) && this.standby && command.name !== "turn_on") this.wake();
    switch (command.name) {
      case "show_dashboard":
        this.showTarget(command.path, { duration: command.duration });
        break;
      case "show_web":
        if (command.kind === "page") this.showWeb(command);
        else this.toUi({ type: "image", command });
        this.hub.update({ overlay: { kind: command.kind, url: command.url, mode: command.layout.mode } });
        break;
      case "play_media":
      case "notify":
      case "play":
      case "pause":
      case "seek":
      case "set_volume":
      case "mute":
        this.toUi({ type: command.name === "notify" ? "notify" : "media", command });
        break;
      case "stop":
        this.toUi({ type: "media", command });
        this.closeOverlays();
        break;
      case "home":
        this.goHome();
        break;
      case "reload":
        this.dashboard.webContents.reload();
        break;
      case "next_dashboard":
        this.cycle(1);
        break;
      case "previous_dashboard":
        this.cycle(-1);
        break;
      case "turn_on":
        this.wake();
        break;
      case "turn_off":
        this.sleep();
        break;
      case "forget_sign_ins":
        break; // Sign-ins aren't saved on Linux yet: there's nothing to forget.
    }
  }

  /** A dashboard path on Home Assistant, or a site's URL. */
  showTarget(target, { duration = null, browsing = false } = {}) {
    clearTimeout(this.timers.hold);
    this.holdUntil = duration ? Date.now() + duration * 1000 : 0;
    if (duration) this.timers.hold = setTimeout(() => this.goHome(), duration * 1000);
    if (browsing) this.lastInput = Date.now();
    this.navigate(this.config().resolve(target));
  }

  goHome() {
    const config = this.config();
    if (!config.isConfigured) return;
    clearTimeout(this.timers.hold);
    this.holdUntil = 0;
    this.toUi({ type: "media", command: { name: "stop" } });
    this.closeOverlays();
    this.navigate(config.resolve(config.homePath));
  }

  /** Within Home Assistant, the frontend switches dashboards itself, without reloading. */
  navigate(url) {
    const config = this.config();
    const wc = this.dashboard.webContents;
    if (config.isHaUrl(url) && config.isHaUrl(wc.getURL()) && !wc.isLoading()) {
      const js = `window.__pleatview?(window.__pleatview.navigate(${JSON.stringify(pathOf(url))}),true):false`;
      wc.executeJavaScript(js).then((done) => done || wc.loadURL(url), () => wc.loadURL(url));
    } else {
      wc.loadURL(url).catch(() => {});
    }
    this.hub.update({ url, dashboard: config.isHaUrl(url) ? pathOf(url) : null });
  }

  showWeb(command) {
    if (!this.web) {
      this.web = this.view(undefined, "persist:dashboard");
      this.window.contentView.addChildView(this.web, 1); // above the dashboard, below PleatView's layer
    }
    this.webLayout = command.layout;
    this.web.setBounds(this.webBounds(command.layout));
    this.web.webContents.loadURL(command.url).catch(() => {});
    clearTimeout(this.timers.web);
    if (command.duration) this.timers.web = setTimeout(() => this.closeOverlays(), command.duration * 1000);
  }

  webBounds(layout) {
    const { width, height } = this.size;
    if (layout.mode !== "pip") return { x: 0, y: 0, width, height };
    const margin = 24;
    const w = Math.round((width * layout.size) / 100);
    const h = Math.round((w * 9) / 16);
    const right = layout.position.endsWith("right");
    const bottom = layout.position.startsWith("bottom");
    const center = layout.position === "center";
    return {
      x: center ? Math.round((width - w) / 2) : right ? width - w - margin : margin,
      y: center ? Math.round((height - h) / 2) : bottom ? height - h - margin : margin,
      width: w,
      height: h,
    };
  }

  closeOverlays() {
    clearTimeout(this.timers.web);
    if (this.web) {
      this.window.contentView.removeChildView(this.web);
      this.web.webContents.close();
      this.web = null;
    }
    this.toUi({ type: "image", command: null });
    this.hub.update({ overlay: null });
  }

  sleep() {
    if (this.standby) return;
    this.standby = true;
    this.toUi({ type: "media", command: { name: "stop" } });
    this.toUi({ type: "standby", on: true });
    this.hub.update({ power: "standby" });
    this.keepAwake();
    displayPower(false).catch(() => {});
  }

  wake() {
    if (!this.standby) return;
    this.standby = false;
    this.toUi({ type: "standby", on: false });
    this.hub.update({ power: "on" });
    this.keepAwake();
    displayPower(true).catch(() => {});
  }
}
