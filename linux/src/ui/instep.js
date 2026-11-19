// A wall's piece (a page drawn across several screens, each playing its own part): its
// playlist says how far behind the time each frame was drawn to play
// (EXT-X-SERVER-CONTROL:HOLD-BACK) and the time at its timestamp zero (EXT-X-PLEATVIEW-CLOCK).
// The frame on screen's timestamp is the video's time plus hls.js's timestamp at time zero;
// kept that far behind by this screen's clock (NTP), every screen of the wall shows the same
// frame. (Not the playlist's segment times: in the piece with the sound, a segment's time is
// its first sound's, a little off its first picture's.)
"use strict";

const WRAP = 2 ** 33 / 90000;  // seconds: MPEG-TS timestamps go round every 26.5 hours

// The piece's clock and the delay it asks for, or null if it isn't a wall's piece.
function wallClock(playlist) {
  const clock = /^#EXT-X-PLEATVIEW-CLOCK:(-?\d+(?:\.\d+)?)\s*$/m.exec(playlist);
  const hold = /^#EXT-X-SERVER-CONTROL:.*?\bHOLD-BACK=(\d+(?:\.\d+)?)/m.exec(playlist);
  return clock && hold ? { clock: Number(clock[1]), holdBack: Number(hold[1]) } : null;
}

// When the frame at this timestamp was drawn: of the times the timestamps that go round could
// stand for, the one nearest now.
function drawnAt(clock, timestamp, now) {
  return clock + timestamp + Math.round((now - clock - timestamp) / WRAP) * WRAP;
}

// What to do, from how far behind the frame on screen was drawn (seconds) and the delay asked
// for: nothing, a little faster or slower, a jump forward when late, or a pause when early
// (nothing is kept from before to go back to).
function inStep(behind, holdBack) {
  const off = behind - holdBack;  // more than nothing: late
  if (!Number.isFinite(off)) return { rate: 1 };
  if (off > 1) return { seek: off, rate: 1 };  // late (after a stall): there at once
  if (off < -0.25) return { wait: -off, rate: 1 };  // early (joined near the live edge): a pause for as long
  if (Math.abs(off) <= 0.002) return { rate: 1 };
  // Faster or slower by twice the seconds off (at most 5%): half of it made up each quarter of a
  // second, and a player whose clock runs a little slow (its sound's) held a few ms from the mark.
  return { rate: Math.round((1 + Math.max(-0.05, Math.min(0.05, off * 2))) * 1000) / 1000 };
}

if (typeof module !== "undefined") module.exports = { wallClock, drawnAt, inStep };
