# Security policy

Please report security problems privately, not in a public issue. Email
privacy@hustlesman.com, or use **Report a vulnerability** under this repository's
*Security* tab.

Include what you found and how to reproduce it. We reply as soon as we can, usually
within a few days, and credit reporters who want it.

## How PleatView is protected

PleatView is built for a home network you trust. What it does, and what it can't do:

- **Pairing.** Home Assistant pairs with the 6-digit PIN shown on the TV. Five wrong PINs
  lock pairing for a minute and change the PIN. The TV keeps only hashes of the tokens it
  issues. When a TV announces a new address, Home Assistant moves to it only after the TV
  proves it issued Home Assistant's token, so another device can't take its place.
- **Home Assistant's token** goes only to the Home Assistant it was made for. The
  integration creates a separate, local-only user without admin rights for each TV; under
  *Reconfigure* it can be read-only. Anyone who can use the TV's remote can see the PIN and
  use the dashboards, as on any wall display.
- **Sign-ins to other sites** are encrypted with a key that never leaves the TV's Android
  keystore, and each one is offered only to the site it was made for (a password prompt's,
  only on the dashboard that asked). The phone page shows the address a password goes to.
  Nothing is backed up or copied to another device.
- **Plain HTTP on the local network.** The TV, Home Assistant and the phone setup page talk
  over plain HTTP, like most local smart-home devices. Someone who can read your network's
  traffic could see the tokens and a sign-in typed on the phone. Use a network only your
  household can join (WPA2 or WPA3), not a guest or shared one.
- **The TV's web engine.** Dashboards and web pages run in the TV's Android System WebView.
  Keep the TV updated: Fire TV and some older TVs update it slowly. Only show sites you trust.
- **Phones and tablets with a screen lock.** On a device with a PIN, pattern or password,
  Home Assistant can't show PleatView over the lock screen or wake the screen for it: a
  dashboard's controls would otherwise work without unlocking. Casts wait until it's
  unlocked. On TVs, and on tablets whose lock is None or Swipe, a cast wakes the screen as
  before. Versions up to 1.0.6 showed the dashboard over any lock screen.
- **The PleatView Renderer** (optional, [Render on Home Assistant](docs/render-on-home-assistant.md))
  runs a TV's dashboards in Chromium on the Home Assistant machine, signed in with that
  TV's token. Its own interface has no sign-in: keep it off the network, as the Home Assistant app
  (Home Assistant's internal network only) and the documented `docker run` (127.0.0.1)
  do. Chromium runs as an unprivileged user. The stream reaches the TV through Home
  Assistant, at an address with a secret only that TV gets. The image holds only free
  software from Debian's main archive on a pinned base; `/api/packages` lists every
  package and version.
- **The PleatView Console** (in development, [docs/console.md](docs/console.md)) keeps each
  organisation's data in its own database, with secrets encrypted by the organisation's own
  key. Passwords are hashed with scrypt, sign-ins slow down after five failures, and every
  change is in the organisation's activity log. It serves HTTPS with its own certificate,
  or plain HTTP on `127.0.0.1` behind a proxy that adds TLS.
- **Releases** are built on GitHub from the tagged source, signed with the same key every
  time, and come with build provenance: `gh attestation verify pleatview.apk --repo
  Hustlesman/pleatview`.

## Home Assistant's version

Use Home Assistant 2026.7 or later. Older versions have published security bugs in
dashboard cards, such as CVE-2026-91130 in the statistics graph card. A TV shows its
dashboard all day, signed in, so a malicious card could act as the TV's user. PleatView
still works with Home Assistant 2025.1 and later, but shows a repair under *Settings →
Repairs* until Home Assistant is updated.
