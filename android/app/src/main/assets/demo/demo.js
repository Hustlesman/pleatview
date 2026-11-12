/*
 * The demo's sample home: translations, clocks, the sample switches, the washing machine
 * and the energy chart. Everything here is made up; nothing leaves the page.
 * TRANSLATIONS comes from translations.js.
 */
"use strict";

const ICONS = {
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  partly: '<path d="M8 2.5v1.2M3.2 7.3H2M4.6 3.9l.9.9M11.4 3.9l-.9.9"/><path d="M4.7 10.6a3.6 3.6 0 0 1 6.5-3"/><path d="M8.5 21h9a4 4 0 0 0 .4-7.98 5.5 5.5 0 0 0-10.4 1.2A3.4 3.4 0 0 0 8.5 21z"/>',
  cloud: '<path d="M7 19h10.5a4.5 4.5 0 0 0 .6-8.96A6.5 6.5 0 0 0 5.6 11.2 4 4 0 0 0 7 19z"/>',
  rain: '<path d="M7 15h10.5a4.5 4.5 0 0 0 .6-8.96A6.5 6.5 0 0 0 5.6 7.2 4 4 0 0 0 7 15z"/><path d="M8 18.5l-1 2.5M12.5 18.5l-1 2.5M17 18.5l-1 2.5"/>',
  bulb: '<path d="M9 18h6M10 21h4"/><path d="M12 3a6 6 0 0 0-3.6 10.8c.7.6 1.1 1.3 1.2 2.2h4.8c.1-.9.5-1.6 1.2-2.2A6 6 0 0 0 12 3z"/>',
  thermometer: '<path d="M14 14.8V5a2 2 0 0 0-4 0v9.8a4 4 0 1 0 4 0z"/><path d="M12 9v7"/>',
  bolt: '<path d="M13 2L4.5 13.5H11L10 22l8.5-11.5H12L13 2z"/>',
  home: '<path d="M3 11l9-7 9 7"/><path d="M5 9.5V20h5v-6h4v6h5V9.5"/>',
  battery: '<rect x="2.5" y="7" width="17" height="10" rx="2"/><path d="M22 11v2M6 10.5v3M9.5 10.5v3M13 10.5v3"/>',
  shield: '<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6l8-3z"/><path d="M9 12l2 2 4-4"/>',
  lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
  unlock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 7.7-1.5"/>',
  garage: '<path d="M3 21V9l9-5 9 5v12"/><path d="M7 21v-9h10v9M7 15h10M7 18h10"/>',
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M3 10h18M8 3v4M16 3v4"/>',
  washer: '<rect x="4" y="2.5" width="16" height="19" rx="2"/><circle cx="12" cy="13.5" r="4.5"/><path d="M7.5 6h.01M10.5 6h6"/>',
};

// Toggles: the label and the look of each state, by kind.
const STATES = {
  light: { on: ["on", "On", "on"], off: ["off", "Off", ""] },
  lock: { on: ["locked", "Locked", "safe"], off: ["unlocked", "Unlocked", "unsafe"] },
  garage: { on: ["closed", "Closed", "safe"], off: ["open", "Open", "unsafe"] },
  alarm: { on: ["armed", "Armed", "safe"], off: ["disarmed", "Off", "unsafe"] },
};

const strings = (() => {
  for (const tag of navigator.languages || [navigator.language]) {
    const language = String(tag).toLowerCase().split("-")[0];
    if (language === "en") break;
    if (typeof TRANSLATIONS !== "undefined" && TRANSLATIONS[language]) {
      document.documentElement.lang = language;
      return TRANSLATIONS[language];
    }
  }
  return {};
})();
const t = (key, english) => strings[key] || english;
const locale = navigator.language || "en";
// Temperatures in Fahrenheit where people expect them.
const fahrenheit = /-(US|LR|MM)$/i.test(locale);

document.querySelectorAll("[data-t]").forEach((element) => {
  if (strings[element.dataset.t]) element.innerHTML = strings[element.dataset.t];
});
document.title = t(document.querySelector("title").dataset.t, document.title);

/** Swaps each <i data-icon="name"> for its drawing, keeping the element's class and style. */
function fillIcons(root) {
  root.querySelectorAll("i[data-icon]").forEach((element) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("class", `icon ${element.className}`.trim());
    if (element.hasAttribute("style")) svg.setAttribute("style", element.getAttribute("style"));
    svg.innerHTML = ICONS[element.dataset.icon] || "";
    element.replaceWith(svg);
  });
}

function temperature(celsius) {
  const value = fahrenheit ? Math.round((celsius * 9) / 5 + 32) : celsius;
  return `${value.toLocaleString(locale)}°`;
}

const timeOptions = { hour: "numeric", minute: "2-digit" };

document.querySelectorAll("[data-celsius]").forEach((element) => {
  element.textContent = temperature(Number(element.dataset.celsius));
});
document.querySelectorAll("[data-kw]").forEach((element) => {
  const kw = Number(element.dataset.kw);
  element.textContent = `${kw.toLocaleString(locale, { minimumFractionDigits: 1, maximumFractionDigits: 1 })} kW`;
});
document.querySelectorAll("[data-percent]").forEach((element) => {
  element.textContent = (Number(element.dataset.percent) / 100).toLocaleString(locale, { style: "percent" });
});
document.querySelectorAll("[data-day]").forEach((element) => {
  const day = new Date();
  day.setDate(day.getDate() + Number(element.dataset.day));
  element.textContent = day.toLocaleDateString(locale, { weekday: "short" });
});
document.querySelectorAll("[data-time]").forEach((element) => {
  const [hours, minutes] = element.dataset.time.split(":").map(Number);
  const at = new Date();
  at.setHours(hours, minutes, 0, 0);
  element.textContent = at.toLocaleTimeString(locale, timeOptions);
});

// ---- Header and camera clocks ----

function tick() {
  const now = new Date();
  const clock = document.getElementById("clock");
  if (clock) clock.textContent = now.toLocaleTimeString(locale, timeOptions);
  const greeting = document.getElementById("greeting");
  if (greeting) {
    const hour = now.getHours();
    greeting.textContent = hour >= 5 && hour < 12 ? t("good_morning", "Good morning")
      : hour >= 12 && hour < 18 ? t("good_afternoon", "Good afternoon") : t("good_evening", "Good evening");
  }
  const date = document.getElementById("date");
  if (date) date.textContent = now.toLocaleDateString(locale, { weekday: "long", day: "numeric", month: "long" });
  document.querySelectorAll("[data-clock]").forEach((element) => {
    element.textContent = now.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  });
}
tick();
setInterval(tick, 1000);

// ---- Switches ----

function show(toggle) {
  const on = toggle.dataset.on === "true";
  const [key, english, look] = STATES[toggle.dataset.kind][on ? "on" : "off"];
  toggle.querySelector(".state").textContent = t(key, english);
  toggle.classList.remove("on", "safe", "unsafe");
  if (look) toggle.classList.add(look);
  toggle.setAttribute("aria-pressed", String(on));
  if (toggle.dataset.kind === "lock") {
    toggle.querySelector("svg").innerHTML = ICONS[on ? "lock" : "unlock"];
  }
}

fillIcons(document);
document.querySelectorAll(".toggle").forEach((toggle) => {
  show(toggle);
  toggle.addEventListener("click", () => {
    toggle.dataset.on = String(toggle.dataset.on !== "true");
    show(toggle);
  });
});

// ---- Remote control: the arrows move between the switches ----

const MOVES = { ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0] };

document.addEventListener("keydown", (event) => {
  const move = MOVES[event.key];
  const items = [...document.querySelectorAll(".toggle")];
  if (!move || !items.length) return;
  const current = document.activeElement;
  if (!items.includes(current)) {
    items[0].focus();
    event.preventDefault();
    return;
  }
  const from = current.getBoundingClientRect();
  let best = null;
  let bestScore = Infinity;
  for (const item of items) {
    const to = item.getBoundingClientRect();
    const dx = to.left + to.width / 2 - (from.left + from.width / 2);
    const dy = to.top + to.height / 2 - (from.top + from.height / 2);
    const along = dx * move[0] + dy * move[1];
    if (item === current || along <= 1) continue;
    const score = along + 2 * Math.abs(dx * move[1] + dy * move[0]);
    if (score < bestScore) {
      bestScore = score;
      best = item;
    }
  }
  if (best) {
    best.focus();
    best.scrollIntoView({ block: "nearest" });
    event.preventDefault();
  }
});

// ---- The washing machine finishes a few seconds after the demo starts ----

const washer = document.getElementById("washer");
if (washer) {
  const bar = document.getElementById("washer-bar");
  const total = Number(washer.dataset.seconds);
  let left = sessionStorage.getItem("washer") === "done" ? 0 : total;
  const update = () => {
    if (left <= 0) {
      washer.textContent = t("done", "Done");
      washer.classList.add("done");
      bar.style.width = "100%";
      bar.style.background = "var(--good)";
      sessionStorage.setItem("washer", "done");
      return;
    }
    washer.textContent = `${t("spinning", "Spinning")} · 0:${String(left).padStart(2, "0")}`;
    bar.style.width = `${90 + (10 * (total - left)) / total}%`;
    left -= 1;
    setTimeout(update, 1000);
  };
  update();
}

// ---- Energy: a made-up sunny day, as far as it has got ----

const chart = document.getElementById("chart");
if (chart) {
  const STEP = 0.25; // hours
  const bump = (h, at, width, height) => height * Math.exp(-(((h - at) / width) ** 2));
  const solar = (h) => (h < 6.5 || h > 19.5 ? 0 : 4.6 * Math.max(0, Math.sin((Math.PI * (h - 6.5)) / 13)) ** 1.5 * (1 - bump(h, 15, 0.4, 0.35)));
  const home = (h) => 0.35 + bump(h, 7.4, 0.6, 1.8) + bump(h, 9.5, 0.5, 1.4) + bump(h, 12.6, 0.7, 0.7) + bump(h, 18.7, 0.9, 2.6);
  const now = new Date();
  const nowHours = now.getHours() + now.getMinutes() / 60;

  let solarToday = 0;
  let homeToday = 0;
  let exported = 0;
  for (let h = 0; h < nowHours; h += STEP) {
    solarToday += solar(h) * STEP;
    homeToday += home(h) * STEP;
    exported += Math.max(0, solar(h) - home(h)) * STEP;
  }
  const kwh = (value) => `${value.toLocaleString(locale, { maximumFractionDigits: 1, minimumFractionDigits: 1 })} kWh`;
  document.getElementById("solar-today").textContent = kwh(solarToday);
  document.getElementById("home-today").textContent = kwh(homeToday);
  document.getElementById("exported-today").textContent = kwh(exported);

  const W = 640;
  const H = 300;
  const left = 44;
  const bottom = 268;
  const top = 12;
  const x = (h) => left + ((W - left - 8) * h) / 24;
  const y = (kw) => bottom - ((bottom - top) * kw) / 5;
  const points = (f) => {
    const out = [];
    for (let h = 0; h <= 24; h += STEP) out.push(`${x(h).toFixed(1)},${y(f(h)).toFixed(1)}`);
    return out;
  };
  const solarPoints = points(solar);
  const area = `M${x(0)},${y(0)} L${solarPoints.join(" L")} L${x(24)},${y(0)} Z`;
  const line = `M${points(home).join(" L")}`;
  let grid = "";
  for (let kw = 1; kw <= 5; kw += 1) {
    grid += `<line x1="${left}" x2="${W - 8}" y1="${y(kw)}" y2="${y(kw)}" stroke="rgba(255,255,255,.08)"/>`;
    grid += `<text x="${left - 8}" y="${y(kw) + 4}" text-anchor="end">${kw}</text>`;
  }
  grid += `<text x="${left - 8}" y="${y(0) + 4}" text-anchor="end">kW</text>`;
  for (let h = 0; h <= 24; h += 6) {
    const at = new Date();
    at.setHours(h % 24, 0, 0, 0);
    const label = h === 24 ? "" : at.toLocaleTimeString(locale, { hour: "numeric" });
    grid += `<text x="${x(h)}" y="${H - 8}" text-anchor="middle">${label}</text>`;
  }
  const drawn = (opacity) =>
    `<path d="${area}" fill="rgba(255,200,87,${0.28 * opacity})" stroke="rgba(255,200,87,${opacity})" stroke-width="2"/>` +
    `<path d="${line}" fill="none" stroke="rgba(24,188,242,${opacity})" stroke-width="2.5"/>`;
  chart.setAttribute("viewBox", `0 0 ${W} ${H}`);
  chart.innerHTML =
    `<defs><clipPath id="past"><rect x="0" y="0" width="${x(nowHours)}" height="${H}"/></clipPath></defs>` +
    `<g fill="rgba(255,255,255,.55)" font-size="13">${grid}</g>` +
    drawn(0.3) +
    `<g clip-path="url(#past)">${drawn(1)}</g>` +
    `<line x1="${x(nowHours)}" x2="${x(nowHours)}" y1="${top}" y2="${bottom}" stroke="#fff" stroke-dasharray="4 4" opacity=".6"/>`;
}

// ---- A single camera, for picture-in-picture ----

const single = document.getElementById("single");
if (single) {
  const CAMERAS = { "front-door": ["front_door", "Front door"], driveway: ["driveway", "Driveway"],
    garden: ["garden", "Back garden"], "living-room": ["living_room", "Living room"] };
  const PICTURES = { "front-door": "front-door.webp", driveway: "driveway.webp", garden: "garden.webp",
    "living-room": "living-room.webp" };
  // Only the cameras above, by their own names: not "constructor" or other inherited ones.
  const id = new URLSearchParams(location.search).get("id");
  const camera = Object.prototype.hasOwnProperty.call(CAMERAS, id) ? id : "front-door";
  single.querySelector("img").src = PICTURES[camera];
  single.querySelector(".label").textContent = t(...CAMERAS[camera]);
}
