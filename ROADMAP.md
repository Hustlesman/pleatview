# PleatView roadmap

**Goal: PleatView on every store by Friday 2 April 2027.** That means:

- the TV app is installable from Google Play (Google TV / Android TV) and the Amazon
  Appstore (Fire TV), and LG and Samsung TVs have their own store apps,
- the same app installs from Google Play on Android phones, tablets and Chromebooks, and
  from the Amazon Appstore on Fire tablets; iPhones and iPads use the web player,
- it has been validated on real hardware, including a 30-day soak,
- the Home Assistant integration is in the HACS default list (done 2 Nov),
- install guides live on [pleatview.hustlesman.com](https://pleatview.hustlesman.com).

Dates assume about 20 hours of development a week, and include store review times. We
re-check them at the first weekly review.

**Principle: as little interaction as possible.** Every step a person has to take is a
step to remove. Setup should be: type a short link into the TV's Downloader app (later,
one click in the store), press one button to add the integration, confirm the discovered
TV and enter its PIN. Nobody should need adb, a computer, or to type a password with
the remote. The same goes for running the project: checks, the website and releases run
on their own.

## Status

Last updated 5 November 2026. **Current milestone: M1** (M0 finished early).

| Milestone | Target | Needs first | Status |
| --- | --- | --- | --- |
| [M0](#m0--foundation) Foundation | Fri 6 Nov 2026 | — | Done 28 Oct |
| [M1](#m1--validated-on-real-android-tv-hardware) Validated on real Android TV hardware | Fri 20 Nov 2026 | M0, devices | In progress |
| [M2](#m2--public-beta) Public beta, Google Play closed test, phones and tablets | Fri 4 Dec 2026 | M1, 12 beta testers | Not started |
| [M3](#m3--google-play-and-amazon-appstore) Google Play and Amazon Appstore | Fri 15 Jan 2027 | M2, store reviews | Not started |
| [M4](#m4--web-player) Web player for any TV browser | Fri 29 Jan 2027 | M3 submitted | Not started |
| [M5](#m5--lg-and-samsung-apps) LG and Samsung apps submitted | Fri 5 Mar 2027 | M4, test TVs | Not started |
| [M6](#m6--on-every-store) On every store | Fri 2 Apr 2027 | M5 approvals, 30-day soak | Not started |
| [M7](#m7--iphone-and-ipad-app) iPhone and iPad app | Fri 14 May 2027 | M4, Apple developer account | Not started |

Progress is tracked in [GitHub milestones](https://github.com/Hustlesman/pleatview/milestones).
Bugs and features for the current milestone are [issues](https://github.com/Hustlesman/pleatview/issues);
each later milestone gets its issues when it starts.

**Want to help?** Tell us about a device that works or doesn't, report a bug, or join the
Google TV beta ([#9](https://github.com/Hustlesman/pleatview/issues/9)).

Done so far: v1.0.7 is published as a signed release with build attestations (versions are
1.x from 29 Oct; each milestone ends with the next minor release). One command
(`tools/e2e/run.py`) tests the Android app and the Home Assistant integration together in
20 checks, on an Android TV 16 emulator or a real TV: pairing, automatic sign-in, kiosk mode,
casting, cameras, notifications, sign-ins, standby, updates, and recovery from a Home Assistant
restart and from a frozen page. Phone and tablet emulators add touch and the lock screen, for 22. A Fire TV Stick and a Chromecast HD pass the automated checks,
and both ran a 42-hour soak test without a crash. A security review on 2 Nov fixed what it
found, and no dependency has a known vulnerability. 56 app tests and 31 integration tests pass.

## How we stay on track

1. **One milestone at a time.** Store reviews run in the background.
   When a milestone is only waiting on a reviewer, the next one may start.
2. **Done means every exit box is ticked.** Not "mostly done".
3. **New ideas go to the [parking lot](#parking-lot)** and are only looked at between
   milestones.
4. **P1 bugs are fixed immediately, in any milestone.** P1 means a crash, failed pairing,
   a dashboard that won't load, or a security issue. Everything else waits for the next
   milestone's scope.
5. **Weekly check-in, Mondays, 15 minutes.** Update the status table and the GitHub
   milestones. If a milestone is
   more than a week late, move scope to the parking lot rather than moving the date.
6. **Every milestone ends with a tagged release** and a changelog.

## M0 — Foundation

*Fri 6 Nov 2026.* The project can be built, tested, released and found.

- [x] Code on GitHub ([Hustlesman/pleatview](https://github.com/Hustlesman/pleatview)), MIT license
- [x] App ID `com.hustlesman.pleatview`; integration manifest has documentation, issue tracker and code owner
- [x] CI workflow: app tests, lint and build; integration tests; hassfest; HACS validation
- [x] Website and privacy policy in `site/`, deployed by GitHub Pages
- [x] Release workflow: a version tag builds the signed APK and app bundle and publishes a GitHub release
- [x] Repository description and topics set (the HACS check needs them)
- [x] Install without a computer: Downloader plus `pleatview.hustlesman.com/apk`, which always serves the newest release
- [x] "My Home Assistant" buttons that open PleatView in HACS and start its setup
- [x] Setup ends at the PIN: a dedicated user by default, other sign-in methods under Reconfigure
- [x] Release key in GitHub secrets; `tools/bump-version.sh` keeps app and integration versions in step
- [x] `pleatview.hustlesman.com` live over HTTPS
- [x] v0.1.1 tagged and published ([release](https://github.com/Hustlesman/pleatview/releases/tag/v0.1.1))
- [x] Work tracked in GitHub milestones and issues

**Exit:** CI green on `main`, the site is live, and a signed v0.1.1 APK can be downloaded
from GitHub Releases.

## M1 — Validated on real Android TV hardware

*Mon 9 → Fri 20 Nov 2026.* Prove it works on the devices people own, for days at a time.

- [x] `docs/test-plan.md`: the checklist run per device and per release. It covers
      discovery, pairing, automatic sign-in, kiosk, zoom, switching and rotating
      dashboards, video (MP4, HLS, RTSP), cameras (HLS, WebRTC cards, Frigate),
      text-to-speech announcements, notifications in and over other apps, standby and
      wake, launch at boot, re-pairing after a data wipe, network loss and Home
      Assistant restarts.
- [x] Scripted end-to-end test in `tools/e2e` (emulator and Home Assistant in Docker),
      runnable with one command
- [ ] Test plan passed on: Google TV Streamer, Chromecast with Google TV (HD, the
      low-memory worst case), a TV with Google TV built in, Fire TV (Fire OS 7 or 8).
      Fire TV Stick (3rd Gen, Fire OS 7.7, 1 GB): every automated check passes
      (`tools/e2e/run.py --device`, 29 Oct), and it opens by itself after a reboot.
      Chromecast with Google TV (HD, Android TV 14, 1.4 GB): every automated check passes
      (30 Oct). By hand on 1 Nov with v1.0.3: an update in place keeps the pairing and
      settings; a notification shows over YouTube; `show_dashboard` brings PleatView back
      in front of another app; after Home the entity turns `off`; the menu's *Display over
      other apps* button leads to the permission. On the Fire TV, clearing PleatView's data
      makes Home Assistant ask for the new PIN and the settings come back, and removing
      the integration returns the TV to its setup screen and deletes its Home Assistant
      user. On 3 Nov the Chromecast moved from the Google Play build to the GitHub one,
      installed with Downloader: Home Assistant asked for the new PIN and kept the TV's
      entity and settings. Still to do: the "My Home Assistant" buttons, a sign-in on a
      real site
- [x] Home Assistant versions: oldest supported (2025.1), current stable, beta. On 5 Nov
      all 20 checks passed on 2025.1.4, 2026.10.4 and the 2026.11.0 release, after the
      2026.11 beta (2026.11.0b3) on 3 Nov. The matrix runs again before each release
- [x] Soak on the Chromecast HD and the Fire TV Stick with `tools/soak/run.py`: a busy
      household, a live camera and rotation on. 42 hours on both (30 Oct to 1 Nov), stopped
      before the planned 7 days; the 30-day soak in M6 is the long run. No crashes, and
      PleatView's memory stayed flat (Chromecast +0.2 %, Fire TV −3 %). Both recovered from a
      Home Assistant restart. Fire OS's "Are you still watching?" sends the Fire TV to its
      home screen every 4 hours; twice Fire OS then closed PleatView to free memory, and it
      came back by itself both times. A 2-minute Wi-Fi drop on the Fire TV left a page
      frozen, and PleatView didn't recover until its WebView was rebuilt: it now does that
      by itself when a page stops answering. A first 23-hour run passed on 29 Oct
- [x] Decide the foreground-service type Google Play will accept: `connectedDevice`
      (Home Assistant controls the TV over the network), with Wi-Fi kept optional
- [x] Ask for "Display over other apps" from the PleatView menu on TVs that have that
      settings screen, with adb only as the fallback
- [ ] Test plan covers installing with Downloader and the "My Home Assistant" buttons.
      Downloader with `pleatview.hustlesman.com/apk`, then discovery and the PIN, worked
      on the Fire TV on 29 Oct and on the Chromecast on 3 Nov
- [x] Fixes from the first hardware run: Webpage dashboards (Grafana, OpenSearch…) open
      the site directly, with the zoom, instead of in Home Assistant's frame; a
      discovered TV goes straight to the PIN; the setup screen has a QR code for phones
- [x] PleatView comes back by itself when someone leaves it in another app and nothing has
      played sound for a while (10 minutes by default)

**Exit:** the test plan passes on all four devices, the soak passes, v1.1.0 is released.

## M2 — Public beta

*Mon 23 Nov → Fri 4 Dec 2026.* Real users, real feedback, and the testers Google Play
requires.

- [ ] The public beta announced on the Home Assistant Community forum and r/homeassistant
- [ ] GitHub issue templates: bug report (device, OS, Home Assistant version, logs) and
      device report
- [ ] **Demo for reviewers:** a "Try the demo" option on the setup screen, connected to
      an isolated, read-only demo Home Assistant (demo data only, never a real home).
      Store reviewers can't test without a server, and Google reviews beta builds too.
- [ ] **Phones, tablets and Chromebooks.** The same Android app, made for touch: the menu
      opens by touch, setup and the menu work in portrait, and Home Assistant gets battery
      and charging sensors for wall-mounted tablets. Google Play builds install on phones,
      tablets and Chromebooks as well as TVs. Checked with `tools/e2e/run.py` on phone and
      tablet emulators, and by hand on a real phone, tablet and Chromebook. (Added 3 Nov;
      it was in the parking lot.) Done on 3 Nov: touch, portrait layouts, wording, battery
      and charging sensors, Play builds for every form factor, and phone and tablet
      emulators passing every check. Still to do: the real phone, tablet and Chromebook
- [ ] Google Play beta: 12 or more testers on Google TV, Android phones, tablets or
      Chromebooks who stay in for 14 days, which Google requires before PleatView can go
      public. Sign up in
      [#9](https://github.com/Hustlesman/pleatview/issues/9)
- [ ] P1 bugs fixed within 48 hours; a beta build every week
- [ ] Device compatibility table on the website, filled from tester reports
- [ ] One-press pairing: approve Home Assistant on the TV with OK, instead of typing the PIN
- [x] Sign-ins for dashboards behind a login form or a password prompt: entered from a phone
      code, saved encrypted on the TV only, used again by themselves
- [x] **In Home Assistant's Marketplace.** Home Assistant 2026.11 builds HACS in as
      *Settings → Marketplace*, which lists the HACS default repositories. PleatView joined
      the HACS default list on 2 Nov ([hacs/default#12905](https://github.com/hacs/default/pull/12905))
      and its catalogue on 4 Nov. On 5 Nov, Home Assistant 2026.11.0 listed it and installed
      it without a GitHub account; the install guide says so

**Exit:** ≥12 testers active for 14 days, no open P1 bugs, the five most-reported issues
fixed, v1.2.0 released.

## M3 — Google Play and Amazon Appstore

*Mon 7 Dec 2026 → Fri 15 Jan 2027* (reviews and holidays included). Install from the
store on Google TV and Fire TV.

- [ ] Store listings with TV, phone and tablet screenshots, a feature graphic and a
      description
- [ ] Releases go to Google Play automatically from the release workflow
- [ ] Checked against Google's TV app quality guidelines
- [ ] Public on Google Play once the beta has run its 14 days
- [ ] Submitted to the Amazon Appstore for Fire TV and Fire tablets (Fire OS devices only)
- [ ] **Encrypted connections at home.** Today Home Assistant, the TV and the phone page talk
      plain HTTP on the local network ([SECURITY.md](SECURITY.md)). The TV makes its own
      certificate, and Home Assistant pins it when pairing, so the tokens travel encrypted.
      The phone page gets the same protection without a browser warning, or a reason why it
      can't. Home Assistant's token is also stored encrypted on the TV, like sign-ins

**Exit:** PleatView installs from Google Play on Google TV and from the Amazon Appstore on
Fire TV, and both links are on the website and in the README.

## M4 — Web player

*Mon 14 Dec 2026 → Fri 29 Jan 2027* (starts once M3 is submitted). One client for any TV
browser engine, and the base for the LG and Samsung apps.

LG and Samsung apps are web apps and can't run a server, so the TV has to connect to
Home Assistant instead of the other way round.

- [ ] **Protocol v2:** players connect out to Home Assistant's websocket. A player pairs
      by showing a code that you approve in Home Assistant. Same commands and state as
      v1, documented in `docs/protocol.md`.
- [ ] Integration: players appear as devices with the same `media_player` and `notify`
      entities and services; tests cover v2
- [ ] **Web player served by the integration** at `/pleatview/player`. Because it's on
      the same address as Home Assistant, dashboards and sign-in work without loosening
      Home Assistant's security headers. It handles kiosk mode, zoom, video
      (`<video>` with hls.js), camera images, notifications, standby, and reloads on
      stalls.
- [ ] Checked in Chrome, Firefox, Safari (including an iPad and an iPhone, added to the
      home screen) and a Raspberry Pi kiosk, plus a first run in
      the LG and Samsung TV browsers. Set the minimum webOS and Tizen versions (2021+
      models expected). A first look on 1 Nov, on a 2024 Samsung (Tizen 9, Chrome 120
      engine): Home Assistant dashboards work in its browser (connected in 2 s, all cards
      within 20 s, no errors), and it plays H.264, HEVC, VP9 and AV1. Home Assistant
      serves it the older ES5 build, because the browser calls itself "SamsungBrowser/8.0"
- [ ] The Android app stays on protocol v1 (moving it is in the parking lot)

**Exit:** the web player passes the test plan, minus native-only items, in a Chromium
kiosk. v1.4.0 is released.

## M5 — LG and Samsung apps

*Mon 1 Feb → Fri 5 Mar 2027.* Store apps that wrap the web player.

- [ ] Shared TV wrapper: first-run screen (enter the Home Assistant address with the
      remote, show the pairing code), opens the player full screen, Back and Exit
      behaviour per each store's rules, offline screen
- [ ] **LG webOS:** `.ipk` built with the webOS CLI, screen kept awake, tested in
      Developer Mode on a 2021+ LG TV, submitted to the LG Content Store
- [ ] **Samsung Tizen:** `.wgt` built with the Tizen CLI and Samsung certificates, remote
      keys registered, screensaver disabled while showing a dashboard, tested on a 2021+
      Samsung TV (Samsung Remote Test Lab for early checks), submitted to Samsung TV
      Apps
- [ ] OLED burn-in protection: optional pixel shift, plus guidance in the docs

**Exit:** both apps pass the vendors' pre-submission checklists on real TVs and are
submitted.

## M6 — On every store

*Mon 8 Mar → Fri 2 Apr 2027.* Everything listed, documented and supportable.

- [ ] LG and Samsung approvals, with review feedback fixed
- [x] Add the manifest category `media_entertainment.tv_streaming` once hassfest's
      published image accepts it (it does since 30 Oct)
- [ ] 30-day soak on one Android reference device, one LG TV and one Samsung TV
- [ ] Website: install guide per platform, automation cookbook, troubleshooting,
      compatibility matrix
- [ ] Support policy: Home Assistant versions supported (minimum version plus the last
      three monthly releases), OS versions supported, and a release within a week of
      each Home Assistant release when needed

**Exit:** the release is published on every channel and announced.

## M7 — iPhone and iPad app

*Mon 5 Apr → Fri 14 May 2027* (first estimate; may start earlier while M6 only waits on
reviews). Until then, iPhones and iPads use the web player in Safari (M4).

- [ ] Native app that wraps the web player, like the LG and Samsung apps: finds Home
      Assistant on the network, shows the pairing code, keeps the screen awake while a
      dashboard shows, and works with Guided Access for wall-mounted iPads
- [ ] Built and signed by GitHub Actions on macOS runners; TestFlight beta
- [ ] Submitted to the App Store

**Exit:** PleatView installs from the App Store on iPhone and iPad, and the link is on the
website and in the README.

## Platforms

| Platform | How | Store | When |
| --- | --- | --- | --- |
| Google TV / Android TV | Native app | Google Play | Yes (M3) |
| Fire TV on Fire OS | Same native app | Amazon Appstore | Yes (M3) |
| Android phones, tablets and Chromebooks | Same native app | Google Play | Yes (beta in M2, public in M3) |
| Fire tablets | Same native app | Amazon Appstore | Yes (M3) |
| iPhone and iPad | Web player in Safari, then a native app that wraps it | App Store | Web player (M4); app in M7 |
| LG webOS (2021+) | Web player in a webOS app | LG Content Store | Yes (M5) |
| Samsung Tizen (2021+) | Web player in a Tizen app | Samsung TV Apps | Yes (M5) |
| Browsers and kiosks | Web player served by Home Assistant | — | Yes (M4) |
| Amazon Echo Show 8 | Web player in the built-in Silk browser, opened by an Alexa routine or skill. Echo devices can't install apps, and the 2025 model runs Vega OS instead of Fire OS | — | After M6 |
| Google Nest Hub | A Google Cast receiver that loads the web player; Home Assistant casts it, and casts it again when the hub drops it | Google Cast (registered receiver) | After M6 |
| Fire TV on Vega OS, Hisense VIDAA, Philips/Titan OS, Xbox, Google Cast | Web player wrappers | Various | After M6 |
| Roku, Apple TV | No web engine for apps, so dashboards are impossible. Home Assistant's own integrations already cast media to them. | — | Not planned |

## Risks

| Risk | Mitigation |
| --- | --- |
| Google Play rejects the background service or the TV app review fails | Choose the service type in M1, prepare the declaration video, and if needed only run the control server while the app is visible on the Play build |
| A Home Assistant frontend release breaks kiosk mode or sign-in (monthly releases) | Scripted end-to-end test (M1) run against each Home Assistant beta; a fix release within a week |
| Store reviewers can't test without a Home Assistant server | Demo mode and a public read-only demo instance (M2) |
| LG or Samsung store approval takes too long | Meanwhile, the web player works in their TV browsers and sideloading is documented |
| OLED burn-in on displays that stay on 24/7 | Pixel shift option, dark themes, night standby, guidance in the docs |
| TV screensavers or energy-saving modes override keep-awake on webOS and Tizen | Use the platforms' keep-awake APIs in M5 and document the TV settings |
| Apple rejects an app that mostly shows web pages (App Store guideline 4.2) | Native features beyond the page (discovery, pairing, keep-awake, Guided Access); meanwhile the web player works in Safari |
| Smart displays close the page after a while: the Nest Hub stops a cast after about 10 minutes, and an Echo Show can go back to its home screen | The integration opens or casts the player again when it stops; checked on each device before it's listed |
| One developer, many platforms | The rules above: one milestone at a time, a fixed scope, and the parking lot |

## Parking lot

Ideas we'll consider after M6, or between milestones if there's slack:

- Picture-in-picture video over other apps (Android overlay window)
- Update prompts for GitHub installs (store installs update by themselves)
- Move the Android app to protocol v2, so there's a single protocol
- TV remote buttons as Home Assistant events for automations
- Assist (voice) on the TV
- Dashboard schedules (for example, a calendar in the morning and energy in the evening)
- Several Home Assistant servers on one TV
- Run the end-to-end test in CI every week against the Home Assistant beta (GitHub runners have KVM)
- A Downloader short code for `pleatview.hustlesman.com/apk` (digits instead of a URL on Fire TV)
- A standalone controller for PleatView TVs without Home Assistant: a small program for
  Windows, macOS and Linux with its web page at `localhost`, offering schedules, playlists,
  groups of TVs and a media library. Built on protocol v2, so it covers every PleatView TV,
  and designed alongside M4. It lives in this repository, next to the other clients of the
  protocol. The TV app gets a mode without Home Assistant
