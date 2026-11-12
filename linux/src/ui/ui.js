// PleatView's own layer: setup screen, notification cards, media, banner, menu and the
// standby curtain. The app sends it messages; it reports what it covers, so the app can
// let touches through everywhere else, and what's playing.
"use strict";

const $ = (id) => document.getElementById(id);
const screenEl = $("screen");
const video = $("video");
const announcement = $("announcement");
const state = { offset: { x: 0, y: 0 }, media: null, hls: null, mediaTimer: null, imageTimer: null, ducked: false };

// ---- What the layer covers ------------------------------------------------------------

const FULL = ["setup", "menu", "approval", "signin", "curtain"];

function reportRegions() {
  const visible = (el) => el && !el.hidden;
  const fullMedia = visible($("media")) && !$("media").classList.contains("pip") && state.media?.mode !== "background";
  const fullImage = visible($("image")) && !$("image").classList.contains("pip");
  const full = FULL.some((id) => visible($(id))) || fullMedia || fullImage;
  const rects = [];
  const add = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width && r.height) rects.push({ x: r.x + state.offset.x, y: r.y + state.offset.y, width: r.width, height: r.height });
  };
  if (!full) {
    for (const el of document.querySelectorAll(".region, .toast, #media.pip, #image.pip")) if (visible(el) && el.offsetParent) add(el);
  }
  window.pleatview.send({ type: "regions", full, rects });
}

let regionsQueued = false;
function regionsChanged() {
  if (regionsQueued) return;
  regionsQueued = true;
  requestAnimationFrame(() => {
    regionsQueued = false;
    reportRegions();
  });
}
new MutationObserver(regionsChanged).observe(screenEl, { subtree: true, childList: true, attributes: true, attributeFilter: ["hidden", "class", "style"] });

// ---- Setup screen ---------------------------------------------------------------------

function showSetupInfo(info) {
  if (!info) return;
  $("setup-address").textContent = info.address ? `Host  ${info.address}\nPort  ${info.port}` : "Not connected to a network";
  $("setup-pin").textContent = info.pin.replace(/(\d{3})(\d{3})/, "$1 $2");
  $("setup-manual").textContent = `No integration? Scan the code, or open http://${info.address}:${info.port} on your phone.`;
  $("setup-device").textContent = `${info.name} · v${info.version} · Help: pleatview.hustlesman.com`;
  $("setup-qr").innerHTML = info.qr || "";
  if (!$("menu").hidden) $("menu-info").textContent = menuInfo(info);
}

// ---- Notification cards -----------------------------------------------------------------

const MAX_VISIBLE = 3;

function notify(command) {
  let stack = document.querySelector(`.stack.${command.position}`);
  if (!stack) {
    stack = document.createElement("div");
    stack.className = `stack ${command.position}`;
    $("toasts").appendChild(stack);
  }
  const card = document.createElement("div");
  card.className = "toast card";
  if (command.imageUrl) {
    const img = document.createElement("img");
    img.src = command.imageUrl;
    img.alt = "";
    img.onload = regionsChanged;
    card.appendChild(img);
  }
  if (command.title) {
    const title = document.createElement("div");
    title.className = "toast-title";
    title.textContent = command.title;
    card.appendChild(title);
  }
  const message = document.createElement("div");
  message.className = "toast-message";
  message.textContent = command.message;
  card.appendChild(message);
  stack.appendChild(card);
  while (stack.children.length > MAX_VISIBLE) stack.firstElementChild.remove();
  requestAnimationFrame(() => card.classList.add("shown"));
  setTimeout(() => {
    card.classList.remove("shown");
    setTimeout(() => card.remove(), 250);
  }, command.duration * 1000);
}

// ---- Media ------------------------------------------------------------------------------

function place(el, layout) {
  el.classList.toggle("pip", layout.mode === "pip");
  if (layout.mode !== "pip") {
    el.style.cssText = "";
    return;
  }
  const width = (window.innerScreenWidth * layout.size) / 100;
  const height = (width * 9) / 16;
  const margin = 24;
  const right = layout.position.endsWith("right");
  const bottom = layout.position.startsWith("bottom");
  const center = layout.position === "center";
  el.style.width = `${width}px`;
  el.style.height = `${height}px`;
  el.style.left = center ? `${(window.innerScreenWidth - width) / 2}px` : right ? "auto" : `${margin}px`;
  el.style.right = right && !center ? `${margin}px` : "auto";
  el.style.top = center ? `${(window.innerScreenHeight - height) / 2}px` : bottom ? "auto" : `${margin}px`;
  el.style.bottom = bottom && !center ? `${margin}px` : "auto";
}

function isHls(command) {
  return /mpegurl/i.test(command.mimeType || "") || /\.m3u8(\?|#|$)/i.test(command.url);
}

function load(element, command) {
  if (state.hls) {
    state.hls.destroy();
    state.hls = null;
  }
  if (isHls(command) && window.Hls?.isSupported()) {
    state.hls = new window.Hls({ lowLatencyMode: true });
    state.hls.loadSource(command.url);
    state.hls.attachMedia(element);
  } else {
    element.src = command.url;
  }
}

function play(command) {
  if (command.announce) return announce(command);
  if (/^rtsps?:/i.test(command.url)) {
    banner("RTSP streams don't play on Linux yet. Show the camera with pleatview.show_camera instead.", 10);
    return;
  }
  stop(false);
  const audio = command.layout.mode === "background";
  state.media = { ...command, mode: command.layout.mode };
  place($("media"), command.layout);
  $("media").hidden = audio;
  video.loop = command.loop;
  if (command.volume !== null) video.volume = command.volume;
  load(video, command);
  if (command.startPosition) video.currentTime = command.startPosition;
  video.play().catch(() => {});
  if (audio) showNowPlaying(command);
  clearTimeout(state.mediaTimer);
  if (command.duration) state.mediaTimer = setTimeout(() => stop(), command.duration * 1000);
  reportMedia();
}

function stop(report = true) {
  clearTimeout(state.mediaTimer);
  if (state.hls) {
    state.hls.destroy();
    state.hls = null;
  }
  video.pause();
  video.removeAttribute("src");
  video.load();
  state.media = null;
  $("media").hidden = true;
  $("now-playing").hidden = true;
  if (report) reportMedia();
}

/** Plays a clip over whatever is playing, which is ducked meanwhile. */
function announce(command) {
  if (!video.paused && !state.ducked) {
    state.ducked = video.volume;
    video.volume = video.volume * 0.2;
  }
  announcement.src = command.url;
  announcement.volume = command.volume ?? 1;
  announcement.play().catch(unduck);
}

function unduck() {
  if (state.ducked !== false) video.volume = state.ducked;
  state.ducked = false;
}
announcement.addEventListener("ended", unduck);
announcement.addEventListener("error", unduck);

function showNowPlaying(command) {
  $("now-playing-title").textContent = command.title || "Playing";
  $("now-playing-subtitle").textContent = command.artist || "";
  $("now-playing-subtitle").hidden = !command.artist;
  const art = $("now-playing-art");
  art.hidden = !command.imageUrl;
  if (command.imageUrl) art.src = command.imageUrl;
  $("now-playing").hidden = false;
}

function reportMedia() {
  const m = state.media;
  const media = m && {
    state: video.paused ? "paused" : video.readyState < 3 ? "buffering" : "playing",
    url: m.url,
    mime_type: m.mimeType,
    title: m.title,
    artist: m.artist,
    image_url: m.imageUrl,
    mode: m.mode,
    position: Number.isFinite(video.currentTime) ? Math.round(video.currentTime * 10) / 10 : 0,
    duration: Number.isFinite(video.duration) ? Math.round(video.duration * 10) / 10 : null,
  };
  window.pleatview.send({ type: "media", media });
  $("now-playing-progress").value = media?.duration ? media.position / media.duration : 0;
}
for (const event of ["playing", "pause", "waiting", "seeked", "durationchange"]) video.addEventListener(event, reportMedia);
setInterval(() => state.media && !video.paused && reportMedia(), 5000);
video.addEventListener("ended", () => !video.loop && stop());
video.addEventListener("error", () => {
  if (!state.media) return;
  banner("This video can't be played.", 8);
  stop();
});
video.addEventListener("volumechange", () => window.pleatview.send({ type: "volume", volume: state.ducked === false ? video.volume : state.ducked, muted: video.muted }));

function mediaCommand(command) {
  switch (command.name) {
    case "play_media": return play(command);
    case "play": return state.media && video.play().catch(() => {});
    case "pause": return video.pause();
    case "toggle": return state.media && (video.paused ? video.play().catch(() => {}) : video.pause());
    case "stop": return stop();
    case "seek": return state.media && (video.currentTime = command.position);
    case "set_volume": return (video.volume = command.level);
    case "mute": return (video.muted = command.muted);
  }
}

function image(command) {
  clearTimeout(state.imageTimer);
  const layer = $("image");
  if (!command) {
    layer.hidden = true;
    $("image-img").removeAttribute("src");
    return;
  }
  place(layer, command.layout);
  $("image-img").src = command.url;
  layer.hidden = false;
  if (command.duration) {
    state.imageTimer = setTimeout(() => {
      image(null);
      window.pleatview.send({ type: "overlay-closed" });
    }, command.duration * 1000);
  }
}

// ---- Banner, menu, standby --------------------------------------------------------------

let bannerTimer = null;
function banner(text, seconds = 0) {
  clearTimeout(bannerTimer);
  $("banner").textContent = text || "";
  $("banner").hidden = !text;
  if (text && seconds) bannerTimer = setTimeout(() => ($("banner").hidden = true), seconds * 1000);
}

const menuInfo = (info) => `${info.name} · ${info.address}:${info.port} · PIN ${info.pin.replace(/(\d{3})(\d{3})/, "$1 $2")} · v${info.version}`;

function openMenu(message) {
  $("menu-info").textContent = menuInfo(message.info);
  $("menu-url").textContent = message.haUrl || "";
  const items = $("menu-items");
  items.replaceChildren();
  const add = (label, action) => {
    const button = document.createElement("button");
    button.textContent = label;
    button.addEventListener("click", () => {
      closeMenu();
      action();
    });
    items.appendChild(button);
  };
  if (message.configured) {
    add("Home dashboard", () => window.pleatview.send({ type: "action", action: "home" }));
    for (const d of message.dashboards) add(d.title, () => window.pleatview.send({ type: "action", action: "open", path: d.path }));
    add("Reload", () => window.pleatview.send({ type: "action", action: "reload" }));
  }
  if (message.signIns) {
    add(`Forget saved sign-ins (${message.signIns})`, () => window.pleatview.send({ type: "action", action: "forget-sign-ins" }));
  }
  add("Exit", () => window.pleatview.send({ type: "action", action: "quit" }));
  $("menu").hidden = false;
  window.pleatview.send({ type: "menu", open: true });
  items.firstElementChild?.focus();
}

function closeMenu() {
  if ($("menu").hidden) return;
  $("menu").hidden = true;
  window.pleatview.send({ type: "menu", open: false });
}

$("menu").addEventListener("click", (event) => event.target === $("menu") && closeMenu());
document.addEventListener("keydown", (event) => {
  if ($("menu").hidden) return;
  const buttons = [...document.querySelectorAll("#menu-items button")];
  const index = buttons.indexOf(document.activeElement);
  if (event.key === "ArrowDown") buttons[Math.min(index + 1, buttons.length - 1)]?.focus();
  else if (event.key === "ArrowUp") buttons[Math.max(index - 1, 0)]?.focus();
  else return;
  event.preventDefault();
});
$("curtain").addEventListener("pointerdown", () => window.pleatview.send({ type: "wake" }));

// ---- From the app ---------------------------------------------------------------------

window.pleatview.onMessage((message) => {
  switch (message.type) {
    case "screen":
      window.innerScreenWidth = message.width;
      window.innerScreenHeight = message.height;
      document.documentElement.style.setProperty("--w", `${message.width}px`);
      document.documentElement.style.setProperty("--h", `${message.height}px`);
      regionsChanged();
      break;
    case "offset":
      state.offset = { x: message.x, y: message.y };
      screenEl.style.transform = `translate(${-message.x}px, ${-message.y}px)`;
      break;
    case "setup":
      showSetupInfo(message.info);
      $("setup").hidden = !message.visible;
      break;
    case "setup-info":
      showSetupInfo(message.info);
      break;
    case "notify":
      notify(message.command);
      break;
    case "media":
      mediaCommand(message.command);
      break;
    case "image":
      image(message.command);
      break;
    case "banner":
      banner(message.text);
      break;
    case "menu":
      openMenu(message);
      break;
    case "close-menu":
      closeMenu();
      break;
    case "standby":
      $("curtain").hidden = !message.on;
      break;
    case "approval":
      showApproval(message.approval);
      break;
    case "sign-in":
      showSignIn(message.request, message.qr);
      break;
  }
});

// ---- A dashboard asking for a username and password --------------------------------------

function showSignIn(request, qr) {
  $("signin").hidden = !request;
  if (request) {
    $("signin-title").textContent = `Sign in to ${request.title}`;
    $("signin-failed").hidden = !request.failed;
    $("signin-qr").innerHTML = qr || ""; // an SVG the app made from the phone page's address
    $("signin-password").value = "";
    ($("signin-username").value ? $("signin-password") : $("signin-username")).focus();
  } else {
    $("signin-form").reset();
  }
  regionsChanged();
}

$("signin-form").addEventListener("submit", (event) => {
  event.preventDefault();
  window.pleatview.send({
    type: "sign-in",
    username: $("signin-username").value.trim(),
    password: $("signin-password").value,
    remember: $("signin-remember").checked,
  });
  $("signin-password").value = "";
});
$("signin-not-now").addEventListener("click", () => window.pleatview.send({ type: "sign-in-dismiss" }));
document.addEventListener("keydown", (event) => {
  if (!$("signin").hidden && event.key === "Escape") {
    event.preventDefault();
    window.pleatview.send({ type: "sign-in-dismiss" });
  }
});

// ---- Pairing approved on the screen ----------------------------------------------------

let approvalId = null;

function showApproval(approval) {
  approvalId = approval?.id ?? null;
  $("approval").hidden = !approval;
  if (approval) {
    $("approval-title").textContent = `Pair with ${approval.client}?`;
    $("approval-text").textContent = `${approval.client} at ${approval.address} asks to control this screen. ` +
      `Press Enter to allow it. Only allow it if you just set this screen up in ${approval.client}.`;
    $("approval-allow").focus();
  }
  regionsChanged();
}

function decide(allow) {
  if (!approvalId) return;
  window.pleatview.send({ type: "approval", id: approvalId, allow });
  showApproval(null);
}

$("approval-allow").addEventListener("click", () => decide(true));
$("approval-deny").addEventListener("click", () => decide(false));
document.addEventListener("keydown", (event) => {
  if ($("approval").hidden) return;
  if (event.key === "Escape") decide(false);
  else if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
    (document.activeElement === $("approval-allow") ? $("approval-deny") : $("approval-allow")).focus();
  } else return;
  event.preventDefault();
});

window.innerScreenWidth = window.innerWidth;
window.innerScreenHeight = window.innerHeight;
window.pleatview.send({ type: "ready" });
