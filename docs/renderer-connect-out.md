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

## What the controller gets

- **Rendering:** pages shown for the site's screens, at the size and turn it asks for, with
  notifications and pictures drawn over them, and the streams' segments for its previews.
- **Who the renderer is:** its name, version, how busy it is, what it encodes with, how many
  screens it renders, and where the site's screens reach its streams.

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
| `show` | `key` (1–48 of `A-Z a-z 0-9 _ -`), `url` (http or https), `width`, `height`, `scale`, `kiosk`, `rotation`: as `PUT /api/sessions/<key>`, without Home Assistant's address and token | `{"stream": "/stream/<secret>/index.m3u8"}`, on `streams` |
| `overlay` | `key` and what `POST /api/sessions/<key>/overlay` takes | `{}` |
| `stop` | `key` | `{}` |
| `segment` | `key`, `name` (`index.m3u8` or `s<n>.ts`) | `{"type": "…", "data": "<base64>"}`: the playlist (with its segments' names) or a segment, at most 4 MB |

Keys from the controller are its own: they never name the sessions Home Assistant has.
Sessions from the controller count towards `max_screens` like the others.

Every call that changes what the renderer shows (`show`, `overlay`, `stop`) is written to its
log, with what it asked for and whether it was done, so the site sees what its controller did;
so is every call it doesn't know. Requests are JSON objects of at most 64 kB, at most
16 at once and 30 a second; more closes the connection.

## Next

- **Home Assistant at the site,** shared when the site turns it on: the entities it chooses
  (cameras by default), their pictures and live streams, with a token the site gives the
  renderer that never leaves it.
- **Screens at the site** paired through the renderer and moved to protocol v2, for screens
  set up with Home Assistant that can't be given a controller's address otherwise.
