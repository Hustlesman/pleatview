// Plays the live stream of this screen's dashboards that Home Assistant renders
// (docs/render-on-home-assistant.md). Starts again after any error, as the Android app does:
// the stream needs a moment when Home Assistant starts rendering.
"use strict";

const video = document.getElementById("video");
const source = decodeURIComponent(location.hash.slice(1));
let hls = null;
let retry = 0;
let wall = null;  // a wall's piece: { clock, holdBack } from its playlist (instep.js)
let zero = null;  // seconds: the timestamp at the video's time zero

function start() {
  clearTimeout(retry);
  if (hls) hls.destroy();
  zero = null;
  hls = new window.Hls({ liveSyncDurationCount: 2, maxBufferLength: 6, backBufferLength: 0 });
  hls.on(window.Hls.Events.ERROR, (event, data) => {
    if (data.fatal) retry = setTimeout(start, 2000);
  });
  hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
    video.play().catch(() => {});
    readClock();
  });
  hls.on(window.Hls.Events.INIT_PTS_FOUND, (event, data) => { zero = data.initPTS / data.timescale; });
  hls.loadSource(source);
  hls.attachMedia(video);
}

// A live stream that stalls falls behind: catch up rather than play late.
video.addEventListener("waiting", () => {
  if (hls && hls.liveSyncPosition && hls.liveSyncPosition - video.currentTime > 6) video.currentTime = hls.liveSyncPosition;
});

// A wall's piece: kept the delay its playlist asks for behind the time it was drawn (instep.js).
async function readClock() {
  try {
    wall = window.wallClock(await (await fetch(source, { cache: "no-store" })).text());
  } catch {
    // tried again with the next look at the playlist
  }
}
setInterval(readClock, 30000);  // the renderer starting again starts a new clock

let waiting = false;
setInterval(() => {
  if (waiting || !wall || zero === null || video.paused || video.seeking || video.readyState < 3) return;
  const now = Date.now() / 1000;
  const step = window.inStep(now - window.drawnAt(wall.clock, video.currentTime + zero, now), wall.holdBack);
  if (step.seek) video.currentTime += step.seek;
  if (step.wait) {
    waiting = true;
    video.pause();
    setTimeout(() => { waiting = false; video.play().catch(() => {}); }, step.wait * 1000);
  }
  video.playbackRate = step.rate;
}, 250);

start();
