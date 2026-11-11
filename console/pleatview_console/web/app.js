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
  constructor(status, message) {
    super(message);
    this.status = status;
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
  if (!response.ok) throw new ApiError(response.status, body.error || `Error ${response.status}`);
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
  if (options) {
    input = h("select", { id, name }, options.map(([v, text]) => h("option", { value: v, selected: String(v) === String(value), text })));
  } else {
    input = h("input", { id, name, type, value, autocomplete, required, spellcheck: "false" });
  }
  return [h("label", { for: id, text: label }), input, hint ? h("p", { class: "hint", text: hint }) : null];
}

function values(form) {
  const out = {};
  for (const el of form.querySelectorAll("input, select")) out[el.name] = el.value;
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
  ], "Set up", async (data) => signedIn(await api("POST", "/api/setup", data)));
}

function showLogin() {
  $("#bar").hidden = true;
  formPage("Sign in", [
    { name: "username", label: "User name", autocomplete: "username" },
    { name: "password", label: "Password", type: "password", autocomplete: "current-password" },
  ], "Sign in", async (data) => signedIn(await api("POST", "/api/login", data)));
}

function signedIn(me) {
  app.me = me;
  $("#bar").hidden = false;
  $("#username").textContent = me.user.name;
  $("#add-tenant").hidden = !me.user.admin;
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
      const result = await api("POST", "/api/tenants", data);
      localStorage.setItem("tenant", result.id);
      signedIn({ user: result.user, tenants: result.tenants });
    },
  });
}

// ---- The organisation -----------------------------------------------------------------

async function selectTenant(id) {
  app.tenant = id;
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
  const data = await api("GET", `/api/t/${app.tenant}/screens`);
  app.screens = data.screens;
  app.discovered = data.discovered;
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
  if (current === "sites") renderSites();
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

function editScreen(screen) {
  dialog({
    title: `Edit ${screen.name}`,
    fields: [
      { name: "name", label: "Name", value: screen.name },
      { name: "site_id", label: "Site", options: siteOptions(), value: screen.site_id },
    ],
    ok: "Save",
    submit: async (data) => {
      await api("PATCH", `/api/t/${app.tenant}/screens/${screen.id}`, { name: data.name.trim(), site_id: Number(data.site_id) });
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

function renderSites() {
  const own = may("own");
  const count = (site) => app.screens.filter((s) => s.site_id === site.id).length;
  page(
    h("div", { class: "section-head" },
      h("h1", { text: "Sites" }),
      own ? h("button", { type: "button", class: "quiet", onclick: addSite, text: "Add a site" }) : null),
    h("section", { class: "panel" }, app.overview.sites.map((site) => h("div", { class: "row" },
      h("span", {}, h("strong", { text: site.name }), " ", h("span", { class: "meta", text: `${count(site)} screen${count(site) === 1 ? "" : "s"}` })),
      own ? h("button", { type: "button", class: "quiet", onclick: () => renameSite(site), text: "Rename" }) : null,
      own ? h("button", { type: "button", class: "danger", onclick: () => removeSite(site), text: "Remove", disabled: count(site) > 0, title: count(site) ? "Move or remove its screens first" : null }) : null))),
  );
}

async function refreshOverview() {
  app.overview = await api("GET", `/api/t/${app.tenant}`);
  render();
}

function addSite() {
  dialog({
    title: "Add a site",
    fields: [{ name: "name", label: "Name", hint: "A building, a branch or a home." }],
    ok: "Add",
    submit: async (data) => {
      await api("POST", `/api/t/${app.tenant}/sites`, { name: data.name.trim() });
      await refreshOverview();
    },
  });
}

function renameSite(site) {
  dialog({
    title: "Rename the site",
    fields: [{ name: "name", label: "Name", value: site.name }],
    ok: "Save",
    submit: async (data) => {
      await api("PATCH", `/api/t/${app.tenant}/sites/${site.id}`, { name: data.name.trim() });
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
