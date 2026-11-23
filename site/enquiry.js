/* The Console page's enquiry form. First-party, no library, nothing from anywhere else.

   - The address's #early-access, #talk or #self-hosting picks what the enquiry is about.
   - When someone starts typing, a ticket comes from our checking service (the form's data-gate).
     It says when they started, so a form sent too quickly to be a person's isn't taken.
   - Sending posts the form as JSON to that service, which checks it and passes it on. Whatever
     goes wrong, everything typed stays in the form. */
(function () {
  "use strict";
  var KINDS = { "#early-access": "early_access", "#talk": "talk", "#self-hosting": "self_host" };
  var form = document.getElementById("enquiry-form");
  if (!form) return;
  var box = form.closest(".enquiry");
  var error = form.querySelector(".form-error[role=alert]");
  var sent = box.querySelector(".sent");
  var button = form.querySelector("button[type=submit]");
  var gate = form.getAttribute("data-gate");
  var failedText = error.textContent;
  var ticket = null;  // a promise of {ticket, at, least}

  function sync() { form.elements.message.required = form.elements.kind.value !== "early_access"; }
  function pick(hash) {
    var kind = KINDS[hash];
    if (kind) { form.elements.kind.value = kind; sync(); }
  }
  function state(name, text) {
    box.setAttribute("data-state", name);
    button.disabled = name === "sending";
    form.setAttribute("aria-busy", String(name === "sending"));
    error.textContent = text || failedText;
    error.hidden = name !== "failed";
    sent.hidden = name !== "sent";
    if (name === "sent") sent.focus();
  }
  function later(ms) { return new Promise(function (done) { setTimeout(done, ms); }); }

  function getTicket() {
    if (!ticket) {
      ticket = fetch(gate + "/v1/ticket", { mode: "cors", credentials: "omit", cache: "no-store" })
        .then(answer)
        .then(function (data) {
          if (!data.ticket) throw new Error(data.error || "");
          return { ticket: data.ticket, at: Date.now(), least: (data.least || 4) * 1000 };
        });
      ticket.catch(function () { ticket = null; });  // (asked again at the next try)
    }
    return ticket;
  }

  // Only our service's own answers are read: anything else (a challenge page, a proxy's error) is a failure.
  function answer(response) {
    if ((response.headers.get("Content-Type") || "").indexOf("application/json") !== 0) throw new Error("");
    return response.json();
  }

  function send(again) {
    var got;
    return getTicket().then(function (t) {
      got = t;
      return later(Math.max(0, t.least + 300 - (Date.now() - t.at)));
    }).then(function () {
      var f = form.elements;
      return fetch(gate + "/v1/enquiry", {
        method: "POST", mode: "cors", credentials: "omit", cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: f.kind.value, name: f.name.value, email: f.email.value, organisation: f.organisation.value,
                               screens: f.screens.value, message: f.message.value, website: f.website.value, ticket: got.ticket }),
      });
    }).then(answer).then(function (data) {
      if (data.ok) return;
      if (data.code !== "quick") ticket = null;  // (used, or no good: a new one for the next try)
      if (!again && (data.code === "quick" || data.code === "ticket")) return send(true);
      var failure = new Error(data.error || "");
      failure.said = ["field", "wait", "busy", "later"].indexOf(data.code) >= 0;  // (the service's own words, for the person)
      throw failure;
    }, function (problem) {
      ticket = null;
      throw problem;
    });
  }

  form.addEventListener("change", function (e) { if (e.target.name === "kind") sync(); });
  form.addEventListener("focusin", function () { getTicket().catch(function () {}); });
  window.addEventListener("hashchange", function () { pick(location.hash); });
  // A link to the hash already in the address fires no hashchange.
  document.addEventListener("click", function (e) {
    var link = e.target.closest && e.target.closest("a[href^='#']");
    if (link) pick(link.hash);
  });
  pick(location.hash);
  sync();

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    state("sending");
    send(false).then(function () { state("sent"); }, function (problem) {
      state("failed", problem && problem.said ? problem.message : "");  // (never the browser's own, such as "Failed to fetch")
      getTicket().catch(function () {});  // (ready for the next try)
    });
  });
})();
