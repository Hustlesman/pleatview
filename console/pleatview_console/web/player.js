// PleatView Console's playlist player: what a screen shows for a playlist page. Plays the
// pictures, videos and sounds in order, full screen, again and again, and picks up changes
// to the playlist each time round.
"use strict";

const stage = document.getElementById("stage");
const itemsUrl = `${location.pathname.replace(/\/$/, "")}/items`;
let list = [];
let index = 0;
let timer = 0;

function message(text) {
  const p = document.createElement("p");
  p.className = "message";
  p.textContent = text;
  stage.replaceChildren(p);
}

async function load() {
  try {
    const response = await fetch(itemsUrl, { cache: "no-store" });
    if (response.ok) list = (await response.json()).items;
  } catch {
    // keep the last list: the console may be restarting
  }
}

function next(delay = 0) {
  clearTimeout(timer);
  timer = setTimeout(show, delay);
}

async function show() {
  if (index >= list.length || list.length === 0) {
    index = 0;
    await load();
  }
  if (list.length === 0) {
    message("Nothing to play yet.");
    next(15000);
    return;
  }
  const item = list[index++];
  if (item.kind === "image") {
    const img = document.createElement("img");
    img.src = item.url;
    img.alt = "";
    img.onerror = () => next(1000);
    stage.replaceChildren(img);
    next(item.seconds * 1000);
    return;
  }
  const media = document.createElement(item.kind === "audio" ? "audio" : "video");
  media.src = item.url;
  media.autoplay = true;
  media.playsInline = true;
  media.onended = () => next();
  media.onerror = () => next(1000);
  stage.replaceChildren(media);
  media.play().catch(() => {
    // A browser that won't play sound by itself plays it muted instead.
    media.muted = true;
    media.play().catch(() => next(1000));
  });
}

show();
