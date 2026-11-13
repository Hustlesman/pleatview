// PleatView for Linux: Home Assistant dashboards, media and notifications on a screen
// attached to a mini PC or a Raspberry Pi, controlled by the PleatView integration.
import { app } from "electron";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import QRCode from "qrcode";
import { API_VERSION, createApi, createCalls } from "./api.js";
import { Config } from "./config.js";
import { Connection } from "./connect.js";
import { defaultName, deviceId, hardware, lanAddress } from "./device.js";
import { Display } from "./display.js";
import { Hub } from "./hub.js";
import { Advertiser } from "./mdns.js";
import { Pairing } from "./pairing.js";
import { SignIns } from "./signins.js";
import { Store } from "./store.js";
import { BadCommand } from "./command.js";
import { Helper, RecentLog, diagnostics, newer } from "./upkeep.js";

// The last lines the app logged, for a controller's diagnostics (upkeep.js).
const recentLog = new RecentLog().capture();
const started = Math.floor(Date.now() / 1000);

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
// Restarting the computer and updating go through the package's helper, where it's set up.
const helper = new Helper();
const hub = new Hub({ appVersion: version, features: helper.ready ? ["restart_device", "update"] : [] });
// Sign-ins for dashboards behind a password: on this screen only, encrypted (signins.js).
const signIns = new SignIns(store);
const advertiser = new Advertiser();
let qr = { text: "", svg: "" };

const pairing = new Pairing({
  load: () => store.read("tokens.json"),
  save: (json) => store.write("tokens.json", json),
  onChanged: () => {
    display?.refreshSetup();
    display?.showApproval();
  },
});

const name = () => config.deviceName || defaultName();
// Protocol v2: the code to type into a controller while it hasn't let this screen in yet.
let enrolCode = null;

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
  return { name: name(), address, port: PORT, pin: pairing.pin, version, qr: qr.svg, enrol: enrolCode };
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

const info = () => ({ id, name: name(), manufacturer, model, app_version: version, api_version: API_VERSION });

/** Starts the app again: systemd does it for the kiosk (pleatview-kiosk enable), Electron otherwise. */
function startAgain() {
  console.log("PleatView: starting again");
  if (!process.env.INVOCATION_ID) app.relaunch();
  app.quit();
}

/** Upkeep's commands (docs/protocol.md, "Upkeep"); everything else goes to the display. */
function dispatch(command) {
  if (command.name === "restart" && command.what === "app") {
    console.log("PleatView: starting again, as the controller asked");
    setTimeout(startAgain, 1000); // once the answer is out
    return;
  }
  if (command.name === "restart" || command.name === "update") {
    if (!helper.ready) throw new BadCommand("not_supported");
    if (command.name === "restart") {
      console.log("PleatView: restarting the computer, as the controller asked");
      return helper.restartDevice();
    }
    if (!newer(command.version, version)) throw new BadCommand(`${version} is installed, and only newer versions are`);
    console.log(`PleatView: updating to ${command.version}, as the controller asked`);
    helper.update(command.version);
    hub.update({ update: { version: command.version, state: "waiting", error: null, at: Math.floor(Date.now() / 1000) } });
    return;
  }
  hub.dispatch(command);
}

/** Reports how the helper's update goes, and starts the new version once it's in. */
function followUpdates() {
  const status = helper.status();
  if (status && JSON.stringify(status) !== JSON.stringify(hub.state.update)) hub.update({ update: status });
  if (status?.state === "installed" && (status.at ?? 0) >= started && newer(status.version, version)) {
    console.log(`PleatView: version ${status.version} is installed; starting it`);
    clearInterval(following);
    setTimeout(startAgain, 1000);
  }
}
const following = setInterval(followUpdates, 5000);
followUpdates();

/** The home page's address, for the checks: Home Assistant's, or a full web address. */
function homeAddress() {
  if (config.renderStream && /^https?:/.test(config.renderStream)) return config.renderStream;
  if (config.haUrl) return config.haUrl;
  return /^https?:/.test(config.homeDashboard ?? "") ? config.homeDashboard : null;
}
const backend = {
  info,
  stateJson: () => hub.stateJson(),
  onState: (listener) => hub.onState(listener),
  dispatch,
  diagnostics: () => diagnostics({ home: homeAddress(), log: recentLog, dataDir: store.dir }),
  config: () => config,
  updateConfig,
  submitSignIn: (username, password, remember) => display?.signInFlow?.apply(username, password, remember) ?? false,
  signIns: () => signIns.describe(),
  forgetSignIns: (site) => (display ? display.forgetSignIns(site) : signIns.forget(site)),
  setupPage: () => readFileSync(join(assets, "setup.html")),
  approvalRequested: () => display?.showApproval(),
  enrol: (url, token, fingerprint) => connection.enrol(url, token, fingerprint),
  connectOut: (url) => connection.enrol(url),
};
const api = createApi(backend, pairing);
// Protocol v2: one connection out to a controller anywhere, once one is given.
const connection = new Connection({
  store,
  info,
  call: createCalls(backend, pairing),
  stateJson: () => hub.stateJson(),
  onState: (listener) => hub.onState(listener),
  showCode: (code, host) => {
    enrolCode = code ? { code, host } : null;
    display?.refreshSetup();
  },
});

let display = null;

app.whenReady().then(() => {
  const qrCode = (text) => QRCode.toString(text, { type: "svg", errorCorrectionLevel: "M", margin: 2 });
  display = new Display({ hub, config: () => config, setup: setupInfo, assets, quit: () => app.quit(), pairing, signIns, qrCode });
  hub.display = display;
  display.open();
  api.listen(PORT, "0.0.0.0", () => console.log(`PleatView ${version}: control API on port ${PORT}`));
  api.on("error", (e) => console.error(`PleatView: can't listen on port ${PORT}: ${e.message}`));
  announce();
  connection.start();
});

app.on("before-quit", () => {
  connection.stop();
  api.close();
  advertiser.shutdown();
});
app.on("window-all-closed", () => app.quit());
