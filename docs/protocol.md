# PleatView control protocol (v1)

The TV app runs a small HTTP server (default port **8765**) and advertises it over
mDNS as `_pleatview._tcp` with TXT records `id`, `name`, `api` and `version`. The
Home Assistant integration (or any other client) talks to it with JSON over HTTP.

All responses are JSON unless noted. Errors look like `{"error": "message"}`.
Each request uses its own connection (`Connection: close`). Request bodies need a
`Content-Length` and are limited to 256 KiB.

## Pairing and authentication

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `GET /api/info` | none | Identity: `id`, `name`, `manufacturer`, `model`, `app_version`, `api_version`, `paired` |
| `POST /api/pair` | none | `{"pin": "123456", "client": "Home Assistant"}` → `{"token": "…"}` |
| `POST /api/verify` | none | `{"nonce": "<32–128 hex digits>"}` → `{"proofs": ["…"]}` (app 1.0.4 and later; see below) |
| `POST /api/unpair` | bearer | Revokes the calling token |
| `GET /` | none | Phone setup page (it pairs with the PIN itself) |
| `POST /api/signin` | bearer or PIN | Username and password for the page waiting for one (see [Sign-ins](#sign-ins)) |
| `GET /api/signins` | bearer | `{"sign_ins": [{"kind": "form", "site": "…", "username": "…"}]}`, never passwords |
| `DELETE /api/signins` | bearer | `{"site": "…"}` forgets that site's sign-ins, `{}` all → `{"forgotten": 2}` |

The 6-digit PIN is shown on the TV's setup screen and in its menu (press Back on
the home dashboard). It changes after every successful pairing. Wrong PINs return
`403 {"error": "invalid_pin"}`; after 5 wrong attempts pairing locks for 60 s and
the PIN changes (`429 {"error": "too_many_attempts", "retry_after": 60}`).

Every other endpoint needs `Authorization: Bearer <token>` and returns `401` without
a valid one. The TV stores only SHA-256 hashes of tokens and keeps the newest 8.

Anyone on the network can announce a TV's `id`, so before a client sends its token to a
new address it asks that address to prove it issued the token. It sends a fresh random
`nonce`; the TV answers with one proof per paired client:
`hex(HMAC-SHA256(key = hex(SHA-256(token)), message = nonce))`, both as UTF-8 text. The
client computes the same value from its own token and moves only if one of the proofs
matches. Test vector: token `token-1` and nonce `00112233445566778899aabbccddeeff` give
`cfd2cb0b0606646c50484f870216c1870afd7f225109845b6435fdfd40887c7e`.

## State

`GET /api/state` returns the current snapshot. `GET /api/events` is a Server-Sent
Events stream that sends the full snapshot on connect and after every change
(`event: state`), plus a `: ping` comment every 20 s.

```json
{
  "foreground": true,
  "power": "on",
  "view": "dashboard",
  "url": "http://192.168.1.10:8123/lovelace/0",
  "dashboard": "/lovelace/0",
  "ha_connected": true,
  "auth_error": false,
  "last_error": null,
  "overlay": {"kind": "image", "url": "http://…/snapshot.jpg", "mode": "pip"},
  "media": {
    "state": "playing",
    "url": "http://…/master_playlist.m3u8",
    "mime_type": "application/vnd.apple.mpegurl",
    "title": "Front door",
    "artist": null,
    "image_url": null,
    "mode": "pip",
    "position": 12.4,
    "duration": null,
    "updated_at": 1761650000.25
  },
  "volume": 1.0,
  "muted": false,
  "config_rev": "3f9a…",
  "overlay_permission": false,
  "sign_in": null,
  "battery": {"level": 87, "charging": true},
  "screen": {"width": 1920, "height": 1080, "density": 2.0},
  "features": ["render_stream"],
  "app_version": "0.1.0"
}
```

- `foreground`: PleatView is the visible app. `power`: `on` or `standby` (blanked).
- `view`: `setup` (no Home Assistant URL yet) or `dashboard`.
- `dashboard`: path on Home Assistant currently shown, or null for other sites.
- `ha_connected`: the Home Assistant frontend's websocket state, from its heartbeat.
- `media.state`: `playing`, `paused` or `buffering`; `duration` is null for live streams.
- `overlay_permission`: whether the app may draw over other apps / start itself
  from the background.
- `sign_in`: null, or the page waiting for a username and password:
  `{"kind": "prompt" | "form", "title": "Grafana", "site": "http://grafana.lan:3000", "failed": false}`.
  `title` is the dashboard's name; `failed` means the last sign-in for it was rejected.
- `battery`: null on devices without one (TVs), else `level` in percent and `charging`,
  which means plugged in, also when full. Home Assistant shows them as sensors, for
  example to switch a wall tablet's smart plug on and off.
- `screen`: the whole screen in pixels, and pixels per CSS pixel at zoom 100, so Home
  Assistant can render dashboards to match (null until the app has been on screen).
- `features`: what the app can do beyond this version of the protocol. Home Assistant only
  sends configuration a feature needs to apps that list it. `render_stream`: the app plays
  `render_stream` (1.0.14 and later; not the Linux app yet).

## Commands

`POST /api/command` with `{"command": "<name>", …parameters}`. Invalid commands get
`400` with a reason. Display commands bring PleatView to the front when it's in the
background, if Android allows it (see `overlay_permission`).

| Command | Parameters |
| --- | --- |
| `show_dashboard` | `path` (path on HA or full URL), `duration`? (s, then return home) |
| `show_url` | `url`, layout |
| `show_image` | `url` (image or MJPEG stream), layout |
| `play_media` | `url` (http(s)/rtsp(s)), `mime_type`?, `title`?, `artist`?, `image_url`?, `loop`?, `volume`? (0–1), `start_position`? (s), `announce`?, layout |
| `notify` | `message`, `title`?, `image_url`?, `duration`? (s, default 10), `position`? |
| `play`, `pause`, `stop` | — (`stop` also closes web/image overlays) |
| `seek` | `position` (s) |
| `set_volume` | `level` (0–1, PleatView's own playback volume) |
| `mute` | `muted` (bool) |
| `home` | — stop media, close overlays, show the home dashboard |
| `reload` | — reload the dashboard page |
| `next_dashboard`, `previous_dashboard` | — cycle through configured dashboards |
| `turn_on`, `turn_off` | — leave / enter standby (black screen, screen may sleep) |
| `forget_sign_ins` | `site`? — forget the saved sign-ins for one site, or all |

Layout parameters: `mode` (`fullscreen`, `pip`, or `background` for audio),
`position` (`top-left`, `top-right`, `bottom-left`, `bottom-right`, `center`),
`size` (picture-in-picture width, 10–100 % of the screen), `duration` (s).

`play_media` picks a player from `mime_type` or the URL's extension: audio plays in
the background with a now-playing card, video full screen, and images or
`multipart/x-mixed-replace` (MJPEG) are shown like `show_image`. HLS, DASH, RTSP and
progressive files are played by ExoPlayer. `announce: true` plays the clip over
whatever is playing, which is ducked meanwhile.

## Sign-ins

Dashboards that aren't Home Assistant can ask for a username and password, as a
browser password prompt (HTTP basic auth, identified by host and realm) or as a
login form (identified by the page's origin). The TV answers with a sign-in saved
for exactly that site. A password prompt names only its host, so its sign-in is also
kept for the origin of the dashboard that asked, and used only on that dashboard.
Without one, or when the site rejects it, it reports
`sign_in` in its state and shows a card with a QR code. The code opens the phone page
at `#signin=<title>&to=<address>&pin=<PIN>`, where `<address>` is where the password goes
(`host[:port]`, or a password prompt's host), and the phone sends:

```json
POST /api/signin
{"pin": "123456", "username": "kiosk", "password": "…", "remember": true}
```

A bearer token works instead of `pin`. The PIN is used up like a pairing (it changes
afterwards, with the same lockout), but no token is issued. Replies: `200`, `400` without a
password, `403 invalid_pin`, `409 no_sign_in_pending`, `429 too_many_attempts`.

A sign-in is saved only once the site accepts it, and only if `remember` is true.
Saved sign-ins stay on the TV, encrypted with a key in the Android keystore, and are
never sent to Home Assistant.

## Configuration

`GET /api/config` and `POST /api/config` (partial update; unknown keys are rejected).

| Key | Default | Meaning |
| --- | --- | --- |
| `device_name` | TV's name | Name advertised over mDNS |
| `ha_url` | — | Home Assistant base URL; empty shows the setup screen |
| `ha_token` | — | Long-lived access token, write-only (`ha_token_set` is returned instead) |
| `dashboards` | `[]` | `[{"title": "Cameras", "path": "/dashboard-cams"}]` |
| `home_dashboard` | first dashboard, else `/` | Path or URL |
| `kiosk` | `full` | `full` hides the sidebar and header, `sidebar`, or `off` |
| `zoom` | 100 | Page zoom in percent (25–400); lower fits more on screen |
| `rotate_seconds` | 0 | Cycle `dashboards` when nobody is using the remote |
| `return_home_minutes` | 0 | Return home this long after remote-control browsing |
| `reopen_minutes` | 10 | Come back this long after someone left for another app, once nothing has played sound for as long (needs `overlay_permission` on Android 10+) |
| `reload_minutes` | 0 | Periodic full reload |
| `keep_screen_on` | true | Stop the screensaver while a dashboard is shown |
| `launch_on_boot` | true | Open after boot (needs `overlay_permission`) |
| `render_stream` | empty | A live HLS stream of the TV's dashboards, rendered by Home Assistant ([guide](render-on-home-assistant.md)). The app plays it full screen instead of loading pages, and keeps reporting the `url` it would show, which Home Assistant renders. Only for apps with the `render_stream` feature |
| `rev` | — | Opaque revision set by Home Assistant; any update without `rev` clears it |

`ha_token` belongs to the Home Assistant at `ha_url`: an update that moves `ha_url` to
another origin (scheme, host or port) clears the stored token unless it brings a new one.

When `ha_token` is set, the dashboard WebView answers the Home Assistant frontend's
external-auth requests with it, so no login page is shown. Without it, sign in on
the TV once and the frontend keeps its own session.
