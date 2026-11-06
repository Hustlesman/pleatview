# PleatView for Linux (beta)

Home Assistant dashboards, media and notifications on a screen attached to a mini PC or a
Raspberry Pi, controlled by the same PleatView integration as the Android app. Home
Assistant discovers it, you pair it with the PIN on screen, and it shows up as a media
player and a notifier.

For Ubuntu and Raspberry Pi OS (bookworm or trixie), on x86-64 and on ARM64 (Raspberry
Pi 4 and 5 with the 64-bit OS). Checked on a Raspberry Pi 5 (4 GB) with Raspberry Pi OS 13
and its desktop, on a monitor and on a TV with sound over HDMI, and on Ubuntu 24.04 in a
container; mini PCs and the Raspberry Pi 4 are next. A Raspberry Pi 4 needs 2 GB of memory
or more.

## Install

In a terminal on the computer (or over SSH), these two lines download the package for it
(`amd64` for Intel and AMD, `arm64` for a Raspberry Pi) from the newest
[release](https://github.com/Hustlesman/pleatview/releases) and install it:

```bash
wget -O /tmp/pleatview.deb https://github.com/Hustlesman/pleatview/releases/latest/download/pleatview_$(dpkg --print-architecture).deb
sudo apt install /tmp/pleatview.deb
```

Run them again to update. Each package carries a build attestation:
`gh attestation verify /tmp/pleatview.deb --repo Hustlesman/pleatview`.

Then start it with the computer:

- **No desktop** (Ubuntu Server, Raspberry Pi OS Lite): `sudo pleatview-kiosk enable` runs
  PleatView full screen on the first console as you, now and at every start. It uses cage,
  which apt installs with PleatView (if you turned recommended packages off:
  `sudo apt install cage`).
- **A desktop** (Ubuntu Desktop, Raspberry Pi OS): `pleatview-kiosk autostart`, then
  restart. Turn on automatic login in the desktop's settings if it isn't on. On a
  Raspberry Pi, leave Screen Blanking off (Raspberry Pi Configuration → Display, or
  `sudo raspi-config nonint do_blanking 1`): PleatView can't hold it off. PleatView is
  also in the applications menu.

`sudo pleatview-kiosk disable` undoes either. The setup screen shows a PIN: in Home
Assistant, open Settings → Devices & services, where the screen appears as a discovered
PleatView device.

On the screen, Escape, the menu key or a right-click (a long press on a touch screen)
opens the menu.

**No sound on a TV?** If the Raspberry Pi started while the TV was off, Raspberry Pi OS
doesn't find the TV's speakers. Restart the Pi with the TV on, or run
`systemctl --user restart wireplumber`.

## What's different from the Android app

- `turn_off` blanks the screen and puts it to sleep with `wlopm` (Raspberry Pi OS's
  desktop and other wlroots ones; `wlr-randr` under cage) or `xset` (X11), and puts a TV
  in standby over HDMI-CEC, on whichever HDMI port it's connected to, when `cec-ctl`
  (package `v4l-utils`) is installed.
- Not yet: sign-ins for dashboards behind a login form or a password prompt, and RTSP
  streams (cameras play through Home Assistant's HLS stream instead).
- Settings live in `~/.config/pleatview`, readable only by you.

## Develop

```bash
npm install
npm test                 # unit tests: protocol server, pairing, commands, configuration
npm start                # run it here (needs a display)
npm run dist             # .deb packages for x86-64 and ARM64 in dist/
```

Without a display, `tools/linux/headless.sh` runs it on a virtual screen in the
`tools/linux` Docker image; `PLEATVIEW_PORT` picks the API port, `PLEATVIEW_HOME` the
settings folder, and `PLEATVIEW_MDNS=0` keeps it from announcing itself.
