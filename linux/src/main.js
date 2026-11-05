// PleatView for Linux: Home Assistant dashboards, media and notifications on a screen
// attached to a mini PC or a Raspberry Pi, controlled by the PleatView integration.
import { app } from "electron";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import QRCode from "qrcode";
import { API_VERSION, createApi } from "./api.js";
import { Config } from "./config.js";
import { defaultName, deviceId, hardware, lanAddress } from "./device.js";
import { Display } from "./display.js";
import { Hub } from "./hub.js";
import { Advertiser } from "./mdns.js";
import { Pairing } from "./pairing.js";
import { Store } from "./store.js";

const here = dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PLEATVIEW_PORT) || 8765;

// The page helper and the phone setup page are shared with the Android app: packaged
// builds carry a copy in assets/, a checkout reads them from the Android app.
const assets = [join(here, "..", "assets"), join(here, "..", "..", "android", "app", "src", "main", "assets")]
  .find((dir) => existsSync(join(dir, "pleatview.js")));

if (!app.requestSingleInstanceLock()) app.exit(0);
// Screens that are also used for other things still keep their dashboard awake.
app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");

const store = new Store();
const id = deviceId(store);
const version = app.getVersion();
const { manufacturer, model } = hardware();
let config = Config.fromJson(store.readJson("config.json", {}));
const hub = new Hub({ appVersion: version });
const advertiser = new Advertiser();
let qr = { text: "", svg: "" };

const pairing = new Pairing({
  load: () => store.read("tokens.json"),
  save: (json) => store.write("tokens.json", json),
  onChanged: () => display?.refreshSetup(),
});

const name = () => config.deviceName || defaultName();

/** What the setup screen and the menu show. The QR code opens the phone page with the PIN filled in. */
function setupInfo() {
  const address = lanAddress();
  const text = `http://${address}:${PORT}/#pin=${pairing.pin}`;
  if (qr.text !== text) {
    qr = { text, svg: "" };
    QRCode.toString(text, { type: "svg", errorCorrectionLevel: "M", margin: 2 }).then((svg) => {
      if (qr.text !== text) return;
      qr.svg = svg;
      display?.refreshSetup();
    });
  }
  return { name: name(), address, port: PORT, pin: pairing.pin, version, qr: qr.svg };
}

function updateConfig(update) {
  const previous = config;
  config = config.merge(update);
  store.writeJson("config.json", config.toJson(true));
  if (previous.deviceName !== config.deviceName) announce();
  display?.applyConfig(previous);
  return config;
}

function announce() {
  // Tests run beside a real Home Assistant, which shouldn't discover them.
  if (process.env.PLEATVIEW_MDNS === "0") return;
  advertiser.announce({ id, name: name(), port: PORT, api: API_VERSION, version }).catch((e) => console.warn("PleatView: mDNS:", e.message));
}

const api = createApi(
  {
    info: () => ({ id, name: name(), manufacturer, model, app_version: version, api_version: API_VERSION }),
    stateJson: () => hub.stateJson(),
    onState: (listener) => hub.onState(listener),
    dispatch: (command) => hub.dispatch(command),
    config: () => config,
    updateConfig,
    // Sign-ins for dashboards behind a password come later on Linux: nothing is waiting for one.
    submitSignIn: () => false,
    signIns: () => [],
    forgetSignIns: () => 0,
    setupPage: () => readFileSync(join(assets, "setup.html")),
  },
  pairing,
);

let display = null;

app.whenReady().then(() => {
  display = new Display({ hub, config: () => config, setup: setupInfo, assets, quit: () => app.quit() });
  hub.display = display;
  display.open();
  api.listen(PORT, "0.0.0.0", () => console.log(`PleatView ${version}: control API on port ${PORT}`));
  api.on("error", (e) => console.error(`PleatView: can't listen on port ${PORT}: ${e.message}`));
  announce();
});

app.on("before-quit", () => {
  api.close();
  advertiser.shutdown();
});
app.on("window-all-closed", () => app.quit());
