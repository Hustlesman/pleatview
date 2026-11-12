import assert from "node:assert/strict";
import { test } from "node:test";
import { Config } from "../src/config.js";
import { DETECT_SCRIPT, FileVault, KIND, SignInFlow, SignIns, siteName } from "../src/signins.js";

function memoryStore() {
  const files = new Map();
  return { files, read: (name) => files.get(name) ?? null, write: (name, text) => files.set(name, text) };
}

test("sign-ins are sealed on disk and found only for their own site", () => {
  const store = memoryStore();
  const signIns = new SignIns(store);
  signIns.save({ kind: KIND.FORM, site: "http://grafana.lan:3000", username: "kiosk", password: "s3cret-pass" });
  signIns.save({ kind: KIND.PROMPT, site: "nas.lan", realm: "NAS", username: "tv", password: "p", dashboard: "http://nas.lan:5000" });
  assert.ok(!store.files.get("signins.sealed").includes("s3cret-pass"));
  assert.match(store.files.get("signins.key"), /^[0-9a-f]{64}$/);

  const again = new SignIns(store);
  assert.equal(again.find(KIND.FORM, "HTTP://GRAFANA.LAN:3000")?.password, "s3cret-pass");
  assert.equal(again.find(KIND.FORM, "http://grafana.lan:3001"), null);
  assert.equal(again.find(KIND.PROMPT, "nas.lan", "NAS", "http://nas.lan:5000")?.username, "tv");
  assert.equal(again.find(KIND.PROMPT, "nas.lan", "NAS", "http://other.lan"), null, "a prompt's sign-in stays with its dashboard");
  assert.deepEqual(again.describe(), [
    { kind: "form", site: "http://grafana.lan:3000", username: "kiosk" },
    { kind: "prompt", site: "nas.lan", username: "tv" },
  ]);
  assert.equal(again.forget("nas.lan"), 1);
  assert.equal(again.forget(), 1);
  assert.equal(new SignIns(store).count, 0);
});

test("a lost key starts over instead of failing", () => {
  const store = memoryStore();
  new SignIns(store).save({ kind: KIND.FORM, site: "http://a.lan", username: "u", password: "p" });
  store.files.set("signins.key", "00".repeat(32));
  assert.equal(new SignIns(store).count, 0);
  const vault = new FileVault(store);
  assert.equal(vault.open(vault.seal(Buffer.from("round trip"))).toString(), "round trip");
});

test("a site's own name from its page title", () => {
  assert.equal(siteName("Login | Proxmox Virtual Environment"), "Proxmox Virtual Environment");
  assert.equal(siteName("Sign in to Grafana"), "Grafana");
  assert.equal(siteName("Grafana - Log in"), "Grafana");
  assert.equal(siteName("Login"), null);
  assert.equal(siteName("http://nas.lan/login"), null);
});

/** The dashboard's page, the clock and the timers, under the test's control. */
function harness() {
  const page = { form: false, filled: [] };
  const queued = [];
  let clock = 0;
  const requests = [];
  const signIns = new SignIns(memoryStore());
  const flow = new SignInFlow({
    signIns,
    onRequest: (request) => requests.push(request),
    evaluate: async (js) => {
      if (js === DETECT_SCRIPT) return page.form ? "form" : "";
      page.filled.push(js);
      return "submitted";
    },
    title: () => "Login | Grafana",
    now: () => clock,
    later: (fn, ms) => queued.push({ fn, at: clock + ms }),
  });
  flow.config = new Config({ haUrl: "http://ha.lan:8123", dashboards: [{ title: "Energy", path: "http://grafana.lan:3000/d/energy" }] });
  const settle = () => new Promise((resolve) => setImmediate(resolve));
  async function tick(ms) {
    clock += ms;
    for (;;) {
      await settle();
      const due = queued.filter((q) => q.at <= clock);
      if (!due.length) return;
      for (const q of due) queued.splice(queued.indexOf(q), 1), q.fn();
    }
  }
  return { page, flow, signIns, requests, tick, settle };
}

test("a login form asks once, and its sign-in is saved when the site accepts it", async () => {
  const { page, flow, signIns, requests, tick } = harness();
  flow.target = "http://grafana.lan:3000/d/energy";
  flow.onNavigate();
  flow.onPageStarted();
  page.form = true;
  flow.onPageShown("http://grafana.lan:3000/login", true);
  await tick(0);
  assert.deepEqual(requests.at(-1), {
    kind: "form", site: "http://grafana.lan:3000", realm: "", title: "Energy", failed: false, address: "grafana.lan:3000",
  });

  assert.equal(flow.apply("kiosk", "pass", true), true);
  assert.equal(requests.at(-1), null);
  assert.match(page.filled.at(-1), /"http:\/\/grafana\.lan:3000", "kiosk", "pass"/);
  page.form = false; // the site took it and showed the dashboard
  flow.onPageStarted();
  flow.onPageShown("http://grafana.lan:3000/d/energy", true);
  await tick(0);
  assert.equal(signIns.find(KIND.FORM, "http://grafana.lan:3000")?.username, "kiosk");

  // Next time the saved sign-in fills the form by itself.
  flow.onNavigate();
  flow.onPageStarted();
  page.form = true;
  flow.onPageShown("http://grafana.lan:3000/login", true);
  await tick(0);
  assert.equal(page.filled.length, 2);
  assert.equal(requests.at(-1), null);
  assert.equal(flow.apply("x", "y", true), false, "nothing is asking");
});

test("a form still there after the sign-in counts as rejected, and asks again", async () => {
  const { page, flow, requests, tick } = harness();
  flow.target = "http://grafana.lan:3000/d/energy";
  flow.onPageStarted();
  page.form = true;
  flow.onPageShown("http://grafana.lan:3000/login", true);
  await tick(0);
  flow.apply("kiosk", "wrong", true);
  await tick(9000); // the form is still on the page well after sending
  assert.equal(requests.at(-1)?.failed, true);
});

test("Home Assistant's own pages aren't searched for a form", async () => {
  const { page, flow, requests, tick } = harness();
  page.form = true;
  flow.onPageShown("http://ha.lan:8123/lovelace/0", true);
  await tick(20_000);
  assert.deepEqual(requests, []);
});

test("password prompts use the saved sign-in once, then ask", async () => {
  const { flow, signIns, requests, settle } = harness();
  flow.target = "http://nas.lan:5000/";
  const answers = [];
  const handler = { proceed: (u, p) => answers.push([u, p]), cancel: () => answers.push("cancel") };
  flow.onPrompt(handler, "nas.lan", "NAS");
  assert.equal(requests.at(-1).kind, "prompt");
  assert.equal(requests.at(-1).title, "nas.lan");
  flow.apply("tv", "right", true);
  assert.deepEqual(answers, [["tv", "right"]]);
  flow.onPageLoaded();
  await settle();
  assert.equal(signIns.find(KIND.PROMPT, "nas.lan", "NAS", "http://nas.lan:5000")?.password, "right");

  flow.onNavigate();
  flow.onPrompt(handler, "nas.lan", "NAS");
  assert.deepEqual(answers.at(-1), ["tv", "right"]);
  flow.onPrompt(handler, "nas.lan", "NAS"); // asked again on the same page: rejected
  assert.equal(requests.at(-1).failed, true);
  flow.dismiss();
  assert.equal(answers.at(-1), "cancel");
  flow.onPrompt(handler, "nas.lan", "NAS");
  assert.equal(answers.at(-1), "cancel", "not asked again on this page after Not now");
});
