// Sign-ins for dashboards behind a password prompt (HTTP basic auth) or a login form, as in
// the Android app (SignIns.kt and SignInFlow.kt): kept on this screen only, encrypted, each
// offered only to the site it was made for, and saved only once the site accepts it.
import { createCipheriv, createDecipheriv, randomBytes } from "node:crypto";
import { isHttpUrl, originOf } from "./config.js";

export const KIND = Object.freeze({ PROMPT: "prompt", FORM: "form" });

/** A page asking for a username and password; `site` is a prompt's host or a form's origin. */
export function signInRequest(kind, site, realm, title, failed = false) {
  return { kind, site, realm, title, failed, address: site.replace(/^[a-z]+:\/\//i, "") };
}

/** Same kind, site (whatever its capitals) and realm, and the prompt's dashboard if it has one. */
function matches(signIn, kind, site, realm, dashboard = "") {
  return signIn.kind === kind && signIn.site.toLowerCase() === site.toLowerCase() && signIn.realm === realm &&
    (!signIn.dashboard || signIn.dashboard.toLowerCase() === dashboard.toLowerCase());
}

/**
 * AES-256-GCM with a key of this screen's own, made on first use and kept in a file only its
 * user can read, apart from the sign-ins. (A Linux kiosk has no key store such as Android's.)
 */
export class FileVault {
  constructor(store, name = "signins.key") {
    this.store = store;
    this.name = name;
  }

  key() {
    const saved = this.store.read(this.name);
    if (saved && /^[0-9a-f]{64}$/.test(saved.trim())) return Buffer.from(saved.trim(), "hex");
    const key = randomBytes(32);
    this.store.write(this.name, key.toString("hex"));
    return key;
  }

  seal(plain) {
    const iv = randomBytes(12);
    const cipher = createCipheriv("aes-256-gcm", this.key(), iv);
    const body = Buffer.concat([cipher.update(plain), cipher.final()]);
    return Buffer.concat([iv, body, cipher.getAuthTag()]);
  }

  open(sealed) {
    const decipher = createDecipheriv("aes-256-gcm", this.key(), sealed.subarray(0, 12));
    decipher.setAuthTag(sealed.subarray(sealed.length - 16));
    return Buffer.concat([decipher.update(sealed.subarray(12, sealed.length - 16)), decipher.final()]);
  }
}

/** The sign-ins saved on this screen. */
export class SignIns {
  /** @param {{read(name: string): string|null, write(name: string, text: string): void}} store */
  constructor(store, vault = new FileVault(store), name = "signins.sealed") {
    this.store = store;
    this.vault = vault;
    this.name = name;
    this.loaded = null;
  }

  find(kind, site, realm = "", dashboard = "") {
    return this.entries().find((s) => matches(s, kind, site, realm, dashboard)) ?? null;
  }

  /** Replaces the sign-in for the same place. */
  save(signIn) {
    const list = this.entries().filter((s) => !matches(s, signIn.kind, signIn.site, signIn.realm, signIn.dashboard ?? ""));
    list.push({ dashboard: "", realm: "", ...signIn });
    this.persist(list);
  }

  /** Forgets the sign-ins for a site, or all of them; returns how many. */
  forget(site = null) {
    const list = this.entries();
    const kept = site === null ? [] : list.filter((s) => s.site.toLowerCase() !== site.toLowerCase());
    if (kept.length !== list.length) this.persist(kept);
    return list.length - kept.length;
  }

  /** What's saved, without the passwords. */
  describe() {
    return this.entries().map(({ kind, site, username }) => ({ kind, site, username }));
  }

  get count() {
    return this.entries().length;
  }

  entries() {
    if (!this.loaded) this.loaded = this.load();
    return this.loaded;
  }

  load() {
    const sealed = this.store.read(this.name);
    if (!sealed) return [];
    try {
      const list = JSON.parse(this.vault.open(Buffer.from(sealed.trim(), "hex")).toString("utf8"));
      return Array.isArray(list) ? list.filter((s) => s && typeof s.site === "string" && typeof s.password === "string") : [];
    } catch {
      return []; // a lost or replaced key: start over rather than fail
    }
  }

  persist(list) {
    this.loaded = list;
    this.store.write(this.name, list.length ? this.vault.seal(Buffer.from(JSON.stringify(list))).toString("hex") : "");
  }
}

const SIGN_IN_PREFIX = /^(sign in|log in|login|welcome) to /i;
const GENERIC = /^(please )?(sign[ -]?in|log[ -]?in|log on|welcome|authentication|authenticate|account|home)$/i;

/** A site's own name from its page title: "Login | Proxmox Virtual Environment" → "Proxmox Virtual Environment". */
export function siteName(pageTitle) {
  const title = (pageTitle ?? "").trim();
  if (!title || title.includes("://")) return null;
  const parts = title.split(/ \| | - | – | — | · |: /).map((p) => p.trim().replace(SIGN_IN_PREFIX, "").trim());
  return parts.filter((p) => p && !GENERIC.test(p)).pop()?.slice(0, 40) ?? null;
}

/** "form" when the page shows a password field. */
export const DETECT_SCRIPT = `(function () {
  function shown(el) { var r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden"; }
  return Array.prototype.some.call(document.querySelectorAll('input[type="password"]'), shown) ? "form" : "";
})()`;

/** Fills a login form and submits it, but only on its origin: the page may have moved on. */
export function fillScript(origin, username, password) {
  return `(function (origin, username, password) {
  if (location.origin !== origin) return "elsewhere";
  function shown(el) { var r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== "hidden"; }
  var fields = Array.prototype.filter.call(document.querySelectorAll("input"), shown);
  var pass = fields.filter(function (el) { return el.type === "password"; })[0];
  if (!pass) return "no form";
  var user = fields.slice(0, fields.indexOf(pass)).filter(function (el) {
    return ["text", "email", "tel", ""].indexOf(el.type) >= 0 || el.autocomplete === "username";
  }).pop();
  // The native setter and an input event, so frameworks such as React notice the new value.
  var setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
  function set(el, value) {
    el.focus();
    setter.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  if (user && username) set(user, username);
  set(pass, password);
  var scope = pass.form || document;
  var button = scope.querySelector('button[type="submit"], input[type="submit"]') ||
    Array.prototype.filter.call(scope.querySelectorAll("button"), shown)[0];
  setTimeout(function () {
    if (button) button.click();
    else if (pass.form && pass.form.requestSubmit) pass.form.requestSubmit();
    else if (pass.form) pass.form.submit();
  }, 100);
  return "submitted";
})(${JSON.stringify(origin)}, ${JSON.stringify(username)}, ${JSON.stringify(password)})`;
}

const FORM_SEARCHES = 8;
const FORM_SEARCH_INTERVAL_MS = 1500;
const VERDICT_MS = 30_000;
const SAME_PAGE_VERDICT_MS = 8000;
const REJECTED_PAUSE_MS = 10 * 60_000;

/**
 * Signs the dashboard's pages in. Password prompts and login forms get the sign-in saved for
 * that site; without one, or when it stops working, the screen asks through `onRequest`.
 */
export class SignInFlow {
  /**
   * @param {object} options
   * @param {SignIns} options.signIns
   * @param {(request: object|null) => void} options.onRequest
   * @param {(js: string) => Promise<unknown>} options.evaluate runs a script in the dashboard's page
   * @param {() => string} options.title the dashboard page's title
   * @param {() => number} [options.now] milliseconds, steadily increasing
   * @param {(fn: () => void, ms: number) => unknown} [options.later]
   */
  constructor({ signIns, onRequest, evaluate, title, now = () => performance.now(), later = setTimeout }) {
    Object.assign(this, { signIns, onRequest, evaluate, title, now, later });
    this.config = null;
    this.target = null; // the dashboard (path or URL) the screen was asked to show
    this.request = null;
    this.promptHandler = null;
    this.promptsTried = new Set();
    this.trying = null; // { signIn, save, saved, at, navigated, page }
    this.loading = false;
    this.pagesStarted = 0;
    this.rejected = new Map();
    this.dismissed = null;
    this.dismissedPrompts = new Set();
    this.search = 0;
  }

  get pending() {
    return this.request;
  }

  /** A new page starts loading in the dashboard. */
  onNavigate() {
    this.search++;
    this.promptsTried.clear();
    this.dismissedPrompts.clear();
    this.dismissed = null;
    this.promptHandler?.cancel();
    this.promptHandler = null;
    if (this.request) this.show(null);
  }

  // ---- Password prompts -----------------------------------------------------------------

  /** @param {{proceed(username: string, password: string): void, cancel(): void}} handler */
  onPrompt(handler, host, realm) {
    const key = `${host.toLowerCase()}\n${realm}`;
    if (this.dismissedPrompts.has(key)) return handler.cancel();
    const dashboard = this.dashboardOrigin();
    const saved = this.signIns.find(KIND.PROMPT, host, realm, dashboard);
    if (saved && !this.promptsTried.has(key)) {
      this.promptsTried.add(key);
      // A sign-in saved before it knew its dashboard belongs to this one once it works here.
      if (!saved.dashboard && dashboard) {
        this.trying = { signIn: { ...saved, dashboard }, save: true, saved: true, at: this.now() };
      }
      handler.proceed(saved.username, saved.password);
      return;
    }
    // A second prompt after a sign-in means the site rejected it.
    const attempt = this.trying;
    const rejected = this.promptsTried.has(key) ||
      Boolean(attempt && attempt.signIn.kind === KIND.PROMPT && matches(attempt.signIn, KIND.PROMPT, host, realm, dashboard));
    this.trying = null;
    this.promptHandler?.cancel();
    this.promptHandler = handler;
    this.show(signInRequest(KIND.PROMPT, host, realm, this.titleFor(host), rejected));
  }

  /** The origin of the dashboard on screen, which a password-prompt sign-in is kept for. */
  dashboardOrigin() {
    const target = this.target;
    if (!target) return "";
    return (isHttpUrl(target) ? originOf(target) : this.config?.haOrigin) ?? "";
  }

  /** The main frame finished loading: a pending password sign-in worked. */
  onPageLoaded() {
    const attempt = this.trying;
    if (!attempt || attempt.signIn.kind !== KIND.PROMPT) return;
    this.trying = null;
    if (attempt.save) this.signIns.save(attempt.signIn);
  }

  // ---- Login forms --------------------------------------------------------------------

  onPageStarted() {
    this.loading = true;
    this.pagesStarted++;
  }

  /** A page (finished loading) or a single-page app's new route is showing: look for a login form. */
  onPageShown(url, finished) {
    if (finished) this.loading = false;
    const origin = url ? originOf(url) : null;
    if (!origin) return;
    // The site answered with a new page or route. A form filled while its page was still
    // loading would otherwise count that page finishing as the answer.
    const t = this.trying;
    if (t && t.signIn.kind === KIND.FORM && !t.navigated && (!finished || this.pagesStarted > t.page)) {
      this.trying = { ...t, navigated: true };
    }
    const token = ++this.search;
    if (!isHttpUrl(url) || this.config?.isHaUrl(url)) {
      // Home Assistant: no login form here.
      const sent = this.formSent();
      if (sent) this.accepted(sent);
      if (this.request?.kind === KIND.FORM) this.show(null);
      return;
    }
    this.lookForForm(origin, token, 0);
  }

  lookForForm(origin, token, attempt) {
    this.evaluate(DETECT_SCRIPT).catch(() => "").then((result) => {
      if (token !== this.search) return;
      const form = result === "form";
      const sent = this.formSent();
      if (sent && !form && !this.loading) this.accepted(sent);
      else if (sent && form && (sent.navigated || this.now() - sent.at > SAME_PAGE_VERDICT_MS)) this.rejectedForm(sent, origin);
      else if (!sent && form) this.onForm(origin);
      else if (attempt < FORM_SEARCHES) this.later(() => this.lookForForm(origin, token, attempt + 1), FORM_SEARCH_INTERVAL_MS);
      else if (this.request?.kind === KIND.FORM) this.show(null);
    });
  }

  /** The form sign-in waiting for the site's answer, unless it was sent too long ago to tell. */
  formSent() {
    const sent = this.trying?.signIn.kind === KIND.FORM ? this.trying : null;
    if (!sent) return null;
    if (this.now() - sent.at < VERDICT_MS) return sent;
    this.trying = null;
    return null;
  }

  accepted(sent) {
    this.trying = null;
    if (sent.save) {
      this.signIns.save(sent.signIn);
      this.rejected.delete(sent.signIn.site);
    }
    if (this.request?.kind === KIND.FORM) this.show(null);
  }

  rejectedForm(sent, origin) {
    this.trying = null;
    if (sent.saved) this.rejected.set(origin, this.now());
    this.show(signInRequest(KIND.FORM, origin, "", this.titleFor(origin, this.title()), true));
  }

  /** A login form with no sign-in on its way: use the saved one, or ask. */
  onForm(origin) {
    const saved = this.signIns.find(KIND.FORM, origin);
    const rejectedAt = this.rejected.get(origin);
    if (saved && (rejectedAt === undefined || this.now() - rejectedAt > REJECTED_PAUSE_MS)) {
      this.fill(saved, false, true);
    } else if (this.dismissed !== origin) {
      this.show(signInRequest(KIND.FORM, origin, "", this.titleFor(origin, this.title()), Boolean(saved)));
    }
  }

  fill(signIn, save, saved) {
    this.trying = { signIn, save, saved, at: this.now(), navigated: false, page: this.pagesStarted };
    this.evaluate(fillScript(signIn.site, signIn.username, signIn.password)).catch(() => {});
    // A single-page app may sign in without loading a page: watch this one too.
    const token = ++this.search;
    this.later(() => this.lookForForm(signIn.site, token, 0), FORM_SEARCH_INTERVAL_MS);
  }

  // ---- From the screen or a phone ------------------------------------------------------

  /** A username and password for the current request; false if nothing is asking. */
  apply(username, password, remember) {
    const current = this.request;
    if (!current) return false;
    const dashboard = current.kind === KIND.PROMPT ? this.dashboardOrigin() : "";
    const signIn = { kind: current.kind, site: current.site, realm: current.realm, username, password, dashboard };
    this.show(null);
    if (current.kind === KIND.PROMPT) {
      const handler = this.promptHandler;
      if (!handler) return false;
      this.promptHandler = null;
      this.trying = { signIn, save: remember, saved: false, at: this.now() };
      handler.proceed(username, password);
    } else {
      this.fill(signIn, remember, false);
    }
    return true;
  }

  /** "Not now": a password prompt is cancelled; a login form stays for the keyboard. */
  dismiss() {
    const current = this.request;
    if (!current) return;
    if (current.kind === KIND.PROMPT) {
      this.dismissedPrompts.add(`${current.site.toLowerCase()}\n${current.realm}`);
      this.promptHandler?.cancel();
      this.promptHandler = null;
    } else {
      this.dismissed = current.site;
    }
    this.show(null);
  }

  show(next) {
    if (JSON.stringify(next) === JSON.stringify(this.request)) return;
    this.request = next;
    this.onRequest(next);
  }

  /**
   * What to call a site on the screen and the phone: the name of a dashboard on it, else
   * "Home Assistant", else the site's own name from its page title, else its host.
   */
  titleFor(site, pageTitle = null) {
    const host = (site.includes("://") ? hostOf(site) : site.split(":")[0]) || site;
    const onHost = (url) => hostOf(url).toLowerCase() === host.toLowerCase();
    const dashboards = this.config?.dashboards ?? [];
    const opened = dashboards.find((d) => isHttpUrl(d.path) && d.path === this.target && onHost(d.path)) ??
      dashboards.find((d) => isHttpUrl(d.path) && onHost(d.path));
    if (opened) return opened.title;
    if (this.config?.haOrigin && onHost(this.config.haOrigin)) return "Home Assistant";
    return siteName(pageTitle) ?? host;
  }
}

function hostOf(url) {
  try {
    return new URL(url).hostname;
  } catch {
    return "";
  }
}
