# PleatView protocol v2: screens connect out

Status: being built, November 2026: first in the Android and Linux apps and PleatView Light,
for controllers that reach screens over the internet. Home Assistant keeps
[protocol v1](protocol.md) on the local network for now and moves with the web player
([M4](../ROADMAP.md#m4--web-player)). Part of [S1](../ROADMAP.md#s1--secure-remote-management).

## Why

In v1 the controller (such as Home Assistant) connects to the screen, which listens on port
8765 on the local network. That can't work for screens behind NAT, on another site, in a car,
or in a browser (the web player, LG and Samsung apps), and it means the screen listens for
anyone on its network. In v2 the screen connects out to its controller over TLS and keeps that
connection open. It works through NAT, VPNs and zero-trust access connectors (ZTNA) without
opening a port at the screen's end.

The calls stay those of v1, so the apps' logic doesn't change: only how they're carried.

## Keys

Each screen makes one key pair, ECDSA on P-256, in the platform's key store (the Android
Keystore; elsewhere a file only the app's user can read). The private key never leaves it.

- The **public key** travels as base64 of its DER `SubjectPublicKeyInfo`.
- The **key id** is hex of the SHA-256 of that DER: what a controller files the screen under.
- A **signature** is ECDSA with SHA-256 over the UTF-8 text below, as base64 of its DER form.

The signed text binds a fresh challenge to the address the screen was told to use:

```text
pleatview-v2
wss://controller.example/api/pleatview/v2
<nonce>
```

That's `pleatview-v2`, the controller's address, and the nonce, each followed by a newline
(`\n`) except the last. The address is the one the screen connects to, written as
`wss://host[:port]/api/pleatview/v2`: scheme and host in lower case, no port when it's 443. The
controller accepts only its own addresses (it can have several, such as one on the local
network and one behind a zero-trust connector), so a signature made for another controller is
useless to it. Binding to the address rather than to the TLS channel works in browsers, in
Python's standard library and on older Android, and through connectors that end TLS on the way.

## Connecting

1. The screen opens `wss://host[:port]/api/pleatview/v2` with the WebSocket subprotocol
   `pleatview.v2`, over TLS 1.3 (TLS 1.2 only where the platform has nothing newer).
2. It checks the controller's certificate against the system's authorities, or against the
   certificate it pinned when enrolling (hex of the SHA-256 of the certificate's DER), for
   controllers with a certificate of their own.
3. The controller sends `{"type": "challenge", "nonce": "<64 hex digits>"}`, fresh for each
   connection.
4. An enrolled screen answers `{"type": "auth", "key": "<key id>", "signature": "…",
   "info": {…}}`, where `info` is what v1's `GET /api/info` returns. A screen that isn't
   enrolled sends `enrol` instead (below).
5. The controller checks the signature against the public key it registered for that key id
   and answers `{"type": "welcome"}`, or closes the connection with one of the codes below.

All messages are JSON objects, one per WebSocket text frame, with a `type`.

## Enrolment

A screen enrols once with a controller, in one of two ways. In both it sends
`{"type": "enrol", "key": "<public key>", "signature": "…", "info": {…}}` in answer to the
challenge, signed as above, which proves it holds the key.

**With a token, on the local network.** A controller that has already paired with the screen
over v1 (with its PIN, or one press of OK) sends `POST /api/enrol` with its v1 token:
`{"url": "wss://…/api/pleatview/v2", "token": "<one-time token>", "fingerprint": "<optional>"}`.
The screen answers `202` and connects, and its `enrol` carries the `token`. The controller
accepts a token once, for a few minutes, and only from the screen it gave it to (the `id` in
`info`), then answers `welcome`. `fingerprint` pins a controller's own certificate. Nobody types
anything beyond the pairing they already did.

**With a code, anywhere.** The screen is given the controller's address (typed in its menu,
or from its phone page). Its `enrol` has no `token`, and the controller answers
`{"type": "code", "code": "K7RM-W2XD", "expires_in": 600}`. The screen shows the code, and
someone allowed to add screens types it into the controller, which then sends `welcome`. Codes
are 8 characters from `23456789ABCDEFGHJKMNPQRSTUVWXYZ` (no 0, O, 1, I or L), shown as two
groups of four. What people type is read in upper case without spaces or dashes. The
controller limits how many codes it hands out and how many guesses it takes.

After `welcome`, an enrolled screen keeps the controller's address (and pinned certificate)
and signs in with `auth` from then on.

## Messages

| From | Message | Meaning |
| --- | --- | --- |
| Screen | `{"type": "state", "state": {…}}` | v1's state object, after `welcome` and after every change |
| Controller | `{"type": "request", "id": 7, "method": "…", "params": {…}}` | A v1 call (below) |
| Screen | `{"type": "response", "id": 7, "result": {…}}` | Its answer, the body v1 would return |
| Screen | `{"type": "response", "id": 7, "error": "…"}` | Its refusal, with v1's reason |
| Either | `{"type": "ping"}` / `{"type": "pong"}` | Every 20 seconds; a minute without either closes the connection |

| Method | Same as in v1 | `params` |
| --- | --- | --- |
| `info` | `GET /api/info` | none |
| `state` | `GET /api/state` | none |
| `command` | `POST /api/command` | the command and its parameters |
| `config` | `GET /api/config` | none |
| `set_config` | `POST /api/config` | a partial configuration |
| `signins` | `GET /api/signins` | none |
| `forget_signins` | `DELETE /api/signins` | `{"site": "…"}` or `{}` |
| `signin` | `POST /api/signin` | `{"username": "…", "password": "…"}` |

v1's pairing, `verify` and `unpair` have no v2 method: enrolment and revocation replace them.

## Closing

| Code | Meaning | The screen |
| --- | --- | --- |
| 4400 | A message it couldn't read | Reconnects |
| 4401 | Unknown key | Forgets the controller (it was removed while the screen was away) |
| 4403 | Revoked, or enrolment refused | Forgets the controller |
| 4408 | The enrolment code expired | Asks for a new one by connecting again |
| 4409 | The same screen connected again | Stays away until it's told otherwise |

Otherwise the screen reconnects with growing waits, from 2 seconds doubling to a minute,
give or take a fifth, and at once when its network comes back. Removing a screen in the
controller revokes its key immediately: the open connection closes with 4403 and later ones
are refused.

## Thin agent

The screen's part is small: a key pair, a TLS WebSocket, and v1's calls. Approval,
revocation, logs, policies and certificates live in the controller.

## Moving from v1

Screens keep v1 on the local network for setup and for controllers that only speak v1. A
screen can be enrolled with one v2 controller at a time, and stay paired with v1 controllers.

## Decisions

- **Keys:** ECDSA P-256, which every Android Keystore has since Android 6 and browsers'
  WebCrypto has too (the web player).
- **Signatures bind the controller's address,** not the TLS channel (RFC 9266): channel
  binding isn't available to web pages, in Python's standard library or before Android 12,
  and connectors that end TLS would break it.
- **PleatView Light** signs with `python3-cryptography` from Debian.
- **The controller's certificate:** one the screen's system trusts, or one pinned at
  enrolment.
- **Test vectors** for the signed text, key ids and signatures are in
  [`tests/fixtures/protocol-v2.json`](../tests/fixtures/protocol-v2.json); every
  implementation checks them.
