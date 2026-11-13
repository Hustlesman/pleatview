# Security baseline

Status: a self-assessment, 12 Nov 2026, for [S1](../ROADMAP.md#s1--secure-remote-management).
It isn't a certification. Anyone can check each point against the code in this repository.

PleatView is held against **ETSI EN 303 645**, the European baseline for connected consumer
devices, provision by provision. The table says how each part meets it today and what's
still to do. The same points cover most of what the EU **Cyber Resilience Act** asks of a
product's security; the last section lists what that act adds.

The parts: the screen apps (Android and Fire TV, Linux, PleatView Light), the Home Assistant
integration and the PleatView Renderer.

## ETSI EN 303 645

| Provision | How PleatView meets it | Still to do |
| --- | --- | --- |
| **5.1 No universal default passwords** | Every screen and renderer makes its own random 6-digit PIN, shown only on it (screens) or in its log (renderers), and changes it after each pairing; five wrong PINs lock pairing for a minute. | — |
| **5.2 Manage vulnerability reports** | [SECURITY.md](../SECURITY.md): a private address and GitHub's private reporting; reporters credited | State response times (acknowledge within 7 days, a fix or plan within 90) |
| **5.3 Keep software updated** | Store installs update by themselves. Releases are tagged, built on GitHub and announced. Dependencies are pinned and scanned; Home Assistant gets a repair when it's too old for safe dashboards | Update prompts for GitHub and `.deb` installs (an APT repository); a stated support period for each version; renderer images that update by themselves |
| **5.4 Store security parameters securely** | Screens keep only hashes of the tokens they issue. Android keeps sign-ins encrypted in its keystore; the Linux app with a key in a file only its user can read. | Home Assistant's token on the TV encrypted like sign-ins (M3); device keys in the platform key store (S1) |
| **5.5 Communicate securely** | Renderers serve HTTPS with a certificate whose fingerprint is pinned at pairing. A screen proves it issued a token before Home Assistant moves to its new address | Screens and Home Assistant still talk plain HTTP on the local network: encrypted with a pinned certificate in M3, and connections out over TLS 1.3 with device keys in [protocol v2](protocol-v2.md) (S1) |
| **5.6 Minimise exposed attack surfaces** | Screens open one port (8765), with no debug interfaces in release builds. Renderers let in nobody unpaired on the network. Unused Android permissions are absent; Linux kiosks run as an ordinary user, Chromium in the renderer as an unprivileged one | Screens stop listening at all once they connect out (S1) |
| **5.7 Ensure software integrity** | Releases are signed with one key, with build provenance (`gh attestation verify`); store builds are also signed by the stores. Images build from this repository on base images pinned by digest | Sign the APT repository; verify the image digest at install |
| **5.8 Ensure personal data is secure** | Nothing leaves the user's network: no accounts with us, no telemetry, no analytics, no ads. Dashboards and media go from the user's Home Assistant to their screens | — |
| **5.9 Be resilient to outages** | Screens keep their last page when the controller is gone, reconnect by themselves and follow a moved controller; the renderer restarts screens nobody watches when asked again; the Android app and PleatView Light keep a controller's playlists in their own cache and play them offline, also after a restart | — |
| **5.10 Examine telemetry** | No telemetry is collected. Logs stay on the user's machines | — |
| **5.11 Make deleting user data easy** | Removing a screen in Home Assistant resets it and revokes the pairing | A "forget everything" button on the screens themselves |
| **5.12 Make installation and maintenance easy** | Screens and renderers are found by themselves and paired with a PIN; a renderer is chosen from a list | — |
| **5.13 Validate input data** | Configuration and commands are checked against fixed lists on screens; typed addresses must be on a local network | Fuzz the protocol parsers |

## What the Cyber Resilience Act adds

Whether the act applies to free software given away without charge is a question for the
project's owner. If it does, or for businesses that ship PleatView in a product of theirs:

- **A software bill of materials:** the images' package lists and the apps' dependency lists
  exist; publish them as SPDX or CycloneDX with each release.
- **Vulnerability handling for the support period**, with security fixes free of charge.
- **Reporting actively exploited vulnerabilities** to ENISA's single reporting platform
  within 24 hours of learning of them (obligation since 11 September 2026).
- **A support period** stated for each version.

## Threats from managing screens remotely (S1)

| Threat | Answer |
| --- | --- |
| Someone on the internet reaches a screen | Screens open no port to the internet: they connect out, and only to the controller whose certificate they enrolled with |
| A stolen or copied screen token | Tokens are short-lived and renewed with the device key, which never leaves the screen's key store; removing a screen revokes its key at once |
| Someone poses as a screen | A screen proves its key on every connection; enrolment needs a code shown on the screen and approval in the controller |
| A network in between reads or changes traffic | TLS 1.3 only, with the controller's certificate checked (or pinned) |
| A compromised controller | It can only do what the protocol allows: show pages, play media, change settings; screens run no code from it beyond the pages they show |
