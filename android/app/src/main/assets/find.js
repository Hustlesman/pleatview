// Where a page step presses (docs/page-steps.md): the middle of the first element on the
// screen that shows the wanted text, whatever its capitals (buttons and links first, then any
// element of its own, a div included), or that matches the wanted CSS selector. In CSS
// pixels, with the page's width and height; null while there's none, "bad selector" for a
// selector the page can't read. Shared by the Android and Linux apps; the renderer has a
// copy, checked to be the same.
((wanted) => {
  const shown = (el) => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.pointerEvents !== 'none'
      && r.right > 0 && r.bottom > 0 && r.left < innerWidth && r.top < innerHeight;
  };
  let el = null;
  if (wanted.css) {
    try {
      el = [...document.querySelectorAll(wanted.css)].find(shown);
    } catch (e) {
      return 'bad selector';
    }
  } else {
    const text = String(wanted.text).trim().replace(/\s+/g, ' ').toLowerCase();
    const label = (e) => (e.innerText || e.value || e.getAttribute('aria-label') || e.title || '')
      .trim().replace(/\s+/g, ' ').toLowerCase();
    const pick = (selector, own) => [...document.querySelectorAll(selector)]
      .find((e) => (!own || e.children.length === 0) && label(e) === text && shown(e));
    el = pick('button, a, [role=button], input[type=button], input[type=submit], summary, label', false)
      || pick('body *', true);
  }
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return [r.left + r.width / 2, r.top + r.height / 2, innerWidth, innerHeight];
})
