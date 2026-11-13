# PleatView protocol v2: screens connect out (design)

Status: a design for [S1](../ROADMAP.md#s1--secure-remote-management) and the web player
([M4](../ROADMAP.md#m4--web-player)), November 2026. It's built with M4,
so Home Assistant and other controllers get it in the same app version.

## Why

In [protocol v1](protocol.md) the controller (such as Home Assistant) connects to
the screen, which listens on port 8765 on the local network. That can't work for screens
behind NAT, on another site, in a car, or in a browser (the web player, LG and Samsung
apps), and it means the screen listens for anyone on its network. In v2 the screen connects
out to its controller over TLS and keeps that connection open. It works through NAT, VPNs and
zero-trust access connectors (ZTNA) without opening a port at the screen's end.

The commands, state and configuration stay those of v1, so the apps' logic doesn't change:
only how they're carried.

## The connection

1. The screen opens `wss://<controller>/api/pleatview/v2` (TLS 1.3; TLS 1.2 only where the
   platform has nothing newer).
2. It checks the controller's certificate against the system's authorities, or against the
   certificate it pinned when enrolling (self-signed controllers).
3. The controller sends a challenge (32 random bytes). The screen signs it, with the
   connection's TLS channel binding (RFC 9266, `tls-exporter`), using its device key, and
   sends the signature with its device id and app version.
4. The controller checks the signature against the public key it registered at enrolment.
   Unknown or revoked keys are refused and logged.
5. From then on, JSON messages in both directions, one per WebSocket text frame:

| From | Message | Body |
| --- | --- | --- |
| Screen | `state` | The v1 state object, on connecting and after every change |
| Controller | `command` | `{"id": 7, "command": "show_url", …}`, the v1 command and its parameters |
| Screen | `result` | `{"id": 7, "ok": true}` or `{"id": 7, "error": "…"}` |
| Controller | `config` | A partial v1 configuration; the screen answers with `result` and its new configuration |
| Either | `ping` / `pong` | Every 20 seconds; a minute of silence closes the connection |

The screen reconnects with growing waits (2 s to 1 min), and the controller re-sends
the configuration when the screen's `config_rev` differs, as in v1.

## Enrolment

1. On first start, the screen makes its key pair in the platform's key store (the Android
   Keystore; elsewhere a file only its user can read). The private key never leaves it.
2. The user gives the screen the controller's address (typed, scanned from a QR code, or
   found on the local network), and the screen shows an 8-character code.
3. The screen sends an enrolment request: its public key, name, model and the code.
4. In the controller, someone allowed to pair screens approves the request after checking
   the code on the screen. The controller registers the public key for that screen.
5. Self-signed controllers: the screen shows the controller's certificate fingerprint at
   step 2, and pins that certificate once approved.

Removing the screen in the controller revokes its key immediately: the open connection is
closed, and later ones are refused.

## Thin agent

The screen's part is small: a key pair, a TLS WebSocket, and v1's messages. Approval,
revocation, logs, policies and certificates live in the controller.

## Moving from v1

Screens keep v1 on the local network for setup and for controllers that only speak v1.
Once enrolled with a v2 controller, a screen can turn its v1 server off (a setting the
controller sets). Home Assistant's integration serves `/api/pleatview/v2` itself, so the
same app works with Home Assistant and with other controllers.

## Decisions

- **Keys:** ECDSA P-256, which every Android Keystore has since Android 6 and browsers'
  WebCrypto has too (the web player).
- **PleatView Light** signs with `python3-cryptography` from Debian.
- **The controller's certificate:** one the screen's system trusts, or one pinned at
  enrolment.
- **Home Assistant and other controllers together,** with M4.
