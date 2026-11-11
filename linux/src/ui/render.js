// Plays the live stream of this screen's dashboards that Home Assistant renders
// (docs/render-on-home-assistant.md). Starts again after any error, as the Android app does:
// the stream needs a moment when Home Assistant starts rendering.
"use strict";

const video = document.getElementById("video");
const source = decodeURIComponent(location.hash.slice(1));
let hls = null;
let retry = 0;

function start() {
  clearTimeout(retry);
  if (hls) hls.destroy();
  hls = new window.Hls({ liveSyncDurationCount: 2, maxBufferLength: 6, backBufferLength: 0 });
  hls.on(window.Hls.Events.ERROR, (event, data) => {
    if (data.fatal) retry = setTimeout(start, 2000);
  });
  hls.on(window.Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
  hls.loadSource(source);
  hls.attachMedia(video);
}

// A live stream that stalls falls behind: catch up rather than play late.
video.addEventListener("waiting", () => {
  if (hls && hls.liveSyncPosition && hls.liveSyncPosition - video.currentTime > 6) video.currentTime = hls.liveSyncPosition;
});

start();
