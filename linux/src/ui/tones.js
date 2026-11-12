// The tones of warnings and alerts, made here as the Android app makes them (Tones.kt):
// each a few notes. Warnings get short, gentle ones; alerts louder, longer ones. The
// renderer plays the same file into a rendered page (renderer/pleatview_renderer/tones.js).
"use strict";

(function () {
  const bell = (hz, ms, level, gap = 0) => ({ from: hz, to: hz, ms, level, bell: true, gap });
  const flat = (hz, ms, level, gap = 0) => ({ from: hz, to: hz, ms, level, bell: false, gap });
  const glide = (from, to, ms, level) => ({ from, to, ms, level, bell: false, gap: 0 });
  const times = (n, notes) => Array.from({ length: n }, () => notes).flat();

  const TONES = {
    chime: [bell(1318.5, 220, 0.4, 30), bell(1046.5, 520, 0.4)],
    ding: [bell(1568, 800, 0.4)],
    beep: [flat(880, 110, 0.3, 80), flat(880, 110, 0.3)],
    alarm: times(3, [flat(880, 180, 0.7, 30), flat(1174.7, 180, 0.7, 120)]),
    siren: times(3, [glide(650, 1300, 420, 0.7), glide(1300, 650, 420, 0.7)]),
    bell: times(3, [bell(1046.5, 650, 0.85, 60)]),
  };

  /** Plays a tone by name; unknown names and "none" make no sound. */
  function play(name) {
    const notes = TONES[name];
    if (!notes) return;
    // One context for the page, also when this file runs again (the renderer runs it per tone).
    const context = window.__pleatviewToneContext || (window.__pleatviewToneContext = new AudioContext());
    let at = context.currentTime + 0.05;
    for (const note of notes) {
      const seconds = note.ms / 1000;
      const gain = context.createGain();
      gain.connect(context.destination);
      const partials = note.bell ? [[1, 1 / 1.5], [2, 0.35 / 1.5], [3, 0.15 / 1.5]] : [[1, 1]];
      for (const [multiple, share] of partials) {
        const osc = context.createOscillator();
        const level = context.createGain();
        level.gain.value = share;
        osc.frequency.setValueAtTime(note.from * multiple, at);
        osc.frequency.linearRampToValueAtTime(note.to * multiple, at + seconds);
        osc.connect(level).connect(gain);
        osc.start(at);
        osc.stop(at + seconds);
      }
      // 6 ms in and out, so notes don't click; a bell then fades away.
      gain.gain.setValueAtTime(0, at);
      gain.gain.linearRampToValueAtTime(note.level, at + 0.006);
      if (note.bell) gain.gain.setTargetAtTime(0, at + 0.006, seconds * 0.3);
      else gain.gain.setValueAtTime(note.level, at + seconds - 0.006);
      gain.gain.linearRampToValueAtTime(0, at + seconds);
      at += seconds + note.gap / 1000;
    }
  }

  window.pleatviewTones = { play, names: Object.keys(TONES) };
})();
