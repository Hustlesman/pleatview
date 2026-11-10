# Render on Home Assistant

Some dashboards are too much for a TV: a wall of nine YouTube live streams, or a heavy
dashboard on a cheap streaming stick. The machine that runs Home Assistant is often far
stronger. With the **PleatView Renderer**, that machine runs the TV's dashboards in a
browser of its own and sends the TV one H.264 video of them, which every TV plays in
hardware without effort.

What we measured on a Chromecast with Google TV (HD), with nine YouTube live streams:

| | On the TV itself | Rendered on Home Assistant |
| --- | --- | --- |
| Playback | Buffering most of the time | Every stream moving, about 3 seconds behind live |
| The TV's processor | Busy with nine players | PleatView at about 40 % of one core |
| Network | Nine YouTube streams | One stream of about 3 to 4 Mbit/s |

The Home Assistant machine, an Intel Core i5-7500 with Quick Sync, used about 2.5 of its
4 cores and 1.2 GB of memory for that wall. A dashboard without video needs far less.

## What you need

- **PleatView 1.0.14 or later on the TV**: Android TV, Google TV, Fire TV, or a phone or
  tablet. The Linux app doesn't play rendered dashboards yet.
- **A Home Assistant machine with power to spare.** A mini PC or a NAS with an Intel or
  AMD processor is ideal, and its graphics encode the video. A Raspberry Pi 4 or 5 manages
  ordinary dashboards, not walls of video.
- **The TV's sign-in managed by Home Assistant**, which is the default when you add a TV.
  The renderer signs in as the TV's own Home Assistant user.

## 1. Install the renderer

### Home Assistant OS: as an app

Home Assistant 2026 calls add-ons *apps*. On older versions, read *Add-ons* and *Add-on
store* below.

In *Settings → Apps*, choose *Install app*. In the App store, open the menu at the top
right and choose **Repositories**.

![The App store's menu, with Repositories](img/render-1-repositories.webp)

*Add*, and enter `https://github.com/Hustlesman/pleatview`.

![The Add repository dialog with PleatView's address](img/render-2-add-repository.webp)

Back in the App store, search for *PleatView* and open **PleatView Renderer**.

![The App store, with PleatView Renderer under PleatView](img/render-3-store.webp)

*Install*. Home Assistant builds the renderer on your machine, from the source in this
repository, which takes a few minutes. Then *Start*.

![PleatView Renderer installed, with its Start button](img/render-4-start.webp)

Its *Log* shows the address to use in PleatView. Added as above, the repository gives every
Home Assistant the same one: `http://ce7dee09-pleatview-renderer:8780`.

![The renderer's log, with the address to use](img/render-5-log.webp)

### Home Assistant Container: next to it

Build the renderer from source and run it on the same machine:

```sh
docker build -t pleatview-renderer https://github.com/Hustlesman/pleatview.git#v1.0.16:renderer
docker run -d --name pleatview-renderer --restart unless-stopped --network host \
  -e HOST=127.0.0.1 --device /dev/dri pleatview-renderer
```

Its address is then `http://127.0.0.1:8780`, as its log says. Leave out
`--device /dev/dri` on a machine without Intel or AMD graphics; the processor encodes
instead, using about one core per TV.

![The renderer's log in a terminal, with the address to use](img/render-7-docker.webp)

## 2. Turn it on for a TV

*Settings → Devices & services → PleatView*, and the gear next to the TV. At the bottom,
put the renderer's address in **Render on Home Assistant**, and *Submit*.

![PleatView's Display settings with the renderer's address in Render on Home Assistant](img/render-6-option.webp)

Only that TV changes; every other TV still runs its own dashboards. Empty the field to go
back.

## What changes on the TV

![Four films playing side by side, rendered on Home Assistant](ideas/img/live-streams-7-tv.webp)

- Dashboards, rotation, the channel keys on the remote, `show_dashboard` and Home work as
  before: the renderer follows whatever the TV would show, at the TV's size, zoom and kiosk
  mode.
- The picture is about three seconds behind. A light switched on shows on the dashboard
  three seconds later.
- The TV only shows the picture. Browsing inside a dashboard with the remote, and the sign-in
  questions of webpage dashboards, don't reach the page.
- Notifications, cameras in a corner, media and announcements still play on the TV itself.
- When the TV is off or in another app, the renderer stops its screen after 90 seconds and
  starts it again when the TV is back.

## Privacy and security

- Everything stays on your network. The renderer sends the stream to Home Assistant, which
  passes it to the TV at an address that includes a secret only that TV gets.
- The renderer's own interface has no sign-in, so it must not be reachable from the
  network: the app is only on Home Assistant's internal network, and the `docker run`
  above listens on 127.0.0.1 only.
- Chromium, which shows the pages, runs as an unprivileged user.
- The renderer is free software from Debian's main archive (Chromium, Xvfb, ffmpeg, Python's
  aiohttp, tini, the Mesa and Intel VA-API drivers and two font packages) on a pinned Debian
  12 base image. `http://<renderer>/api/packages` lists every installed package and version,
  for audits and vulnerability scans.

## Walls of live streams

Rendered on Home Assistant, a [wall of live streams](ideas/live-streams.md) plays on any
TV, however slow: the TV decodes one video, not nine.
