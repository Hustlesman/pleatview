# PleatView Renderer: connecting out to a controller

Status: being built, November 2026. Part of [S1](../ROADMAP.md#s1--secure-remote-management).

A renderer at a site connects out to a controller somewhere else: at a head office, at a
managed service provider, or in a data centre. It renders the pages of the site's PleatView
Light screens there, at the site, and the screens play its streams on the site's own network.
Nothing is opened at the site: the renderer keeps one TLS connection out, as screens do in
[protocol v2](protocol-v2.md).

The site decides what the controller gets. A controller that is broken into, or an operator
who goes too far, reaches only what the site has given it. A controller can't turn the
renderer into a way into the site's network.

## What the site decides

Everything below is set on the renderer itself, in its app's options or its environment, and
can't be changed from the controller:

| Option | Variable | Meaning |
| --- | --- | --- |
| `controller` | `CONTROLLER` | The controller's address, such as `controller.example.com` or `wss://controller.example.com/api/pleatview/renderer`. Empty: the renderer doesn't connect out |
| `controller_fingerprint` | `CONTROLLER_FINGERPRINT` | The SHA-256 of the controller's certificate (hex, with or without colons), for a controller whose certificate the renderer's system doesn't trust |
| `site_pages` | `SITE_PAGES` | Places on the site's own network that the controller's pages may load, comma-separated: `host:port` (such as `homeassistant.local:8123`) or a network (`192.168.1.40/32`). Empty by default: pages from the controller reach only the public internet |
| `stream_url` | `STREAM_URL` | This machine's address on the site's network for the streams port, such as `http://192.168.1.10:8781`. In the Home Assistant OS app, which can't see it, the site's screens get their streams through the controller until it's set; elsewhere it's the machine's own address |
| none | `STREAM_PORT` | The port the site's screens fetch their streams from (default `8781`; the app maps it as `8781/tcp`) |
| `share_home_assistant` | `SHARE_HOME_ASSISTANT` | Share the site's Home Assistant with the controller: the entities in `shared_entities` ([below](#home-assistant-at-the-site)). Off by default |
| `shared_entities` | `SHARED_ENTITIES` | The entities shared, by id, `*` standing for any letters, comma-separated: `camera.*` (the default), `camera.front_door`, `todo.shopping` |
| `find_screens` | `FIND_SCREENS` | Find the site's PleatView screens for the controller, which adds them as on its own network: with OK pressed on the screen, or its PIN ([below](#screens-at-the-site)). On by default |
| `share_screens` | `SHARE_SCREENS` | Let the controller list the site's PleatView screens in Home Assistant and move the ones it chooses to itself, with nobody at them ([below](#screens-at-the-site)). Off by default |
| none | `HA_URL`, `HA_TOKEN` | Home Assistant's address and a long-lived token, outside the Home Assistant OS app (which uses the Supervisor's own, through `homeassistant_api`) |

## What the controller gets

- **Rendering:** pages shown for the site's screens, at the size and turn it asks for, with
  notifications and pictures drawn over them, and the streams' segments for its previews.
- **Who the renderer is:** its name, version, how busy it is, what it encodes with, how many
  screens it renders, and where the site's screens reach its streams.
- **The site's Home Assistant,** only when the site shares it: what it lists, and its screens
  only if it lets them go ([below](#home-assistant-at-the-site)).
- **The site's screens,** unless the site turns `find_screens` off: their ids and names, never
  their addresses, to be added with someone at each ([below](#screens-at-the-site)).

Nothing else. The connection carries a fixed list of calls (below), each checked; nothing is
passed on to the site's network, and nothing lets the controller change the renderer's options.

## Pages from the controller stay off the site's network

Every page the controller has the renderer show loads through a filter in the renderer:

- **The public internet** is open, on any port.
- **Everything else is closed:** this machine (the renderer's own API, the browser's debugging
  port), private networks (10/8, 172.16/12, 192.168/16, fc00::/7), link-local addresses and cloud
  metadata (169.254/16, fe80::/10), shared address space (100.64/10), multicast and reserved ranges,
  IPv4 written inside IPv6, and Home Assistant's Supervisor.
- **Places the site lists** in `site_pages`, and only those, are open on the site's network.

The filter is a SOCKS5 proxy on 127.0.0.1. The browser sends it every connection, WebSockets
included, as a name and a port; the filter looks the name up itself and connects to the
address it checked, so a name that changes its answer between the check and the connection
(DNS rebinding) gets nowhere. Each connection is a tunnel to one host and port, so a request
can't ride on a connection made for another. The browser makes no WebRTC connections around
it, and none of its own calls home. A place in `site_pages` opens only as it's written there:
`127.0.0.1:9999` doesn't open `localhost:9999`. Pages from the site's own controller over
protocol v1 (Home Assistant) load as before.

## Streams on the site's network

The site's screens fetch a stream from the renderer itself, at an address with a secret in it
(`http://<renderer>:8781/stream/<32 hex digits>/index.m3u8`), made afresh for each screen's
session. That listener serves those streams and nothing else, and listens only while the
renderer connects out. Screens elsewhere (and the site's screens, while `stream_url` isn't
known) get their streams through the controller, which fetches the segments over the
connection.

## Keys and enrolment

As in [protocol v2](protocol-v2.md#keys), with a label of its own, so neither kind of signature
can stand in for the other:

- The renderer makes one ECDSA P-256 key pair, kept in a file only it can read
  (`renderer-key.pem` in its data folder). The private key never leaves it.
- It connects to `wss://host[:port]/api/pleatview/renderer`, WebSocket subprotocol
  `pleatview.renderer.v1`, over TLS 1.2 or later. It checks the controller's certificate
  against the system's authorities, or against `controller_fingerprint` when given. It never
  connects without TLS.
- It signs the challenge as in v2, over this text:

  ```text
  pleatview-renderer-v1
  wss://controller.example/api/pleatview/renderer
  <nonce>
  ```

- **Enrolling:** the first time, it sends `enrol` with its public key. The controller answers
  `code`, and the renderer writes the code in its log: `Type the code K7RM-W2XD into the
  controller`. Someone allowed to add renderers types it into the controller, which answers
  `welcome`. From then on the renderer signs in with `auth`.
- **Another controller:** changing `controller` enrols again from the start; the old one's
  enrolment is forgotten.

Messages are v2's: `challenge`, `enrol`, `auth`, `code`, `welcome`, `request` and
`response`, `ping` and `pong` every 20 seconds, and `retry`. So are the closing codes, and
the waits between connections. A renderer that's removed in the controller (4401, 4403)
forgets it, writes so in its log, and stays away until its options change or it restarts.

## Calls

| Method | `params` | Answer |
| --- | --- | --- |
| `info` | none | What `GET /api/info` gives, with `streams`: the addresses the site's screens reach its streams at |
| `show` | `key` (1–48 of `A-Z a-z 0-9 _ -`), `url` (http or https), `width`, `height`, `scale`, `kiosk`, `rotation`, `tiles`: as `PUT /api/sessions/<key>`, without Home Assistant's address and token | `{"stream": "/stream/<secret>/index.m3u8"}`, on `streams`; for a wall (`tiles`), `{"streams": ["/stream/<secret>/t0.m3u8", …]}`, a stream for each tile in the order given |
| `overlay` | `key` and what `POST /api/sessions/<key>/overlay` takes | `{}` |
| `stop` | `key` | `{}` |
| `segment` | `key`, `name` (`index.m3u8` or `s<n>.ts`; a wall's: `t<tile>.m3u8` or `t<tile>-<n>.ts`) | `{"type": "…", "data": "<base64>"}`: the playlist (with its segments' names) or a segment, at most 4 MB; a camera's too (`ha_stream`'s key) |
| `ha_config` | none | Home Assistant's `name` and `version`, `shared` (the patterns) and `screens` (whether they may move) |
| `ha_entities` | none | `{"entities": [{"entity_id", "domain", "name", "state"}]}`: the shared ones, without their other attributes |
| `ha_picture` | `entity_id` (a shared camera) | `{"type": "image/jpeg", "data": "<base64>"}`, at most 3 MB |
| `ha_stream` | `key`, `entity_id` (a shared camera) | `{"stream": "/stream/<secret>/index.m3u8"}`: its live stream, made here, on `streams` |
| `ha_todo` | `entity_id` (a shared to-do list) | `{"items": [{"summary", "status", "due"}]}` |
| `ha_calendar` | `entity_id` (a shared calendar), `start`, `end` (ISO 8601) | `{"events": [{"summary", "start", "end", "location", "description", "uid", "recurrence_id"}]}`, repeats worked out by Home Assistant |
| `ha_screens` | none | `{"screens": [{"id", "name", "manufacturer", "model", "app_version", "online", "connects_out"}]}`, with `share_screens` |
| `ha_enrol` | `screen` (an `id` from `ha_screens`), `token` (the controller's one-time token for it) | `{}`, once the screen took it, with `share_screens` |
| `site_screens` | none | `{"screens": [{"id", "name", "in_home_assistant", "connects_out"}]}`: the screens found at the site (`connects_out` only for the ones Home Assistant has), with `find_screens` |
| `site_add` | `screen` (an `id` from `site_screens`), `how` (`ok` or `pin`), `pin` (with `pin`), `token` (the controller's one-time token for it) | With `pin`: `{"added": true}`, once the screen took it. With `ok`: `{"ticket": "…"}`, while the screen asks the person in front of it |
| `site_asked` | `ticket` | `{"state": "pending"}`, then `added`, `denied`, `expired`, or `failed` with an `error` |

Keys from the controller are its own: they never name the sessions Home Assistant has.
Sessions from the controller count towards `max_screens` like the others; a wall counts as many
as its page has 1080p pictures. A wall shows no notices (`overlay` is refused): its screens are
one picture.

## One page across several screens: walls

A session with `tiles` is a wall: the renderer draws its page once, `width` × `height` (up to
7680 a side and 2 × 3840 × 2160 pixels in all; `rotation` stays 0), and cuts it into a stream
for each of its screens. `tiles` lists 2 to 16 of them, each `{"x", "y", "width", "height"}` in
the page's pixels (64 to 3840 a side, inside the page, rounded down to even numbers),
`rotation` (how that screen is mounted: its piece is turned back, as for a screen of its own)
and `sound` (true for one of them at most: the page's sound goes with that piece).

One ffmpeg cuts the pieces from the same frames and encodes each with a keyframe every
segment, so segment *n* of every piece starts on the same frame. Each piece's playlist is
served with:

- `#EXT-X-PROGRAM-DATE-TIME` on every segment: when its first sample was drawn, by one clock for
  the whole wall (each piece's timestamps are lined up with the first piece's at a segment both
  have, by their first pictures);
- `#EXT-X-SERVER-CONTROL:HOLD-BACK=4.000`: how far behind that time screens play;
- `#EXT-X-PLEATVIEW-CLOCK:<seconds>`: the time at the piece's timestamp zero, for players that
  see timestamps but not playlists' times.

A player that keeps the delay by its own clock (kept right by NTP) shows the same frame as the
screens beside it. The apps do: Android's player (ExoPlayer) by the playlist's times and delay,
adjusting its speed; the Linux app's and PleatView Light's by a few hundredths faster or
slower, a jump forward when late, a pause when early; both by the frame's own timestamp and
the piece's clock (in the piece with the sound, a segment's time is its first sound's, which
can be an audio frame off its first picture's). Checked with the Linux app's player twice in
Chromium, one piece with the sound: the same frame on both, 8 times out of 8; and Light on a
Raspberry Pi Zero 2 W: within 20 ms of the delay.

Every call that changes what the renderer shows (`show`, `overlay`, `stop`) is written to its
log, with what it asked for and whether it was done, so the site sees what its controller did;
so is every call it doesn't know. Requests are JSON objects of at most 64 kB, at most
16 at once and 30 a second; more closes the connection.

## Home Assistant at the site

A site that runs Home Assistant can share some of it with the controller, through the renderer,
when it turns `share_home_assistant` on. Home Assistant needn't be reachable from the controller,
or from anywhere outside the site: the renderer asks it, on the site's network, for what the
controller asks, and only for that:

- **The entities in `shared_entities`,** cameras (`camera.*`) by default: their names and
  states, without their other attributes (a camera's carry a token for its picture).
- **Cameras:** a picture now, and the live stream. The renderer fetches the stream from Home
  Assistant (its HLS stream, or its picture stream for a camera without one) and makes it a
  stream of its own, served like a screen's: the site's screens fetch it on the streams port,
  the controller through the connection. A camera nobody fetches for 90 seconds stops; four at
  most stream at once.
- **To-do lists and calendars** that are shared: their items and events.

The token is the site's. In the Home Assistant OS app the renderer uses the Supervisor's own
(`homeassistant_api`); elsewhere, the address and long-lived token in `HA_URL` and `HA_TOKEN`.
It stays in the renderer's own process: the controller never gets it or an address signed with
it, it isn't on any command line, and the browsers and ffmpegs the renderer starts don't have it
in their environment. ffmpeg reads the camera's stream from the renderer, on its standard input,
without root. Anything not shared is refused, whatever the controller asks.

## Screens at the site

**Found for the controller** (`find_screens`, on by default). The controller lists the site's
PleatView screens and adds them as it would on its own network: someone at the screen presses OK
on it, or reads its PIN to whoever types it into the controller. It needs the PleatView
integration (1.1.4 or later) in the site's Home Assistant, with or without screens added to it:

1. The app can't see the site's network itself (it's on Home Assistant's own), so `site_screens`
   asks Home Assistant: the screens its PleatView integration found and hasn't added (its
   discoveries, each with the address it was found at, as `configuration_url`), and the ones it
   has (`pleatview.list_screens`). The controller gets their ids and names, not their addresses.
2. For a screen chosen, the controller makes a one-time token, as for any screen it pairs with
   ([protocol v2](protocol-v2.md#enrolment)), and sends it with `site_add` and how it's allowed.
3. The renderer pairs with the screen at the address it found it at: with the PIN, or by asking
   the person in front of it, who sees the controller's host name as who asks. Then it hands the
   screen the controller's address for screens with the token, and unpairs. The screen connects
   out to the controller and enrols with the token.

The renderer pairs only with screens it found, at the addresses it found them at: the call names
a screen's id, never an address. A screen an administrator chose to ignore in Home Assistant
isn't listed. A renderer elsewhere than in the app finds screens through the Home Assistant at
`HA_URL`.

**Moved by the controller** (`share_screens`, off by default). Screens already set up with the
site's Home Assistant can't be given the controller's address by anyone but the people at the
site. With `share_screens` on, the controller may move them itself, with nobody at them,
through Home Assistant (the PleatView integration from 1.1.4, and screens with PleatView 1.0.19
or later):

1. `ha_screens` lists Home Assistant's PleatView screens (its `pleatview.list_screens` service).
2. For each screen chosen, the controller makes a one-time token, as for any screen it pairs
   with ([protocol v2](protocol-v2.md#enrolment)), and sends it with `ha_enrol`.
3. The renderer has Home Assistant pass it on (`pleatview.enrol_with`): the screen connects out
   to the controller and enrols with the token. Home Assistant keeps its own pairing, and shows a
   notification for each screen it moved.

Either way, a screen goes only to this renderer's own controller: the renderer gives it, or Home
Assistant, the controller's address for screens (`wss://<controller>/api/pleatview/v2`, and its
pinned certificate when there is one), never an address the call names. Home Assistant's
services and its discoveries are for its administrators only.
