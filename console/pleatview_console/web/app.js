// PleatView Console's web app. No libraries: the DOM is built with h(), text always as text.
"use strict";

const $ = (selector) => document.querySelector(selector);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

class ApiError extends Error {
  constructor(status, message, body = {}) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

async function api(method, path, data) {
  const options = { method, credentials: "same-origin", headers: {} };
  if (method !== "GET") options.headers["X-PleatView"] = "1";
  if (data !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(data);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new ApiError(0, "The console can't be reached.");
  }
  let body = {};
  try {
    body = await response.json();
  } catch {
    // not JSON: keep the status
  }
  if (response.status === 401 && app.me && !path.startsWith("/api/login")) {
    signedOut();
  }
  if (response.status === 403 && body.action && app.me) {
    // The account must change its one-time password or set up two-factor sign-in first.
    setTimeout(() => (body.action === "change_password" ? showChangePassword() : showTwoFactorSetup()));
  }
  if (!response.ok) throw new ApiError(response.status, body.error || `Error ${response.status}`, body);
  return body;
}

const app = {
  me: null,
  tenant: null,
  overview: null,
  screens: [],
  discovered: [],
  audit: [],
  events: null,
  installPrompt: null,
};

const ROLE_MAY = {
  operate: ["owner", "operator"],
  own: ["owner"],
};
const may = (what) => app.overview && ROLE_MAY[what].includes(app.overview.role);

// ---- Start ------------------------------------------------------------------------------

async function start() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
  window.addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    app.installPrompt = event;
    $("#install").hidden = false;
  });
  $("#install").addEventListener("click", async () => {
    if (!app.installPrompt) return;
    app.installPrompt.prompt();
    await app.installPrompt.userChoice;
    app.installPrompt = null;
    $("#install").hidden = true;
  });
  $("#logout").addEventListener("click", logout);
  $("#change-password").addEventListener("click", changePassword);
  $("#add-tenant").addEventListener("click", addTenant);
  $("#organisations").addEventListener("click", () => organisations().catch((e) => toast(e.message)));
  $("#two-factor").addEventListener("click", () => twoFactor().catch((e) => toast(e.message)));
  $("#console-settings").addEventListener("click", () => consoleSettings().catch((e) => toast(e.message)));
  $("#tenant").addEventListener("change", (event) => selectTenant(event.target.value));
  $("#dialog-cancel").addEventListener("click", () => $("#dialog").close());
  window.addEventListener("hashchange", render);

  let status;
  try {
    status = await api("GET", "/api/status");
  } catch (error) {
    page(h("div", { class: "panel narrow" }, h("h1", { text: "PleatView Console" }), h("p", { text: error.message })));
    return;
  }
  if (status.setup) showSetup();
  else if (!status.user) showLogin();
  else signedIn(status);
}

function page(...content) {
  $("#main").replaceChildren(...content.filter((part) => part != null && part !== false));
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.add("shown");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("shown"), 3000);
}

// ---- Forms and dialogs ----------------------------------------------------------------

function field({ name, label, type = "text", value = "", hint, options, autocomplete, required = true }) {
  const id = `f-${name}`;
  let input;
  if (type === "checks") {
    const chosen = new Set((value || []).map(String));
    input = h("fieldset", { id, class: "checks" }, options.length
      ? options.map(([v, text]) => h("label", { class: "check" },
        h("input", { type: "checkbox", name, value: v, checked: chosen.has(String(v)) }), " ", text))
      : h("p", { class: "hint", text: "Nothing to choose yet." }));
  } else if (options) {
    input = h("select", { id, name }, options.map(([v, text]) => h("option", { value: v, selected: String(v) === String(value), text })));
  } else {
    input = h("input", { id, name, type, value, autocomplete, required, spellcheck: "false" });
  }
  return [h("label", { for: id, text: label }), input, hint ? h("p", { class: "hint", text: hint }) : null];
}

function values(form) {
  const out = {};
  for (const el of form.querySelectorAll("input, select")) {
    if (el.type === "checkbox") {
      out[el.name] = out[el.name] || [];
      if (el.checked) out[el.name].push(el.value);
    } else if (el.name) {
      out[el.name] = el.value;
    }
  }
  return out;
}

// A form on the page (setup, sign-in): submit() gets the values; its error shows under the form.
function formPage(title, fields, button, submit) {
  const error = h("p", { class: "error", role: "alert" });
  const go = h("button", { type: "submit", text: button });
  const form = h("form", {}, fields.map(field), error, h("div", { class: "actions" }, go));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    go.disabled = true;
    try {
      await submit(values(form));
    } catch (e) {
      error.textContent = e.message;
    } finally {
      go.disabled = false;
    }
  });
  page(h("div", { class: "panel narrow" }, h("img", { class: "logo", src: "/icon.svg", alt: "", width: 64, height: 64 }), h("h1", { text: title }), form));
  form.querySelector("input")?.focus();
}

// A dialog: submit() gets the values and closes it by resolving; its error stays in the dialog.
function dialog({ title, fields = [], text, ok = "OK", danger = false, submit }) {
  const dlg = $("#dialog");
  const form = $("#dialog-form");
  const okButton = $("#dialog-ok");
  $("#dialog-title").textContent = title;
  $("#dialog-error").textContent = "";
  $("#dialog-body").replaceChildren(...[text ? h("p", { text }) : null, ...fields.flatMap(field)].filter(Boolean));
  okButton.textContent = ok;
  $("#dialog-cancel").hidden = false;
  okButton.className = danger ? "danger" : "";
  form.onsubmit = async (event) => {
    event.preventDefault();
    okButton.disabled = true;
    $("#dialog-error").textContent = "";
    try {
      await submit(values(form));
      dlg.close();
    } catch (e) {
      $("#dialog-error").textContent = e.message;
    } finally {
      okButton.disabled = false;
    }
  };
  dlg.showModal();
  form.querySelector("#dialog-body input, #dialog-body select")?.focus();
}

// ---- Signing in -------------------------------------------------------------------------

function showSetup() {
  $("#bar").hidden = true;
  formPage("Set up PleatView Console", [
    { name: "name", label: "Your name", autocomplete: "name" },
    { name: "username", label: "User name", autocomplete: "username" },
    { name: "password", label: "Password", type: "password", autocomplete: "new-password", hint: "At least 12 characters." },
    { name: "tenant", label: "Organisation", hint: "Your company, a customer, or your household. You can add more later." },
    { name: "site", label: "First site", value: "Main site", hint: "A building, a branch or a home." },
  ], "Set up", async (data) => signedIn(await api("POST", "/api/setup", { ...data, timezone: browserZone() })));
}

function showLogin(withCode = false, values = {}) {
  $("#bar").hidden = true;
  const fields = [
    { name: "username", label: "User name", autocomplete: "username", value: values.username || "" },
    { name: "password", label: "Password", type: "password", autocomplete: "current-password", value: values.password || "" },
  ];
  if (withCode) {
    fields.push({ name: "code", label: "Code", autocomplete: "one-time-code",
      hint: "The 6 digits in your authenticator app, or one of your recovery codes." });
  }
  formPage("Sign in", fields, "Sign in", async (data) => {
    try {
      signedIn(await api("POST", "/api/login", data));
    } catch (error) {
      if (error.body?.two_factor && !withCode) {
        showLogin(true, data);
        $("#f-code").focus();
        return;
      }
      throw error;
    }
  });
}

function signedIn(me) {
  app.me = me;
  if (me.user.must_change) return showChangePassword();
  if (me.require_2fa && !me.user.two_factor) return showTwoFactorSetup();
  $("#bar").hidden = false;
  $("#username").textContent = me.user.name;
  $("#add-tenant").hidden = !me.user.admin;
  $("#console-settings").hidden = !me.user.admin;
  $("#organisations").hidden = !me.user.admin;
  const select = $("#tenant");
  select.replaceChildren(...me.tenants.map((t) => h("option", { value: t.id, text: t.name })));
  select.hidden = me.tenants.length < 2;
  const remembered = localStorage.getItem("tenant");
  const tenant = me.tenants.find((t) => t.id === remembered) || me.tenants[0];
  if (tenant) {
    select.value = tenant.id;
    selectTenant(tenant.id);
  } else {
    page(h("div", { class: "panel narrow" }, h("h1", { text: "No organisation yet" }),
      h("p", { text: "Ask a console administrator to add you to an organisation." })));
  }
}

function signedOut() {
  app.me = null;
  app.tenant = null;
  app.events?.close();
  showLogin();
}

async function logout() {
  $("#usermenu").open = false;
  try {
    await api("POST", "/api/logout");
  } finally {
    signedOut();
  }
}

// First sign-in with a one-time password: the account's own password before anything else.
function showChangePassword() {
  $("#bar").hidden = true;
  formPage("Choose your password", [
    { name: "current", label: "One-time password", type: "password", autocomplete: "current-password",
      hint: "The password you were given." },
    { name: "password", label: "Your new password", type: "password", autocomplete: "new-password", hint: "At least 12 characters." },
  ], "Save", async (data) => signedIn(await api("POST", "/api/password", data)));
}

// Two-factor sign-in, set up on a page (when the console requires it) or in a dialog.
function showTwoFactorSetup() {
  $("#bar").hidden = true;
  formPage("Set up two-factor sign-in", [
    { name: "password", label: "Your password", type: "password", autocomplete: "current-password",
      hint: "This console asks for a code from an authenticator app at every sign-in." },
  ], "Next", async (data) => {
    const secret = await api("POST", "/api/2fa/setup", data);
    twoFactorSecretPage(secret);
  });
}

function secretBlock(secret) {
  return h("div", {},
    h("p", { text: "Add PleatView Console to an authenticator app: open the link on the phone that has the app, or enter this key in it." }),
    h("p", {}, h("code", { class: "secret", text: secret.secret.match(/.{1,4}/g).join(" ") })),
    h("p", {}, h("a", { href: secret.uri, text: "Open in an authenticator app" })));
}

function twoFactorSecretPage(secret) {
  formPage("Set up two-factor sign-in", [
    { name: "code", label: "Code from the app", autocomplete: "one-time-code" },
  ], "Turn on", async (data) => {
    const result = await api("POST", "/api/2fa/enable", data);
    recoveryPage(result.recovery_codes, async () => signedIn(await api("GET", "/api/status")));
  });
  $("#main form").prepend(secretBlock(secret));
}

function recoveryList(codes) {
  return h("div", {},
    h("p", { text: "Recovery codes: each works once instead of a code from the app, if the phone is lost. Keep them somewhere safe, such as a password manager. They aren't shown again." }),
    h("pre", { class: "codes", text: codes.join("\n") }));
}

function recoveryPage(codes, done) {
  page(h("div", { class: "panel narrow" }, h("h1", { text: "Two-factor sign-in is on" }), recoveryList(codes),
    h("div", { class: "actions" }, h("button", { type: "button", onclick: done, text: "I've saved them" }))));
}

async function twoFactor() {
  $("#usermenu").open = false;
  const accountInfo = await api("GET", "/api/account");
  if (!accountInfo.user.two_factor) {
    dialog({
      title: "Two-factor sign-in",
      text: "Sign-in will also ask for a code from an authenticator app on your phone.",
      fields: [{ name: "password", label: "Your password", type: "password", autocomplete: "current-password" }],
      ok: "Next",
      submit: async (data) => {
        const secret = await api("POST", "/api/2fa/setup", data);
        setTimeout(() => dialog({
          title: "Two-factor sign-in",
          fields: [{ name: "code", label: "Code from the app", autocomplete: "one-time-code" }],
          ok: "Turn on",
          submit: async (codeData) => {
            const result = await api("POST", "/api/2fa/enable", codeData);
            app.me.user.two_factor = true;
            setTimeout(() => dialog({ title: "Two-factor sign-in is on", ok: "I've saved them", submit: async () => {} }));
            setTimeout(() => $("#dialog-body").append(recoveryList(result.recovery_codes)));
            $("#dialog-cancel").hidden = true;
          },
        }));
        setTimeout(() => $("#dialog-body").prepend(secretBlock(secret)));
      },
    });
    return;
  }
  dialog({
    title: "Two-factor sign-in is on",
    text: `${accountInfo.recovery_left} recovery code${accountInfo.recovery_left === 1 ? "" : "s"} left. Enter your password for new recovery codes${accountInfo.require_2fa ? "." : ", or to turn two-factor sign-in off."}`,
    fields: [
      { name: "password", label: "Your password", type: "password", autocomplete: "current-password" },
      { name: "what", label: "Do", options: [["recovery", "Make new recovery codes"], ...(accountInfo.require_2fa ? [] : [["off", "Turn two-factor sign-in off"]])] },
    ],
    ok: "Go",
    submit: async (data) => {
      if (data.what === "off") {
        await api("POST", "/api/2fa/disable", { password: data.password });
        app.me.user.two_factor = false;
        toast("Two-factor sign-in is off");
        return;
      }
      const result = await api("POST", "/api/2fa/recovery", { password: data.password });
      setTimeout(() => {
        dialog({ title: "New recovery codes", ok: "I've saved them", submit: async () => {} });
        $("#dialog-body").append(recoveryList(result.recovery_codes));
      });
    },
  });
}

async function consoleSettings() {
  $("#usermenu").open = false;
  const current = await api("GET", "/api/settings");
  dialog({
    title: "Console settings",
    fields: [{ name: "require_2fa", label: "Two-factor sign-in", value: String(current.require_2fa),
      options: [["false", "Each person chooses"], ["true", "Required for everyone"]] }],
    ok: "Save",
    submit: async (data) => {
      await api("POST", "/api/settings", { require_2fa: data.require_2fa === "true" });
      toast("Saved");
    },
  });
}

function changePassword() {
  $("#usermenu").open = false;
  dialog({
    title: "Change password",
    fields: [
      { name: "current", label: "Current password", type: "password", autocomplete: "current-password" },
      { name: "password", label: "New password", type: "password", autocomplete: "new-password", hint: "At least 12 characters. You stay signed in here; other sessions end." },
    ],
    ok: "Change",
    submit: async (data) => {
      await api("POST", "/api/password", data);
      toast("Password changed");
    },
  });
}

function addTenant() {
  $("#usermenu").open = false;
  dialog({
    title: "Add an organisation",
    fields: [
      { name: "name", label: "Name", hint: "A customer or another organisation. Its screens, people and logs are kept apart from every other." },
      { name: "site", label: "First site", value: "Main site" },
    ],
    ok: "Add",
    submit: async (data) => {
      const result = await api("POST", "/api/tenants", { ...data, timezone: browserZone() });
      localStorage.setItem("tenant", result.id);
      signedIn({ user: result.user, tenants: result.tenants });
    },
  });
}

// ---- The organisation -----------------------------------------------------------------

async function selectTenant(id) {
  app.tenant = id;
  app.renderers = null;
  localStorage.setItem("tenant", id);
  app.events?.close();
  try {
    app.overview = await api("GET", `/api/t/${id}`);
    await loadScreens();
  } catch (error) {
    page(h("p", { class: "error", text: error.message }));
    return;
  }
  app.events = new EventSource(`/api/t/${id}/events`);
  app.events.onmessage = (message) => onEvent(JSON.parse(message.data));
  render();
}

async function loadScreens() {
  const [data, pages] = await Promise.all([
    api("GET", `/api/t/${app.tenant}/screens`),
    api("GET", `/api/t/${app.tenant}/pages`),
  ]);
  app.screens = data.screens;
  app.discovered = data.discovered;
  app.pages = pages.pages;
}

let discoveredTimer = 0;
function onEvent(event) {
  if (event.type === "screen") {
    const index = app.screens.findIndex((s) => s.id === event.screen.id);
    if (index >= 0) app.screens[index] = event.screen;
    else app.screens.push(event.screen);
    const card = document.querySelector(`[data-screen="${event.screen.id}"]`);
    if (card && card.dataset.site === String(event.screen.site_id)) card.replaceWith(screenCard(event.screen));
    else if (tab() === "screens") render();
  } else if (event.type === "removed") {
    app.screens = app.screens.filter((s) => s.id !== event.id);
    if (tab() === "screens") render();
  } else if (event.type === "media") {
    if (tab() === "content") refreshMedia();
  } else if (event.type === "discovered") {
    clearTimeout(discoveredTimer);
    discoveredTimer = setTimeout(async () => {
      await loadScreens().catch(() => {});
      if (tab() === "screens") render();
    }, 500);
  }
}

const tab = () => (location.hash.replace(/^#\//, "") || "screens").split("/")[0];

function render() {
  if (!app.overview) return;
  const current = tab();
  for (const link of document.querySelectorAll("#tabs a")) {
    if (link.dataset.tab === current) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  $("#members-tab").hidden = !may("own");
  if (current === "sites") renderSites();
  else if (current === "members") renderMembers();
  else if (current === "groups") renderGroups();
  else if (current === "content") renderContent();
  else if (current === "schedules") renderSchedules();
  else if (current === "activity") renderActivity();
  else renderScreens();
}

// ---- Screens ----------------------------------------------------------------------------

function describe(screen) {
  if (screen.rejected) return "The screen no longer accepts the console. Pair it again.";
  if (!screen.online) return screen.seen ? `Offline since ${when(screen.seen)}` : "Not reached yet";
  const state = screen.state || {};
  if (state.power === "standby") return "Standby";
  if (state.media) return `Playing ${state.media.title || state.media.url}`;
  if (state.view === "setup") return "Showing its setup screen";
  if (!state.foreground) return "In another app";
  return `Showing ${state.dashboard || state.url || "a page"}`;
}

function dotClass(screen) {
  if (screen.rejected) return "dot bad";
  if (!screen.online) return "dot";
  return screen.state?.power === "standby" ? "dot standby" : "dot on";
}

function screenCard(screen) {
  const operate = may("operate");
  const standby = screen.state?.power === "standby";
  const send = (command, params = {}) => async () => {
    try {
      await api("POST", `/api/t/${app.tenant}/screens/${screen.id}/command`, { command, ...params });
      toast(`Sent to ${screen.name}`);
    } catch (error) {
      toast(error.message);
    }
  };
  return h("article", { class: "card", "data-screen": screen.id, "data-site": screen.site_id },
    h("div", { class: "card-head" },
      h("span", { class: dotClass(screen), title: screen.online ? "Online" : "Offline" }),
      h("strong", { text: screen.name }),
      screen.managed_by === "home_assistant" ? h("span", { class: "badge", text: "Home Assistant", title: "Home Assistant manages this screen's settings; the console sends it commands." }) : null),
    h("div", { class: "meta", text: [screen.model, screen.app_version && `PleatView ${screen.app_version}`, screen.host].filter(Boolean).join(" · ") }),
    h("div", { text: describe(screen) }),
    screen.schedule ? h("div", { class: "meta", text: `Schedule: ${screen.schedule}` }) : null,
    operate ? h("div", { class: "buttons" },
      h("button", { type: "button", onclick: () => showPage(screen), text: "Show a page" }),
      h("button", { type: "button", onclick: () => playMedia(screen), text: "Play" }),
      h("button", { type: "button", onclick: () => notify(screen), text: "Notify" }),
      h("button", { type: "button", class: "quiet", onclick: send("home"), text: "Home" }),
      h("button", { type: "button", class: "quiet", onclick: send("reload"), text: "Reload" }),
      h("button", { type: "button", class: "quiet", onclick: send(standby ? "turn_on" : "turn_off"), text: standby ? "Wake" : "Standby" }),
      h("button", { type: "button", class: "quiet", onclick: () => editScreen(screen), text: "Edit" }),
    ) : null);
}

function renderScreens() {
  const operate = may("operate");
  const sections = [];
  sections.push(h("div", { class: "section-head" },
    h("h1", { text: "Screens" }),
    operate ? h("button", { type: "button", class: "quiet", onclick: addByAddress, text: "Add by address" }) : null));

  if (operate && app.discovered.length) {
    sections.push(h("section", { class: "panel" },
      h("h3", { text: "Found on the network" }),
      app.discovered.map((found) => h("div", { class: "row" },
        h("span", {}, h("strong", { text: found.name }), " ", h("span", { class: "meta", text: `${found.host} · PleatView ${found.version}` })),
        h("button", { type: "button", onclick: () => pair(found), text: "Pair" })))));
  }

  for (const site of app.overview.sites) {
    const screens = app.screens.filter((s) => s.site_id === site.id);
    sections.push(h("div", { class: "section-head" }, h("h3", { text: site.name })));
    sections.push(screens.length
      ? h("div", { class: "grid" }, screens.map(screenCard))
      : h("p", { class: "empty", text: operate ? "No screens here yet. Pair one found on the network, or add one by its address." : "No screens here yet." }));
  }
  page(...sections);
}

const siteOptions = () => app.overview.sites.map((s) => [s.id, s.name]);

function pinField() {
  return { name: "pin", label: "PIN", autocomplete: "off", hint: "The 6 digits on the screen's setup page or in its menu (press Back on the dashboard)." };
}

function pair(found) {
  dialog({
    title: `Pair ${found.name}`,
    fields: [pinField(), { name: "site_id", label: "Site", options: siteOptions() }],
    ok: "Pair",
    submit: async (data) => {
      await api("POST", `/api/t/${app.tenant}/screens`, { device_id: found.id, pin: data.pin.trim(), site_id: Number(data.site_id) });
      await loadScreens();
      render();
      toast(`${found.name} paired`);
    },
  });
}

function addByAddress() {
  dialog({
    title: "Add a screen by address",
    fields: [
      { name: "host", label: "IP address", hint: "Shown on the screen's setup page, such as 192.168.1.20." },
      { name: "port", label: "Port", value: "8765", type: "number" },
      pinField(),
      { name: "site_id", label: "Site", options: siteOptions() },
    ],
    ok: "Pair",
    submit: async (data) => {
      await api("POST", `/api/t/${app.tenant}/screens`, {
        host: data.host.trim(), port: Number(data.port), pin: data.pin.trim(), site_id: Number(data.site_id),
      });
      await loadScreens();
      render();
      toast("Screen paired");
    },
  });
}

function command(screen, title, fields, ok, build) {
  dialog({
    title: `${title}: ${screen.name}`,
    fields,
    ok,
    submit: async (data) => {
      await api("POST", `/api/t/${app.tenant}/screens/${screen.id}/command`, build(data));
      toast(`Sent to ${screen.name}`);
    },
  });
}

const seconds = (text) => (text.trim() ? Number(text) : undefined);

function showPage(screen) {
  command(screen, "Show a page", [
    { name: "url", label: "Address", type: "url", hint: "Any web page, starting with https:// or http://." },
    { name: "duration", label: "For how long (seconds)", type: "number", required: false, hint: "Empty: until something else is shown." },
  ], "Show", (data) => ({ command: "show_url", url: data.url.trim(), duration: seconds(data.duration) }));
}

function playMedia(screen) {
  command(screen, "Play", [
    { name: "url", label: "Address of the video, audio or stream", type: "url", hint: "HLS, DASH, MP4, MP3, RTSP and more." },
    { name: "title", label: "Title", required: false },
  ], "Play", (data) => ({ command: "play_media", url: data.url.trim(), title: data.title.trim() || undefined }));
}

function notify(screen) {
  command(screen, "Notify", [
    { name: "title", label: "Title", required: false },
    { name: "message", label: "Message" },
    { name: "duration", label: "Seconds on screen", type: "number", value: "10", required: false },
  ], "Send", (data) => ({ command: "notify", title: data.title.trim() || undefined, message: data.message, duration: seconds(data.duration) }));
}

async function editScreen(screen) {
  if (!app.renderers) await loadRenderers();
  const own = screen.managed_by !== "home_assistant";
  const fields = [
    { name: "name", label: "Name", value: screen.name },
    { name: "site_id", label: "Site", options: siteOptions(), value: screen.site_id },
  ];
  if (own) {
    fields.push(
      { name: "home_page", label: "Home page", value: screen.home_page ?? "",
        options: [["", "A web address (below)"], ...app.pages.map((p) => [p.id, p.name])] },
      { name: "home", label: "Web address", type: "url", required: false, value: screen.home_page ? "" : screen.config.home_dashboard || "",
        hint: screen.home_without_ha || !screen.online
          ? "What the screen shows, and comes back to: any web page. Empty: its setup screen."
          : "Needs PleatView 1.0.17 or later on this screen." },
      { name: "zoom", label: "Zoom (%)", type: "number", required: false, value: screen.config.zoom ?? "",
        hint: "Lower fits more on the screen. Empty: 100." });
    if (screen.plays_stream || screen.renderer_id) {
      fields.push({ name: "renderer_id", label: "Rendered by", value: screen.renderer_id ?? "", required: false,
        options: [["", "The screen itself"], ...(app.renderers || []).map((r) => [r.id, r.name])],
        hint: "A renderer draws the home page and sends the screen a video: for screens that can't show pages, such as PleatView Light." });
    }
  }
  dialog({
    title: `Edit ${screen.name}`,
    fields,
    ok: "Save",
    submit: async (data) => {
      const change = { name: data.name.trim(), site_id: Number(data.site_id) };
      if (own) {
        change.home_page = data.home_page ? Number(data.home_page) : null;
        if ("renderer_id" in data) change.renderer_id = data.renderer_id ? Number(data.renderer_id) : null;
        change.home = data.home.trim();
        change.zoom = data.zoom.trim() ? Number(data.zoom) : null;
      }
      await api("PATCH", `/api/t/${app.tenant}/screens/${screen.id}`, change);
      await loadScreens();
      render();
    },
  });
  // Removing lives in the same dialog, as a second step.
  $("#dialog-body").append(h("p", {},
    h("button", { type: "button", class: "danger", onclick: () => removeScreen(screen), text: "Remove this screen" })));
}

function removeScreen(screen) {
  $("#dialog").close();
  dialog({
    title: `Remove ${screen.name}?`,
    text: "The console unpairs from the screen and forgets it. Home Assistant and other controllers paired with it keep working.",
    ok: "Remove",
    danger: true,
    submit: async () => {
      await api("DELETE", `/api/t/${app.tenant}/screens/${screen.id}`);
      app.screens = app.screens.filter((s) => s.id !== screen.id);
      render();
      toast(`${screen.name} removed`);
    },
  });
}

// ---- Sites ------------------------------------------------------------------------------

function browserZone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

function zoneOptions(current) {
  const zones = Intl.supportedValuesOf ? Intl.supportedValuesOf("timeZone") : [current || "UTC"];
  return [...new Set(["UTC", current || browserZone(), ...zones])].map((z) => [z, z.replaceAll("_", " ")]);
}

async function loadRenderers() {
  try {
    const data = await api("GET", `/api/t/${app.tenant}/renderers`);
    app.renderers = data.renderers;
    app.discoveredRenderers = data.discovered;
  } catch {
    app.renderers = app.renderers || [];
    app.discoveredRenderers = [];
  }
}

function renderersSection() {
  const operate = may("operate");
  const rows = (app.renderers || []).map((r) => h("div", { class: "row" },
    h("span", {}, h("strong", { text: r.name }), " ",
      h("span", { class: "meta", text: [r.address, r.online ? `${r.screens}/${r.max_screens} screens` : "not reached",
        r.load?.cpu != null ? `processor ${Math.round(r.load.cpu * 100)} %` : null].filter(Boolean).join(" · ") })),
    operate ? h("button", { type: "button", class: "danger", onclick: () => removeRenderer(r), text: "Remove" }) : null));
  const found = operate ? (app.discoveredRenderers || []).map((r) => h("div", { class: "row" },
    h("span", {}, h("strong", { text: r.name }), " ", h("span", { class: "meta", text: `${r.host}:${r.port} · found on the network` })),
    h("button", { type: "button", onclick: () => pairRenderer(r), text: "Pair" }))) : [];
  return [
    h("div", { class: "section-head" }, h("h1", { text: "Renderers" }),
      operate ? h("button", { type: "button", class: "quiet", onclick: () => pairRenderer(null), text: "Add by address" }) : null),
    h("p", { class: "hint", text: "PleatView Renderers draw pages for screens that can't, such as PleatView Light on a Raspberry Pi Zero, and send them a video. Choose one for a screen in its Edit dialog." }),
    rows.length || found.length ? h("section", { class: "panel" }, rows, found) : h("p", { class: "empty", text: "No renderers yet." }),
  ];
}

function pairRenderer(found) {
  const fields = [{ name: "name", label: "Name", value: found?.name || "", hint: "Such as “Rack A”." }];
  if (!found) {
    fields.push({ name: "host", label: "IP address" }, { name: "port", label: "Port", type: "number", value: "8780" },
      { name: "https", label: "Connection", options: [["", "HTTP"], ["1", "HTTPS (the console pins its certificate)"]], required: false });
  }
  fields.push({ name: "pin", label: "PIN", autocomplete: "off", hint: "From the renderer's log: docker logs pleatview-renderer" });
  dialog({
    title: found ? `Pair ${found.name}` : "Add a renderer by address",
    fields,
    ok: "Pair",
    submit: async (data) => {
      const payload = { name: data.name.trim(), pin: data.pin.trim() };
      if (found) payload.device_id = found.id;
      else Object.assign(payload, { host: data.host.trim(), port: Number(data.port), https: Boolean(data.https) });
      await api("POST", `/api/t/${app.tenant}/renderers`, payload);
      await loadRenderers();
      renderSites();
    },
  });
}

function removeRenderer(r) {
  dialog({
    title: `Remove ${r.name}?`,
    text: "The console unpairs from it. Screens it rendered show their pages themselves again, where they can.",
    ok: "Remove",
    danger: true,
    submit: async () => {
      await api("DELETE", `/api/t/${app.tenant}/renderers/${r.id}`);
      await loadRenderers();
      renderSites();
    },
  });
}

function renderSites() {
  const own = may("own");
  if (!app.renderers) {
    loadRenderers().then(() => tab() === "sites" && renderSites());
    app.renderers = [];
  }
  const count = (site) => app.screens.filter((s) => s.site_id === site.id).length;
  page(
    h("div", { class: "section-head" },
      h("h1", { text: app.overview.name }),
      own ? h("button", { type: "button", class: "quiet", onclick: renameTenant, text: "Rename" }) : null,
      own ? h("a", { class: "button quiet", href: `/api/t/${app.tenant}/export`, download: "", text: "Export" }) : null),
    h("p", { class: "hint", text: "Export: everything this organisation set up, as a file, without passwords or keys." }),
    h("div", { class: "section-head" },
      h("h1", { text: "Sites" }),
      own ? h("button", { type: "button", class: "quiet", onclick: addSite, text: "Add a site" }) : null),
    h("section", { class: "panel" }, app.overview.sites.map((site) => h("div", { class: "row" },
      h("span", {}, h("strong", { text: site.name }), " ", h("span", { class: "meta", text: `${count(site)} screen${count(site) === 1 ? "" : "s"} · ${site.timezone.replaceAll("_", " ")}` })),
      own ? h("button", { type: "button", class: "quiet", onclick: () => renameSite(site), text: "Edit" }) : null,
      own ? h("button", { type: "button", class: "danger", onclick: () => removeSite(site), text: "Remove", disabled: count(site) > 0, title: count(site) ? "Move or remove its screens first" : null }) : null))),
    ...renderersSection(),
  );
}

async function refreshOverview() {
  app.overview = await api("GET", `/api/t/${app.tenant}`);
  render();
}

function renameTenant() {
  dialog({
    title: "Rename the organisation",
    fields: [{ name: "name", label: "Name", value: app.overview.name }],
    ok: "Save",
    submit: async (data) => {
      const me = await api("PATCH", `/api/t/${app.tenant}`, { name: data.name.trim() });
      app.me.tenants = me.tenants;
      $("#tenant").replaceChildren(...me.tenants.map((t) => h("option", { value: t.id, text: t.name, selected: t.id === app.tenant })));
      await refreshOverview();
    },
  });
}

async function organisations() {
  $("#usermenu").open = false;
  const { tenants } = await api("GET", "/api/tenants");
  dialog({
    title: "Organisations on this console",
    text: "Deleting one unpairs its screens and deletes its data, media and key for good. Type its name to confirm.",
    fields: [
      { name: "tenant", label: "Organisation", options: tenants.map((t) => [t.id, `${t.name} (${t.members} member${t.members === 1 ? "" : "s"}, ${t.screens} screen${t.screens === 1 ? "" : "s"})`]) },
      { name: "confirm", label: "Its name, to delete it", required: false, autocomplete: "off" },
    ],
    ok: "Delete",
    danger: true,
    submit: async (data) => {
      const me = await api("DELETE", `/api/tenants/${data.tenant}`, { confirm: data.confirm });
      toast("Organisation deleted");
      signedIn({ ...app.me, tenants: me.tenants });
    },
  });
}

function addSite() {
  dialog({
    title: "Add a site",
    fields: [
      { name: "name", label: "Name", hint: "A building, a branch or a home." },
      { name: "timezone", label: "Time zone", options: zoneOptions(), value: browserZone(), hint: "Its schedules run on this clock." },
    ],
    ok: "Add",
    submit: async (data) => {
      await api("POST", `/api/t/${app.tenant}/sites`, { name: data.name.trim(), timezone: data.timezone });
      await refreshOverview();
    },
  });
}

function renameSite(site) {
  dialog({
    title: `Edit ${site.name}`,
    fields: [
      { name: "name", label: "Name", value: site.name },
      { name: "timezone", label: "Time zone", options: zoneOptions(site.timezone), value: site.timezone },
    ],
    ok: "Save",
    submit: async (data) => {
      await api("PATCH", `/api/t/${app.tenant}/sites/${site.id}`, { name: data.name.trim(), timezone: data.timezone });
      await refreshOverview();
    },
  });
}

function removeSite(site) {
  dialog({
    title: `Remove ${site.name}?`,
    ok: "Remove",
    danger: true,
    submit: async () => {
      await api("DELETE", `/api/t/${app.tenant}/sites/${site.id}`);
      await refreshOverview();
    },
  });
}

// ---- Groups -----------------------------------------------------------------------------

const screenName = (id) => app.screens.find((s) => s.id === id)?.name || `Screen ${id}`;
const screenChoices = () => app.screens.map((s) => [s.id, s.name]);

async function renderGroups() {
  let groups;
  try {
    ({ groups } = await api("GET", `/api/t/${app.tenant}/groups`));
  } catch (error) {
    page(h("p", { class: "error", text: error.message }));
    return;
  }
  if (tab() !== "groups") return;
  const operate = may("operate");
  page(
    h("div", { class: "section-head" },
      h("h1", { text: "Groups" }),
      operate ? h("button", { type: "button", class: "quiet", onclick: () => editGroup(), text: "Add a group" }) : null),
    h("p", { class: "hint", text: "Screens that do things together: a page, a video or a notification on all of them at once." }),
    groups.length ? h("section", { class: "panel" }, groups.map((g) => h("div", { class: "row" },
      h("span", {}, h("strong", { text: g.name }), " ", h("span", { class: "meta", text: g.screens.map(screenName).join(", ") || "No screens" })),
      operate ? h("button", { type: "button", onclick: () => groupDo(g), text: "Do" }) : null,
      operate ? h("button", { type: "button", class: "quiet", onclick: () => editGroup(g), text: "Edit" }) : null,
      operate ? h("button", { type: "button", class: "danger", onclick: () => removeGroup(g), text: "Remove" }) : null)))
      : h("p", { class: "empty", text: "No groups yet." }),
  );
}

function editGroup(group) {
  dialog({
    title: group ? `Edit ${group.name}` : "Add a group",
    fields: [
      { name: "name", label: "Name", value: group?.name || "" },
      { name: "screens", label: "Screens", type: "checks", options: screenChoices(), value: group?.screens || [] },
    ],
    ok: "Save",
    submit: async (data) => {
      const payload = { name: data.name.trim(), screens: (data.screens || []).map(Number) };
      if (group) await api("PATCH", `/api/t/${app.tenant}/groups/${group.id}`, payload);
      else await api("POST", `/api/t/${app.tenant}/groups`, payload);
      renderGroups();
    },
  });
}

function removeGroup(group) {
  dialog({
    title: `Remove ${group.name}?`,
    text: "Only the group goes; its screens stay.",
    ok: "Remove",
    danger: true,
    submit: async () => {
      await api("DELETE", `/api/t/${app.tenant}/groups/${group.id}`);
      renderGroups();
    },
  });
}

const GROUP_ACTIONS = [
  ["home_page", "Set their home page"], ["show_url", "Show a web page"], ["play_media", "Play media"],
  ["notify", "Notify"], ["home", "Go home"], ["reload", "Reload"], ["turn_off", "Standby"], ["turn_on", "Wake"],
];

function groupDo(group) {
  const pageOptions = [["", "None: back to their own"], ...app.pages.map((p) => [p.id, p.name])];
  const mediaOptions = (app.media || []).filter((m) => m.address).map((m) => [m.address, m.name]);
  dialog({
    title: `${group.name}: do`,
    fields: [
      { name: "action", label: "What", options: GROUP_ACTIONS },
      { name: "page", label: "Home page (for “Set their home page”)", options: pageOptions, required: false },
      { name: "url", label: "Address (web page or media)", type: "url", required: false,
        hint: mediaOptions.length ? "Or leave empty and choose media below." : null },
      ...(mediaOptions.length ? [{ name: "media", label: "Media from the library", options: [["", "None"], ...mediaOptions], required: false }] : []),
      { name: "message", label: "Message (for “Notify”)", required: false },
    ],
    ok: "Do it",
    submit: async (data) => {
      let payload;
      if (data.action === "home_page") payload = { home_page: data.page ? Number(data.page) : null };
      else if (data.action === "show_url") payload = { command: "show_url", url: data.url.trim() };
      else if (data.action === "play_media") payload = { command: "play_media", url: (data.url.trim() || data.media || "") };
      else if (data.action === "notify") payload = { command: "notify", message: data.message };
      else payload = { command: data.action };
      const result = await api("POST", `/api/t/${app.tenant}/groups/${group.id}/action`, payload);
      toast(result.failed.length ? `Done on ${result.done}; not on ${result.failed.join(", ")}` : `Done on ${result.done} screen${result.done === 1 ? "" : "s"}`);
    },
  });
}

// ---- Content: pages and the media library ---------------------------------------------

const KINDS = { video: "Video", audio: "Sound", image: "Picture" };

function size(bytes) {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`;
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
  return `${Math.ceil(bytes / 1e3)} kB`;
}

function length(seconds) {
  if (!seconds) return "";
  const s = Math.round(seconds);
  return s >= 3600 ? `${Math.floor(s / 3600)}:${String(Math.floor(s / 60) % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`
    : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function describeMedia(m) {
  const parts = [KINDS[m.kind] || "", length(m.duration), size(m.size)];
  const sum = m.summary;
  if (sum?.video) parts.push(`${(sum.video.codec || "").toUpperCase()} ${sum.video.width}×${sum.video.height}`);
  if (sum?.audio) parts.push(`${(sum.audio.codec || "").toUpperCase()}${sum.audio.channels ? ` ${sum.audio.channels} ch` : ""}`);
  return parts.filter(Boolean).join(" · ");
}

function mediaStatus(m) {
  if (m.status === "ready") return "Ready";
  if (m.status === "failed") return `Couldn't convert: ${m.error || "unknown error"}`;
  if (m.status === "converting") return `Converting for every screen… ${Math.round(m.progress * 100)}%`;
  return "Waiting to convert";
}

async function refreshMedia() {
  clearTimeout(refreshMedia.timer);
  refreshMedia.timer = setTimeout(async () => {
    try {
      app.media = (await api("GET", `/api/t/${app.tenant}/media`)).media;
      if (tab() === "content") drawContent();
    } catch {
      // the next event tries again
    }
  }, 300);
}

async function renderContent() {
  try {
    const [pages, media] = await Promise.all([
      api("GET", `/api/t/${app.tenant}/pages`),
      api("GET", `/api/t/${app.tenant}/media`),
    ]);
    app.pages = pages.pages;
    app.media = media.media;
  } catch (error) {
    page(h("p", { class: "error", text: error.message }));
    return;
  }
  if (tab() === "content") drawContent();
}

function drawContent() {
  const operate = may("operate");
  const upload = h("input", { type: "file", multiple: true, hidden: true,
    accept: "video/*,audio/*,image/*,.mkv,.ts,.m2ts,.flac,.opus,.wma,.wmv" });
  upload.addEventListener("change", () => uploadFiles([...upload.files]));
  page(
    h("div", { class: "section-head" },
      h("h1", { text: "Pages" }),
      operate ? h("button", { type: "button", class: "quiet", onclick: () => editPage(null, "web"), text: "Add a web page" }) : null,
      operate ? h("button", { type: "button", class: "quiet", onclick: () => editPage(null, "playlist"), text: "Add a playlist" }) : null),
    h("p", { class: "hint", text: "What screens can show as their home page: any web page, or a playlist of your media that plays full screen." }),
    app.pages.length ? h("section", { class: "panel" }, app.pages.map((p) => h("div", { class: "row" },
      h("span", {}, h("strong", { text: p.name }), " ",
        h("span", { class: "meta", text: [p.kind === "web" ? p.url : `Playlist of ${p.items.length}`, p.screens ? `home of ${p.screens} screen${p.screens === 1 ? "" : "s"}` : null].filter(Boolean).join(" · ") })),
      operate ? h("button", { type: "button", class: "quiet", onclick: () => editPage(p, p.kind), text: "Edit" }) : null,
      operate ? h("button", { type: "button", class: "danger", onclick: () => removePage(p), text: "Remove" }) : null)))
      : h("p", { class: "empty", text: "No pages yet." }),
    h("div", { class: "section-head" },
      h("h1", { text: "Media" }),
      operate ? h("button", { type: "button", class: "quiet", onclick: () => upload.click(), text: "Upload" }) : null,
      upload),
    h("p", { class: "hint", text: "Videos, sounds and pictures in nearly any format. The console converts what a screen can't play as it is." }),
    h("div", { id: "uploads" }),
    app.media.length ? h("section", { class: "panel" }, app.media.map((m) => h("div", { class: "row media" },
      m.thumbnail ? h("img", { class: "thumb", src: `/api/t/${app.tenant}/media/${m.id}/thumb.jpg`, alt: "" }) : h("span", { class: "thumb" }),
      h("span", {}, h("strong", { text: m.name }), h("br"),
        h("span", { class: "meta", text: describeMedia(m) }), h("br"),
        h("span", { class: m.status === "failed" ? "error" : "meta", text: mediaStatus(m) })),
      operate && m.address ? h("button", { type: "button", onclick: () => playOn(m), text: "Play on…" }) : null,
      operate ? h("button", { type: "button", class: "danger", onclick: () => removeMedia(m), text: "Remove" }) : null)))
      : h("p", { class: "empty", text: "Nothing uploaded yet." }),
  );
}

function uploadFiles(files) {
  for (const file of files) {
    const bar = h("progress", { max: 100, value: 0 });
    const row = h("div", { class: "row" }, h("span", { text: `Uploading ${file.name}` }), bar);
    $("#uploads")?.append(row);
    const request = new XMLHttpRequest();
    request.open("POST", `/api/t/${app.tenant}/media?name=${encodeURIComponent(file.name)}`);
    request.setRequestHeader("X-PleatView", "1");
    request.setRequestHeader("Content-Type", "application/octet-stream");
    request.upload.onprogress = (event) => { if (event.lengthComputable) bar.value = (100 * event.loaded) / event.total; };
    request.onload = () => {
      row.remove();
      if (request.status !== 200) {
        let message = `Error ${request.status}`;
        try { message = JSON.parse(request.responseText).error || message; } catch { /* keep */ }
        toast(`${file.name}: ${message}`);
      }
      refreshMedia();
    };
    request.onerror = () => { row.remove(); toast(`${file.name}: the upload stopped`); };
    request.send(file);
  }
}

function playOn(m) {
  dialog({
    title: `Play ${m.name}`,
    fields: [{ name: "screen", label: "On", options: app.screens.map((s) => [s.id, s.name]) }],
    ok: "Play",
    submit: async (data) => {
      await api("POST", `/api/t/${app.tenant}/screens/${data.screen}/command`,
        { command: "play_media", url: m.address, title: m.name });
      toast(`Playing on ${screenName(Number(data.screen))}`);
    },
  });
}

function removeMedia(m) {
  dialog({
    title: `Remove ${m.name}?`,
    text: "It goes from the library and from every playlist.",
    ok: "Remove",
    danger: true,
    submit: async () => {
      await api("DELETE", `/api/t/${app.tenant}/media/${m.id}`);
      renderContent();
    },
  });
}

function editPage(p, kind) {
  const fields = [{ name: "name", label: "Name", value: p?.name || "" }];
  if (kind === "web") {
    fields.push({ name: "url", label: "Address", type: "url", value: p?.url || "", hint: "Any web page, such as a dashboard, a menu board or a website." });
  }
  dialog({
    title: p ? `Edit ${p.name}` : kind === "web" ? "Add a web page" : "Add a playlist",
    fields,
    ok: "Save",
    submit: async (data) => {
      const payload = { name: data.name.trim(), kind };
      if (kind === "web") payload.url = data.url.trim();
      else {
        payload.items = [...$("#dialog-body").querySelectorAll("input[name=item]:checked")].map((box) => {
          const seconds = $("#dialog-body").querySelector(`input[name=seconds-${box.value}]`)?.value;
          return { media_id: Number(box.value), ...(seconds ? { seconds: Number(seconds) } : {}) };
        });
      }
      if (p) await api("PATCH", `/api/t/${app.tenant}/pages/${p.id}`, payload);
      else await api("POST", `/api/t/${app.tenant}/pages`, payload);
      renderContent();
    },
  });
  if (kind === "playlist") {
    // The library, oldest first, which is the order the playlist plays in.
    const chosen = new Map((p?.items || []).map((i) => [i.media_id, i.seconds]));
    const items = [...app.media].reverse();
    $("#dialog-body").append(h("fieldset", { class: "checks" },
      h("legend", { text: "Plays in this order, then starts again" }),
      items.length ? items.map((m) => h("label", { class: "check" },
        h("input", { type: "checkbox", name: "item", value: m.id, checked: chosen.has(m.id) }), " ", m.name,
        m.kind === "image" ? h("input", { type: "number", name: `seconds-${m.id}`, min: 1, max: 86400, value: chosen.get(m.id) || 10, class: "seconds", "aria-label": `Seconds for ${m.name}` }) : null,
        m.kind === "image" ? " s" : null))
        : h("p", { class: "hint", text: "Upload media first." })));
  }
}

function removePage(p) {
  dialog({
    title: `Remove ${p.name}?`,
    text: p.screens ? "Screens with it as their home page keep a web page's address, or go back to their setup screen for a playlist." : null,
    ok: "Remove",
    danger: true,
    submit: async () => {
      await api("DELETE", `/api/t/${app.tenant}/pages/${p.id}`);
      renderContent();
    },
  });
}

// ---- Schedules --------------------------------------------------------------------------

const DAYS = [["1", "Mon"], ["2", "Tue"], ["3", "Wed"], ["4", "Thu"], ["5", "Fri"], ["6", "Sat"], ["7", "Sun"]];

async function renderSchedules() {
  let schedules, groups;
  try {
    [{ schedules }, { groups }] = await Promise.all([
      api("GET", `/api/t/${app.tenant}/schedules`), api("GET", `/api/t/${app.tenant}/groups`)]);
    app.groups = groups;
  } catch (error) {
    page(h("p", { class: "error", text: error.message }));
    return;
  }
  if (tab() !== "schedules") return;
  const operate = may("operate");
  const days = (d) => d === "1234567" ? "Every day" : d === "12345" ? "Weekdays" : d === "67" ? "Weekends" : DAYS.filter(([v]) => d.includes(v)).map(([, n]) => n).join(" ");
  page(
    h("div", { class: "section-head" },
      h("h1", { text: "Schedules" }),
      operate ? h("button", { type: "button", class: "quiet", onclick: () => editSchedule(), text: "Add a schedule" }) : null),
    h("p", { class: "hint", text: "A page as the home page at set times, on a screen, a group or a whole site, by the site's clock. A screen's own schedule comes before its groups', and those before its site's. Outside them, screens show their own home page." }),
    schedules.length ? h("section", { class: "panel" }, schedules.map((e) => h("div", { class: "row" },
      h("span", {}, h("strong", { text: e.name }), " ",
        h("span", { class: "meta", text: `${app.pages.find((p) => p.id === e.page_id)?.name || "?"} on ${targetLabel(e.target)} · ${days(e.days)} ${e.start}–${e.end}` })),
      operate ? h("button", { type: "button", class: "quiet", onclick: () => editSchedule(e), text: "Edit" }) : null,
      operate ? h("button", { type: "button", class: "danger", onclick: () => removeSchedule(e), text: "Remove" }) : null)))
      : h("p", { class: "empty", text: app.pages.length ? "No schedules yet." : "Add a page under Content first." }),
  );
}

function targetLabel(target) {
  const [kind, id] = target.split(":");
  if (kind === "screen") return screenName(Number(id));
  if (kind === "group") return `group ${(app.groups || []).find((g) => String(g.id) === id)?.name || id}`;
  return `site ${app.overview.sites.find((s) => String(s.id) === id)?.name || id}`;
}

function editSchedule(entry) {
  const targets = [
    ...app.overview.sites.map((s) => [`site:${s.id}`, `Site: ${s.name}`]),
    ...(app.groups || []).map((g) => [`group:${g.id}`, `Group: ${g.name}`]),
    ...app.screens.map((s) => [`screen:${s.id}`, `Screen: ${s.name}`]),
  ];
  dialog({
    title: entry ? `Edit ${entry.name}` : "Add a schedule",
    fields: [
      { name: "name", label: "Name", value: entry?.name || "", hint: "Such as “Breakfast menu”." },
      { name: "page_id", label: "Show", options: app.pages.map((p) => [p.id, p.name]), value: entry?.page_id },
      { name: "target", label: "On", options: targets, value: entry?.target },
      { name: "days", label: "Days", type: "checks", options: DAYS, value: (entry?.days || "12345").split("") },
      { name: "start", label: "From", type: "time", value: entry?.start || "08:00" },
      { name: "end", label: "Until", type: "time", value: entry?.end || "17:00", hint: "Before the start: until that time the next day. The same as the start: all day." },
    ],
    ok: "Save",
    submit: async (data) => {
      const payload = { name: data.name.trim(), page_id: Number(data.page_id), target: data.target,
        days: (data.days || []).join(""), start: data.start, end: data.end };
      if (entry) await api("PATCH", `/api/t/${app.tenant}/schedules/${entry.id}`, payload);
      else await api("POST", `/api/t/${app.tenant}/schedules`, payload);
      renderSchedules();
    },
  });
}

function removeSchedule(entry) {
  dialog({
    title: `Remove ${entry.name}?`,
    ok: "Remove",
    danger: true,
    submit: async () => {
      await api("DELETE", `/api/t/${app.tenant}/schedules/${entry.id}`);
      renderSchedules();
    },
  });
}

// ---- Members ----------------------------------------------------------------------------

const ROLES = [["owner", "Owner: everything, members too"], ["operator", "Operator: pairs and controls screens"], ["viewer", "Viewer: sees screens and activity"]];

async function renderMembers() {
  let members;
  try {
    ({ members } = await api("GET", `/api/t/${app.tenant}/members`));
  } catch (error) {
    page(h("p", { class: "error", text: error.message }));
    return;
  }
  if (tab() !== "members") return;
  const roleSelect = (m) => {
    const select = h("select", { "aria-label": `Role of ${m.name}` },
      ROLES.map(([value, text]) => h("option", { value, selected: value === m.role, text: text.split(":")[0] })));
    select.addEventListener("change", async () => {
      try {
        await api("PATCH", `/api/t/${app.tenant}/members/${m.id}`, { role: select.value });
        toast(`${m.name} is now ${select.value}`);
      } catch (error) {
        toast(error.message);
        select.value = m.role;
      }
    });
    return select;
  };
  const canReset = (m) => m.managed || app.me.user.admin;
  page(
    h("div", { class: "section-head" },
      h("h1", { text: "Members" }),
      h("button", { type: "button", class: "quiet", onclick: addMember, text: "Add a member" })),
    h("p", { class: "hint", text: "People who can open this organisation, and what they may do." }),
    h("section", { class: "panel" }, members.map((m) => h("div", { class: "row" },
      h("span", {}, h("strong", { text: m.name }), " ", h("span", { class: "meta", text: [m.username, m.two_factor ? "two-factor on" : null, m.admin ? "console administrator" : null].filter(Boolean).join(" · ") })),
      roleSelect(m),
      canReset(m) ? h("button", { type: "button", class: "quiet", onclick: () => resetMember(m), text: "Reset" }) : null,
      h("button", { type: "button", class: "danger", onclick: () => removeMember(m), text: "Remove" })))),
  );
}

function showOneTimePassword(title, username, password) {
  setTimeout(() => {
    dialog({ title, ok: "Done", submit: async () => {} });
    $("#dialog-cancel").hidden = true;
    $("#dialog-body").append(
      h("p", { text: `Give ${username} this one-time password. They choose their own at the first sign-in. It isn't shown again.` }),
      h("pre", { class: "codes", text: password }));
  });
}

function addMember() {
  dialog({
    title: "Add a member",
    fields: [
      { name: "name", label: "Name" },
      { name: "username", label: "User name", autocomplete: "off" },
      { name: "role", label: "Role", options: ROLES, value: "operator" },
    ],
    ok: "Add",
    submit: async (data) => {
      const result = await api("POST", `/api/t/${app.tenant}/members`, data);
      if (result.password) showOneTimePassword("Member added", data.username, result.password);
      else toast(`${data.username} added`);
      renderMembers();
    },
  });
}

function resetMember(m) {
  dialog({
    title: `Reset ${m.name}`,
    fields: [{ name: "reset", label: "Reset", options: [["password", "Password: a new one-time password"], ...(m.two_factor ? [["two_factor", "Two-factor sign-in: turn it off, for a lost phone"]] : [])] }],
    ok: "Reset",
    danger: true,
    submit: async (data) => {
      const result = await api("POST", `/api/t/${app.tenant}/members/${m.id}/reset`, data);
      if (result.password) showOneTimePassword("Password reset", m.username, result.password);
      else toast("Two-factor sign-in turned off");
      renderMembers();
    },
  });
}

function removeMember(m) {
  dialog({
    title: `Remove ${m.name}?`,
    text: "They can no longer open this organisation. An account that opens nothing else is deleted.",
    ok: "Remove",
    danger: true,
    submit: async () => {
      await api("DELETE", `/api/t/${app.tenant}/members/${m.id}`);
      renderMembers();
    },
  });
}

// ---- Activity ---------------------------------------------------------------------------

const ACTIONS = {
  tenant_created: "Set up the organisation",
  site_added: "Added a site",
  site_renamed: "Renamed a site",
  site_removed: "Removed a site",
  screen_paired: "Paired a screen",
  screen_changed: "Changed a screen",
  screen_removed: "Removed a screen",
  screen_moved: "A screen moved to a new address",
  command: "Sent a command",
  member_added: "Added a member",
  member_changed: "Changed a member's role",
  member_removed: "Removed a member",
  member_password_reset: "Reset a member's password",
  member_two_factor_reset: "Turned off a member's two-factor sign-in",
  group_saved: "Saved a group",
  group_removed: "Removed a group",
  group_action: "Sent to a group",
  page_saved: "Saved a page",
  page_removed: "Removed a page",
  media_added: "Uploaded media",
  media_removed: "Removed media",
  schedule_saved: "Saved a schedule",
  renderer_paired: "Paired a renderer",
  renderer_removed: "Removed a renderer",
  schedule_removed: "Removed a schedule",
  tenant_renamed: "Renamed the organisation",
  tenant_exported: "Exported the organisation",
  two_factor_enabled: "Turned on two-factor sign-in",
  two_factor_disabled: "Turned off two-factor sign-in",
};

const when = (at) => new Date(at * 1000).toLocaleString();

// An entry's details as "key: value" pairs; a site's id as its name.
function details(detail) {
  if (!detail) return "";
  let data;
  try {
    data = JSON.parse(detail);
  } catch {
    return detail;
  }
  if (typeof data !== "object" || data === null) return String(detail);
  return Object.entries(data).map(([key, value]) =>
    key === "site_id" ? `site: ${targetName(`site:${value}`)}` : `${key}: ${value}`).join(", ");
}

function targetName(target) {
  if (!target) return "";
  const [kind, id] = target.split(":");
  if (kind === "screen") return app.screens.find((s) => String(s.id) === id)?.name || `Screen ${id}`;
  if (kind === "site") return app.overview.sites.find((s) => String(s.id) === id)?.name || `Site ${id}`;
  if (kind === "user") return id;
  return target;
}

async function renderActivity(more = false) {
  try {
    const before = more && app.audit.length ? `?before=${app.audit[app.audit.length - 1].id}` : "";
    const { entries } = await api("GET", `/api/t/${app.tenant}/audit${before}`);
    app.audit = more ? app.audit.concat(entries) : entries;
  } catch (error) {
    page(h("p", { class: "error", text: error.message }));
    return;
  }
  if (tab() !== "activity") return;
  page(
    h("div", { class: "section-head" }, h("h1", { text: "Activity" })),
    h("p", { class: "hint", text: "Everything people and screens did in this organisation, newest first." }),
    h("div", { class: "panel" }, h("table", {},
      h("thead", {}, h("tr", {}, ["When", "Who", "What", "On", "Details"].map((t) => h("th", { text: t })))),
      h("tbody", {}, app.audit.map((e) => h("tr", {},
        h("td", { text: when(e.at) }),
        h("td", { text: e.user || "Console" }),
        h("td", { text: ACTIONS[e.action] || e.action }),
        h("td", { text: targetName(e.target) }),
        h("td", { class: "detail", text: [details(e.detail), e.address && `from ${e.address}`].filter(Boolean).join(" · ") })))))),
    app.audit.length && app.audit.length % 100 === 0
      ? h("p", {}, h("button", { type: "button", class: "quiet", onclick: () => renderActivity(true), text: "Older" }))
      : null,
  );
}

start();
