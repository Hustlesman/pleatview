// The screen's state, as reported to Home Assistant (docs/protocol.md, "State"), and the
// route from commands to the display. Plays the part of Hub.kt in the Android app.

const PUBLISH_DELAY_MS = 100;

export class Hub {
  constructor({ appVersion }) {
    this.appVersion = appVersion;
    this.listeners = new Set();
    this.display = null;
    this.published = "";
    this.timer = null;
    this.state = {
      foreground: true,
      power: "on",
      view: "setup",
      url: null,
      dashboard: null,
      ha_connected: null,
      auth_error: false,
      last_error: null,
      overlay: null,
      media: null,
      volume: 1.0,
      muted: false,
      config_rev: "",
      // A Linux kiosk can always bring itself to the front.
      overlay_permission: true,
      sign_in: null,
      battery: null,
      // What this app can do beyond protocol v1 (docs/protocol.md, "State").
      // rotation: Home Assistant may send how the screen is mounted (config "rotation").
      // protocol_v2: it connects out to a controller anywhere (docs/protocol-v2.md).
      features: ["render_stream", "home_without_ha", "rotation", "protocol_v2"],
      screen: null,
    };
  }

  /** Changes the state; listeners hear about it once, a moment later, if anything changed. */
  update(patch) {
    Object.assign(this.state, patch);
    if (this.timer) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      const json = this.stateJson();
      if (json === this.published) return;
      this.published = json;
      for (const listener of this.listeners) listener(json);
    }, PUBLISH_DELAY_MS);
  }

  stateJson() {
    return JSON.stringify({ ...this.state, app_version: this.appVersion });
  }

  onState(listener) {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  dispatch(command) {
    this.display?.handle(command);
  }
}
