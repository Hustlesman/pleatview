/* The Console's prices in the visitor's currency: our own round prices in each (not exchange
   rates), excluding GST and VAT. The currency of the browser's own time zone comes first: New
   Zealand dollars in New Zealand, Australian dollars in Australia, pounds in the UK, euros
   elsewhere in Europe, US dollars everywhere else. The visitor can change it here; nothing is
   asked of anyone, and nothing is kept (no cookie, no storage). Without this script the page
   says US dollars, as written. */
(function () {
  "use strict";
  var TABLE = {
    usd: { sign: "US$", name: "US dollars", standard: ["5.50", "6.50"], pro: ["9", "11"], enterprise: ["12", "14.50"] },
    nzd: { sign: "NZ$", name: "New Zealand dollars", standard: ["9", "11"], pro: ["15", "18"], enterprise: ["20", "24"] },
    aud: { sign: "A$", name: "Australian dollars", standard: ["8", "9.50"], pro: ["13", "15.50"], enterprise: ["17", "20.50"] },
    gbp: { sign: "£", name: "pounds", standard: ["4", "5"], pro: ["7", "8.50"], enterprise: ["9", "11"] },
    eur: { sign: "€", name: "euros", standard: ["5", "6"], pro: ["8", "9.50"], enterprise: ["11", "13"] }
  };

  function local() {
    var zone = "";
    try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { /* (an old browser: US dollars) */ }
    if (/^(Pacific\/(Auckland|Chatham)|NZ)/.test(zone)) return "nzd";
    if (/^Australia\//.test(zone)) return "aud";
    if (/^Europe\/(London|Guernsey|Jersey|Isle_of_Man)$/.test(zone)) return "gbp";
    if (/^Europe\//.test(zone)) return "eur";
    return "usd";
  }

  function each(selector, fn) { Array.prototype.forEach.call(document.querySelectorAll(selector), fn); }

  function show(currency) {
    var t = TABLE[currency];
    each("[data-sign]", function (el) { el.textContent = t.sign; });
    each("[data-currency-name]", function (el) { el.textContent = t.name; });
    each("[data-price]", function (el) {  // "standard:yearly"
      var parts = el.getAttribute("data-price").split(":");
      el.textContent = (el.hasAttribute("data-with-sign") ? t.sign : "") + t[parts[0]][parts[1] === "monthly" ? 1 : 0];
    });
    each("[data-price-line]", function (el) {  // "standard": US$5.50 yearly, US$6.50 monthly
      var plan = t[el.getAttribute("data-price-line")];
      el.textContent = t.sign + plan[0] + " yearly, " + t.sign + plan[1] + " monthly";
    });
    each("select[data-currency-pick]", function (el) { el.value = currency; });
  }

  each("select[data-currency-pick]", function (el) {
    el.addEventListener("change", function () { show(el.value); });
    el.closest("[hidden]") && (el.closest("[hidden]").hidden = false);
  });
  show(local());
})();
