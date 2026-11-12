<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="brand/pleatview-logo-on-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="brand/pleatview-logo-on-light.svg">
    <img src="brand/pleatview-icon.svg" alt="PleatView" height="80">
  </picture>
</h1>

A lightweight app for Google TV, Android TV and Fire TV, plus a Home Assistant
integration, that turns a TV into an always-on Home Assistant display you can cast to.
It also runs on Android phones and tablets, and in beta on Linux mini PCs and the
Raspberry Pi ([PleatView for Linux](linux/README.md)). Made by Hustlesman.

**Website:** [pleatview.hustlesman.com](https://pleatview.hustlesman.com) ·
**Status:** beta, see the [roadmap](ROADMAP.md) for validation and store releases.

![A Home Assistant dashboard on a TV with PleatView](site/img/screens/dashboard.webp)

- **Dashboards, continuously.** Any Home Assistant dashboard full screen, signed in
  automatically, without the sidebar and header, kept running for days. It recovers
  from network loss, Home Assistant restarts and WebView crashes on its own.
- **Casting from Home Assistant.** The TV is a `media_player`: play media from the media
  browser, camera streams, TTS announcements, internet radio, images and web pages.
  Video can play full screen or as picture-in-picture over the dashboard, and a camera or
  video in picture-in-picture also over other apps: the door camera in a corner while a
  film plays on, with its sound and the remote left to the film.
- **Notifications.** `notify.send_message` shows a card on the TV, optionally with a
  camera snapshot or an animated picture, even over other apps when allowed. Three levels:
  a *notice* is the card; a *warning* adds a short tone (chime, ding or beep) while whatever
  plays carries on; an *alert* pauses whatever plays, such as a film in Plex, and sounds a
  stronger one (alarm, siren or bell). Each TV's settings pick its tones.
- **Any web dashboard.** Webpage dashboards (Grafana, UniFi, OpenSearch…) open directly,
  zoomed to fit. When one asks for a password, the TV shows a QR code: sign in on your
  phone, and the TV keeps the sign-in, encrypted, on the TV only. Steps in the page's address
  press buttons once it loads, by their text or a CSS selector, to clear a cookie banner or
  close a pop-up, then drag a map or scroll ([page steps](docs/page-steps.md)).
- **Small.** The release APK is about 2.3 MB (the demo's video and photos are 0.9 MB of
  it), with no Google Play services and no cloud. Everything stays on your LAN.
- **A demo, no server needed.** *Try the demo* on the setup screen shows a sample home and
  a 90-second tour of what Home Assistant can show on top of it: a notification, a doorbell
  camera in a corner, another dashboard for a while and a video. It's built into the app.
- **In your language.** The TV app, its phone page and the integration's settings are in
  English, German, Dutch, French, Italian, Spanish, Polish, Swedish, Portuguese, Czech and
  Danish. If you're a native speaker and something reads oddly, please open an issue.

```
                 control: HTTP + Server-Sent Events (port 8765, paired with a PIN)
 Home Assistant ───────────────────────────────────────────────▶  PleatView on the TV
 + pleatview      ◀───────────────────────────────────────────────  ├─ dashboard WebView
   integration      dashboards & media: the TV loads them from HA   ├─ ExoPlayer (video/audio/cameras)
                                                                    └─ notifications, standby, menu
```

## Install

Three steps, and no computer needed for the TV.

### 1. TV app

<a href="https://www.amazon.com.au/dp/B0HR2T6VWM"><img src="site/img/badges/amazon-appstore.png" alt="Available at Amazon Appstore" height="44" align="middle"></a>
<a href="https://play.google.com/apps/testing/com.hustlesman.pleatview"><img src="site/img/badges/google-play.png" alt="Get it on Google Play" height="65" align="middle"></a>

- **Fire TV and Fire tablets:** from the Amazon Appstore. Search for *PleatView* on the TV,
  or [open it on Amazon](https://www.amazon.com.au/dp/B0HR2T6VWM).
- **Google TV, Android TV, phones and tablets:** Google Play has a beta until it goes public.
  Join [the testers' group](https://groups.google.com/g/pleatview-testers), then [the test](https://play.google.com/apps/testing/com.hustlesman.pleatview), and install PleatView from
  Google Play.
- **No store, or a TV the stores don't offer it on:** install the free **Downloader** app,
  open it, enter **`pleatview.hustlesman.com/apk`** and choose *Install*. Android asks once
  to allow installs from Downloader.

Google Play's version and the download update each other; Amazon signs its version itself,
so moving between it and the download means uninstalling first (Home Assistant then asks for
the new PIN, and keeps the settings).

On an Android phone or tablet, open the same address in its browser and install the
download; Android asks once to allow installs from the browser. For a wall-mounted tablet,
set its screen lock to None or Swipe: with a PIN, pattern or password, Home Assistant can't
wake it or show PleatView over the lock screen.

The download always serves the newest signed release from
[GitHub Releases](https://github.com/Hustlesman/pleatview/releases). Android 7.0 or newer; see
[Tested on](#tested-on) for the devices.

**On a Raspberry Pi or a Linux mini PC** (beta): PleatView for Linux runs on a Raspberry Pi
4 or 5 with the 64-bit Raspberry Pi OS, and on Ubuntu with an Intel or AMD processor. In a
terminal on it (or over SSH), these two lines download the package for the computer and
install it; run them again to update:

```bash
wget -O /tmp/pleatview.deb https://github.com/Hustlesman/pleatview/releases/latest/download/pleatview_$(dpkg --print-architecture).deb
sudo apt install /tmp/pleatview.deb
```

Then `pleatview-kiosk autostart` on a desktop (and restart), or `sudo pleatview-kiosk enable`
without one, opens it whenever the computer starts. Steps 2 and 3 are the same.
[PleatView for Linux](linux/README.md) has the details.

<details>
<summary>Install with adb instead, and allow overlays</summary>

On Google TV, enable *Settings → System → About → Android TV OS build* (press 7 times),
then *Developer options → USB/Wireless debugging*:

```bash
adb connect <tv-ip>:5555     # Google TV 12+: use "adb pair" from Wireless debugging first
adb install -r pleatview.apk
```

**Recommended:** allow PleatView to display over other apps. If your TV has the setting,
use **Allow display over other apps** in PleatView's menu (press Back on the home
dashboard). Most Google TV devices don't have it, so grant it with adb instead:

```bash
adb shell appops set com.hustlesman.pleatview SYSTEM_ALERT_WINDOW allow
```

This lets Home Assistant bring PleatView to the front (Android blocks background starts
otherwise), show notifications and picture-in-picture cameras over other apps, and open
PleatView at boot.

</details>

### 2. Home Assistant integration

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Hustlesman&repository=pleatview&category=integration)

The button opens PleatView on your own Home Assistant: in the built-in *Marketplace* on
Home Assistant 2026.11 and later, in HACS on older versions. You can also search for
PleatView there. Install it, then restart Home Assistant. Without either, copy
`custom_components/pleatview` into your `config/custom_components/` folder.
Requires Home Assistant 2025.1 or newer.

### 3. Pair

Open PleatView on the TV. Home Assistant discovers the TV under **Settings → Devices &
services**: select **Add**, and the TV asks whether to let Home Assistant control it. Press
**OK** on the remote. (Or enter the 6-digit PIN the TV shows.) A few seconds later the TV
shows your default dashboard.

If the TV isn't discovered (for example, mDNS is blocked between VLANs), use this button
and enter the address shown on the TV:

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=pleatview)

Pairing creates a dedicated, local-only Home Assistant user for the TV, so nobody types
a password with the remote. Removing the integration deletes that user again. If the
TV's app data is cleared, Home Assistant asks for the new PIN and restores everything.

<details>
<summary>Other sign-in options</summary>

Open the PleatView integration in Home Assistant and choose **Reconfigure** from its ⋮
menu. You can switch to a read-only user, a long-lived access token you created, or
signing in on the TV with the remote.

Without the integration, open `http://<tv-ip>:8765` on a phone, enter the PIN, and fill
in the Home Assistant URL and a long-lived access token. Dashboards work this way;
casting and notifications need the integration.

</details>

## Tested on

Every release runs `tools/e2e/run.py` on a TV against a real Home Assistant: pairing,
dashboards, kiosk mode, notifications, video, cameras, Webpage dashboards, sign-ins,
standby, a Home Assistant restart, a frozen page and an update in place. Last run on
5 November 2026; [the test plan](docs/test-plan.md) lists every check. The Linux app gets
the same kind of run, `tools/e2e/linux.py`, in Docker on a virtual screen (15 checks, all
passing on 12 November), and is checked by hand on a Raspberry Pi 5.

| Device | Result |
| --- | --- |
| Google TV, Android TV 16 (emulator) | 🟢 Compatible |
| Android 16 phone and tablet (Pixel 8 emulator at 360 dp, like most Samsung phones, and Pixel Tablet emulator, 5 Nov) | 🟢 Compatible, including touch, turning the screen and the lock screen |
| Android 11 phone (Galaxy S10 emulator, 360 dp, a 2020 WebView; 5 Nov) | 🟢 Compatible, checked by hand |
| Fire TV Stick 3rd gen, Fire OS 7.7 (Android 9) | 🟢 Compatible. Animated pictures show their first frame before Android 10. 42-hour soak test: no crashes |
| Chromecast with Google TV (HD), Android TV 14, 1.4 GB | 🟢 Compatible. 42-hour soak test: no crashes |
| Raspberry Pi 5 (4 GB), Raspberry Pi OS 13 with its desktop, on a monitor and a Sony TV (6 Nov) | 🟢 Compatible, checked by hand (beta): pairing, dashboards, notifications, video with sound over HDMI, cameras, Webpage dashboards, standby, restarts and starting at login. Not checked yet: HDMI-CEC with a TV, touch screens |
| Raspberry Pi 4, Raspberry Pi OS 64-bit | 🟡 Testing |
| Ubuntu 24.04, x86-64 | 🟡 Beta, checked in a container; a real mini PC is next |

| Home Assistant | Result |
| --- | --- |
| 2026.11 (current release) | 🟢 Compatible. Installs from the Marketplace without a GitHub account |
| 2026.10 | 🟢 Compatible |
| 2025.1 (oldest supported) | 🟢 Compatible |

## Using it from Home Assistant

You get `media_player.<tv>` and `notify.<tv>`. The media player is `off` while PleatView
is in the background or in standby, `idle` while showing a dashboard, and
`playing`/`paused` while showing media. Its **sources are your dashboards**.

Tablets and phones also get a battery sensor and a charging sensor (`sensor.<device>_battery`,
`binary_sensor.<device>_charging`; charging means plugged in, also when full), for example
to switch a wall tablet's smart plug on below 20 % and off above 80 %. TVs have no battery
and get neither.

**Webpage dashboards** (Grafana, OpenSearch, Kibana, UniFi…) work too: add one in Home
Assistant (*Settings → Dashboards → Add dashboard → Webpage*) and it becomes a source. The
TV opens the site itself rather than inside Home Assistant's frame, so sites that refuse
frames or need their own sign-in cookie still load, and the zoom applies to them as well.
A page of your own in `/config/www`, such as `/local/clock.html`, opens the same way
(from 1.0.12), without Home Assistant's frontend running underneath.

**Page steps** (from 1.0.19) do what a hand would after a page loads: add them to the
Webpage dashboard's address, or `pleatview.show_url`'s, after `#pleatview=`. A click finds
a button by its text or a CSS selector, so `#pleatview=click:Reject all;click:css=#promo .close`
refuses a cookie banner and closes a pop-up whose button is only an icon. Waits, clicks at a
place, drags and scrolls string along, for side bars and maps. See [page steps](docs/page-steps.md).

**A wall of YouTube live streams** is a page your Home Assistant serves (from 1.0.15),
`/api/pleatview/wall?v=ID,ID,…`, as a Webpage dashboard. The TV app shows it itself, each row
of streams in a process of its own. See [a wall of live streams](docs/ideas/live-streams.md).

**Render on Home Assistant.** For a TV too slow for a dashboard, such as a streaming stick
with a wall of nine live streams, Home Assistant's machine can run the dashboard and send
the TV one video of it, which every TV plays smoothly. It takes the PleatView Renderer
(a Home Assistant app, or a container) and one option per TV, from 1.0.14: see
[Render on Home Assistant](docs/render-on-home-assistant.md).

**Sites that ask for a sign-in.** When a dashboard shows a login form or a password
prompt, the TV says "Sign in to *dashboard name*" with a QR code. Scan it, sign in on your
phone (your password manager works there; the page shows the address the password goes
to), and the TV takes it from there. It saves the
sign-in once the site accepts it, encrypted with the TV's keystore and only ever offered to
that site, so it signs in again by itself after a restart or an expired session. Sign-ins
stay on the TV; Home Assistant only sees `sign_in_needed` on the media player, for
automations. Forget them in the TV's menu, on the phone page, or with
`pleatview.forget_sign_ins`. Where a site offers read-only links that need no sign-in, such
as Grafana's public dashboards, those are the sturdiest choice for a wall display.

| To… | Use |
| --- | --- |
| Switch dashboards | `media_player.select_source`, `pleatview.show_dashboard` (optional `duration` to return home), or the *Dashboards* folder in the media browser |
| Cast media | Media panel → choose the TV, or `media_player.play_media`. Animated GIFs play as images; `extra: {loop: true}` repeats a video |
| Show a camera | `pleatview.show_camera`: live HLS stream, MJPEG fallback, picture-in-picture by default |
| Speak | `tts.speak` with the TV as `media_player_entity_id`: plays over whatever is on, ducking it |
| Notify | `notify.send_message`, or `pleatview.notify` for image, camera snapshot, position, duration and `level` (`notice`, `warning`, `alert`) with an optional `tone` (animated GIFs move on Android 10 and later) |
| Open a web page | `pleatview.show_url` (full screen or picture-in-picture) |
| Blank the screen | `media_player.turn_off` / `turn_on` (lets the TV's own sleep timer take over) |
| Reset | `pleatview.home` (stop media, close overlays, go home), `pleatview.reload` |
| Forget saved sign-ins | `pleatview.forget_sign_ins` (optional `site`) |

`media_player.play_media` accepts `extra` keys: `mode` (`fullscreen`, `pip`,
`background`), `position`, `size` (picture-in-picture width in %), `duration`, `title`,
`artist`, `image_url`, `loop`, `volume`, `start_position`. Use content type `dashboard`
with a path, or `web` with a URL, to switch dashboards or open pages.

### Examples

Doorbell pop-up over whatever the dashboard shows:

```yaml
automation:
  - alias: Doorbell on the TV
    triggers:
      - trigger: state
        entity_id: binary_sensor.doorbell
        to: "on"
    actions:
      - action: pleatview.show_camera
        target:
          entity_id: media_player.living_room_tv
        data:
          camera: camera.front_door
          position: bottom-right
          size: 35
          duration: 60
      - action: pleatview.notify
        target:
          entity_id: media_player.living_room_tv
        data:
          title: Front door
          message: Someone is at the door
          camera: camera.front_door
          level: warning
```

A smoke alarm pauses the film and sounds the TV's alert tone:

```yaml
action: pleatview.notify
target:
  entity_id: media_player.living_room_tv
data:
  title: Smoke alarm
  message: Smoke detected in the kitchen
  level: alert
```

Show the energy dashboard for five minutes, then return home:

```yaml
action: pleatview.show_dashboard
target:
  entity_id: media_player.living_room_tv
data:
  path: /energy
  duration: 300
```

Loop a video in a corner:

```yaml
action: media_player.play_media
target:
  entity_id: media_player.living_room_tv
data:
  media_content_type: video
  media_content_id: media-source://media_source/local/aquarium.mp4
  extra:
    mode: pip
    position: top-right
    size: 30
    loop: true
```

PleatView also opens `pleatview://home`, `pleatview://reload` and
`pleatview://dashboard/<path>` links, so anything that can launch Android app links can
start it on a specific dashboard. A link opens a Home Assistant path or one of the TV's
dashboards, not any other website. That should include Home Assistant's Android TV
Remote integration (untested).

## Settings

Set these in the integration's **Configure** dialog. Home Assistant pushes them to
the TV.

| Option | Default | |
| --- | --- | --- |
| Home Assistant URL | internal URL | What the TV uses to load dashboards, media, camera streams and pictures |
| Home dashboard | first dashboard / HA default | Path such as `/lovelace/0` |
| Dashboards | all dashboards | `Title = /path` per line. Sources, rotation, the TV menu and channel up/down |
| Kiosk mode | hide sidebar and header | Or hide only the sidebar, or off |
| Zoom | 100 % | Lower values fit more on a TV (75 % gives a 1280 px wide layout on 1080p). Applies to Webpage dashboards too |
| Rotate dashboards | off | Cycle the list while nobody uses the remote |
| Return home after idle | off | After someone browses with the remote |
| Come back from other apps after | 10 minutes | When someone left PleatView for another app and nothing has played sound for that long. 0 never comes back. Needs the overlay permission on Android 10+ |
| Reload every | off | Full page reload, a safety net for very long uptimes |
| Keep the screen on | on | Prevents the screensaver / ambient mode while a dashboard shows |
| Open when the TV starts | on | Needs the overlay permission |

## Remote control

- **Back** closes, in order: the menu, media, overlays, Home Assistant dialogs and
  in-page navigation. It then returns to the home dashboard, and on the
  home dashboard it opens the **menu** (address, PIN, dashboards, reload, exit).
- **Channel up/down** (or page up/down) switches between configured dashboards.
- **Play/pause, stop, fast-forward, rewind** control media while it plays.
- Any key wakes the screen from standby.

## Staying up 24/7

PleatView keeps the screen awake while a dashboard is visible, and handles failures
itself:

- Home Assistant unreachable: shows a status banner and retries with back-off (also as
  soon as the network returns).
- WebView renderer crash: recreates the WebView and reloads; the app keeps running.
- Frozen page (no heartbeat for 2 minutes) or frontend disconnected for 5 minutes:
  reloads.
- Home Assistant restarts: the frontend reconnects, and so does the integration.
- Media ends: returns to the dashboard; live streams that fall behind rejoin the edge.

The TV's own power settings (sleep timer, HDMI-CEC) still apply. Set them to never sleep
for a permanent display.

## Security

- The control API listens on port 8765 on your LAN. Only clients that know the PIN shown
  on the TV can pair. After 5 wrong PINs, pairing locks for a minute and the PIN changes.
  The TV stores only hashes of the issued tokens.
- Traffic between Home Assistant and the TV is plain HTTP on your LAN, like most local
  integrations. The token handed to the TV belongs to a dedicated *local-only* user
  (unusable from outside your network). Make that user read-only if the TV shouldn't
  control devices.
- The API never returns the token. It is excluded from Android backups and device
  transfers, and is only exposed to pages from your Home Assistant address. Web pages
  opened with `show_url` never see it.
- Every release is signed with the same key (SHA-256
  `27:7B:CC:FA:72:E6:00:CE:D2:58:EF:9B:C4:DE:43:C6:FA:2F:27:53:8A:98:59:B2:C6:F2:EC:7C:34:3A:E3:73`,
  also in the site's `/.well-known/assetlinks.json`), so only a genuine PleatView can
  update yours. From 1.0.1, release files also carry a signed build attestation:
  `gh attestation verify pleatview.apk --repo Hustlesman/pleatview` (the Linux packages
  from 1.0.9 too).

## Development

```
android/                    Kotlin app (no Compose, no AppCompat; WebView + Media3 ExoPlayer)
linux/                      Electron app for Linux mini PCs and the Raspberry Pi (beta)
custom_components/pleatview Home Assistant integration (no extra requirements)
docs/protocol.md            The HTTP/SSE API between the two
tests/                      Integration tests with a fake TV that speaks the real protocol
tools/                      Docker-based build, test and end-to-end scripts
```

```bash
tools/build-apk.sh                    # app unit tests + debug APK
tools/build-apk.sh lintDebug          # any Gradle tasks
tools/test-integration.sh             # integration tests on the pinned Home Assistant
tools/e2e/run.py                      # end to end: Android TV emulator + real Home Assistant
tools/e2e/run.py --form-factor tablet  # ...on a Pixel Tablet emulator (or phone: a Pixel 8 at 360 dp)
tools/e2e/run.py --ha-version 2025.1.4   # ...against another Home Assistant version
tools/e2e/run.py --ha-url http://192.168.1.20:8123 --ha-token-file token.txt
                                      # ...against your own (admin token; the entry is removed afterwards)
tools/e2e/run.py --device 192.168.1.30 --ha-url http://192.168.1.20:8123 --ha-token-file token.txt
                                      # ...on a real TV instead of the emulator
```

The end-to-end test needs KVM and pairs the app with a throwaway Home Assistant that
listens only on 127.0.0.1. It saves screenshots, logs and a summary with the versions
tested to `tools/e2e/out/`. With `--device` it runs the same checks on a real TV over the
network: turn on *ADB debugging* (Fire TV) or *USB debugging* (Google TV) in the TV's
developer options, and allow this computer when the TV asks. The manual
checks for real devices are in [docs/test-plan.md](docs/test-plan.md).

The scripts only need Docker. In Android Studio, open `android/` (AGP 9.4, compileSdk 37,
JDK 17+). The debug build enables WebView debugging, so `chrome://inspect` works.

### Releases

```bash
tools/bump-version.sh 1.1.0   # app versionName/versionCode and integration version
# merge, then tag the merge commit:
git tag v1.1.0 && git push origin v1.1.0
```

The tag runs `.github/workflows/release.yml`. It signs the APK and app bundle with the
release key from the repository secrets and publishes `pleatview.apk`, the integration
zip, the Linux packages (`pleatview_amd64.deb`, `pleatview_arm64.deb`, built in a job
without the key) and checksums. Keep the key safe: Android only updates an installed app
signed with the same key. For a signed local build, set `PLEATVIEW_KEYSTORE` (a path anywhere on
disk), `PLEATVIEW_KEYSTORE_PASSWORD`, `PLEATVIEW_KEY_ALIAS` and `PLEATVIEW_KEY_PASSWORD`
before running `tools/build-apk.sh assembleRelease`. Without them, release builds fall
back to the local debug key. The app bundle for Google Play is
`tools/build-apk.sh bundleRelease`.

## Limitations

- Android TVs (Google TV, Android TV, Fire TV), Android phones and tablets, and in beta
  Linux. LG webOS and Samsung Tizen need a separate web client, which is on the roadmap.
- The Linux app's own screen is in English only (the phone page and the integration are
  translated).
- Without the overlay permission, Home Assistant can't bring PleatView to the front, and
  notifications only show while PleatView is open.
- `turn_on` can't reliably wake a TV that is fully asleep. Wake the panel with HDMI-CEC
  or the Android TV Remote integration first.
- Over another app, a picture-in-picture camera or video is muted, and can't be moved
  or closed with the remote: it closes after its `duration`, with `media_player.media_stop`,
  or when PleatView comes to the front. A TV that can't decode a second video beside the
  other app's shows the camera's picture stream instead, at a few frames a second.
- Kiosk mode styles Home Assistant's frontend from the outside, so frontend releases can
  break it. It covers the 2026 layout and the older Material drawer.
- Self-signed HTTPS certificates are rejected. Use the internal `http://` URL or a
  trusted certificate.

## Support

Help, common questions and contact details are on the
[support page](https://pleatview.hustlesman.com/support.html). Report security problems
privately as described in [SECURITY.md](SECURITY.md).

PleatView is free and open source, and every feature stays free for everyone. If it's
useful to you, you can [chip in](https://donate.stripe.com/cNifZg4Wudug46O4X38Ra00) towards test devices and app-store fees
(payments go through Stripe and appear as Hustlesman). Bug reports and ideas help just as
much. The app has the same link as a QR code under *Support PleatView* in its menu.

## License

[MIT](LICENSE). PleatView is not affiliated with Home Assistant, Nabu Casa, Google,
Amazon, Raspberry Pi, Canonical, LG or Samsung; their names are used only to describe
compatibility. Google Play and the Google Play logo are trademarks of Google LLC. Amazon,
Amazon Appstore and their logos are trademarks of Amazon.com, Inc. or its affiliates. The demo's video clip is *Caminandes: Gran Dillama*, © 2013 Blender
Foundation, [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/); its photos are CC0
([credits](android/app/src/main/assets/demo/CREDITS.txt)).
