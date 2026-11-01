# Test plan

What we check before a release and on each reference device. `tools/e2e/run.py` automates
the items marked **(auto)** against a real Home Assistant, on an Android TV emulator or, with
`--device`, on a real TV over the network. The rest need a person.

Record a device run as a GitHub issue titled `Test run: vX.Y.Z on <device>`. Start with
`tools/e2e/run.py --device <TV address> --ha-url … --ha-token-file …` and paste its
`summary.txt`, which names the TV, Android and Home Assistant versions. Then copy the
checklist below and tick what passed. Anything that fails gets its own issue.

## Matrix

| | Covered by |
| --- | --- |
| **Devices** | Google TV Streamer, Chromecast with Google TV (HD), a TV with Google TV built in, Fire TV on Fire OS (or beta testers), Android TV emulator (auto) |
| **Home Assistant** | Oldest supported (2025.1), current release, current beta (auto: any version) |
| **Install paths** | Downloader with `pleatview.hustlesman.com/apk`, adb, Google Play (from M2) |

## Checklist

### Install and update
- [ ] Downloader: entering `pleatview.hustlesman.com/apk` installs the app, and its banner appears in the TV's app row
- [ ] Installing a newer APK over the old one keeps the pairing and settings **(auto)**
- [ ] The "Open in HACS" button opens PleatView in HACS; installing it and restarting works

### Discovery and pairing
- [ ] Home Assistant discovers the TV within a minute of opening PleatView
- [ ] A wrong PIN is rejected; the right PIN finishes setup with no further questions **(auto)**
- [ ] The "Set up manually" button with the TV's address also works **(auto: manual address)**

### Dashboards
- [ ] The default dashboard appears signed in, without a login page, within 15 seconds **(auto)**
- [ ] Kiosk modes: full hides the sidebar and header, sidebar-only hides just the sidebar, off shows both (auto: full)
- [ ] Zoom 75 % fits more cards; 100 % looks like a browser
- [ ] Switching dashboards: source selection, `show_dashboard` with and without `duration`, channel up/down **(auto: show_dashboard)**
- [ ] Rotation cycles dashboards while nobody uses the remote, and pauses while someone does
- [ ] Back closes Home Assistant dialogs, then goes to the home dashboard, then opens the menu
- [ ] The menu shows address, PIN and version, and its dashboard buttons work
- [ ] The GitHub download's menu has *Support PleatView*, whose QR code opens the donation page; a store build has no such item **(auto: GitHub build)**

### Casting and media
- [ ] MP4 video plays full screen; Home Assistant shows title, position and duration; pause, seek and stop work **(auto: play and stop)**
- [ ] A camera with a live stream plays in a corner (`show_camera`), at the requested position and size, and closes after `duration`
- [ ] A camera without streaming shows as moving images **(auto)**
- [ ] An RTSP URL plays
- [ ] Music or internet radio plays in the background with the now-playing card
- [ ] A text-to-speech announcement plays over a video, which quietens and then recovers
- [ ] An image from the media browser shows full screen, an animated GIF moves; the browser's *Dashboards* folder switches dashboards
- [ ] A video cast with `extra: {loop: true}` starts again at the end
- [ ] A web page opens full screen and as picture-in-picture (`show_url`)
- [ ] A Webpage dashboard opens the site itself, not Home Assistant's frame, and keeps its sign-in (try one that needs a cookie, such as OpenSearch's playground) **(auto)**
- [ ] A dashboard behind a password prompt, and one behind a login form: the TV asks, the phone code signs in, and after a restart the saved sign-in is used without asking **(auto)**
- [ ] *Not now* on a sign-in card, then going home: Back opens the menu, and the card doesn't come back **(auto)**
- [ ] With rotation on, a dashboard shown with `show_dashboard` and a duration stays for that duration, then rotation goes on **(auto)**
- [ ] After Home, or another app left without sound, PleatView comes back after *Come back from other apps after* **(auto)**; not while a video plays in that app, and not while a settings panel only covers part of it
- [ ] A real site's login form (Grafana, Proxmox, UniFi) signs in from the phone code; a wrong password asks again; *Forget saved sign-ins* in the menu removes it

### Notifications
- [ ] `notify.send_message` shows a card that hides itself **(auto)**
- [ ] `pleatview.notify` with a camera snapshot shows the picture; an animated GIF moves on Android 10 and later and shows its first frame on Android 9, without a crash **(auto: GIF)**
- [ ] With the overlay permission, a notification appears over another app, such as YouTube

### Power and background
- [ ] `turn_off` blanks the screen and the entity turns `off`; `turn_on` brings the dashboard back **(auto)**
- [ ] After pressing Home, the entity turns `off`; with the overlay permission, `show_dashboard` brings PleatView back to the front
- [ ] After a reboot, PleatView opens by itself with "Open when the TV starts" on (Android 10 and later also need the overlay permission)
- [ ] The TV's screensaver does not start while a dashboard is showing

### Recovery
- [ ] Restarting Home Assistant: the dashboard and the entity recover within a minute **(auto)**
- [ ] Pulling the network or rebooting the router: a status banner, then recovery without touching the TV
- [ ] Clearing PleatView's data: Home Assistant asks for the new PIN, and the settings come back
- [ ] A WebView crash recovers by itself (debug builds; DevTools `Page.crash`)
- [ ] A page that freezes the WebView (an endless loop; after a Wi-Fi drop a real page did) gets a new one within about 90 seconds **(auto)**

### Sign-in and removal
- [ ] Reconfigure: read-only user (buttons on the TV can't change devices), own token, sign in on the TV
- [ ] Removing the integration returns the TV to its setup screen and deletes the user Home Assistant created

## Soak test (M1)

Run for 7 days on the Google TV Streamer and the Chromecast with Google TV (HD), at the same
time:

1. Show a dashboard with a live camera card, and turn on rotation every 60 seconds.
2. Once a day, record memory with
   `adb shell dumpsys meminfo com.hustlesman.pleatview | grep "TOTAL PSS"`
   and note any blank screen, freeze or crash.
3. Day 3: reboot the router. Day 5: restart Home Assistant.

**Pass:** no manual intervention for 7 days, recovery from both events, and memory that grows
by less than 20 % after the first day.

`tools/soak/run.py` does steps 1 and 2 on a paired TV and adds a busy household: lights
change every 2 minutes, a notification every 15 (text, camera snapshot, animated picture),
and every hour a video and a camera pop-up; every 2 hours a Webpage dashboard, every 3 a
password prompt (its sign-in card has to show, then go). It samples memory, the WebView
renderer, the dashboard's JavaScript heap, threads, open files and CPU every minute, takes a
screenshot every hour and keeps crashes, ANRs and renderer deaths from logcat. A page that
stops answering DevTools for 30 seconds goes in the issues, as a frozen renderer. It runs its
own adb server, so end-to-end runs on other TVs can happen meanwhile.

```bash
setsid nohup tools/soak/run.py --device <tv-ip> --entity media_player.<tv> \
    --out tools/soak/out/<name> --video "media-source://media_source/local/<clip>.mp4" \
    --gif "/media/local/<picture>.gif" --webpage /<webpage-dashboard> \
    --sign-in-page https://the-internet.herokuapp.com/basic_auth >/dev/null 2>&1 &
tools/soak/run.py --report tools/soak/out/<name>    # the summary so far
tools/soak/run.py --restore tools/soak/out/<name>   # stop, put the options back, remove its dashboard
```

Several TVs can soak at once against the same Home Assistant: start one runner per TV, each
with its own `--out`. They share the Soak dashboard, which goes when the last of them is
restored. A Google TV with Wireless debugging has a changing port: pass `--device IP:PORT`.

The report compares the first and the last hour of idle samples, shows the trend after the
first hour in MB per hour, and ends in PASS when memory grew less than 20 % without crashes or
restarts.
