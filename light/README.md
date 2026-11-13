# PleatView Light

For screens on computers too small for a browser, such as a Raspberry Pi Zero 2 W (512 MB).
Home Assistant renders the dashboards ([Render on Home Assistant](../docs/render-on-home-assistant.md))
and PleatView Light only plays that stream, full screen, with mpv and the board's video
decoder, and the dashboards' sound through HDMI. Media and notifications from Home Assistant
play on top.

It's paired and controlled like any PleatView screen: Home Assistant finds it, you enter the
PIN it shows, and in its options you choose a renderer under *Render on Home Assistant*.

## Install

On Raspberry Pi OS Lite (or any Debian or Ubuntu without a desktop):

```sh
wget -O /tmp/pleatview-light.deb https://github.com/Hustlesman/pleatview/releases/latest/download/pleatview-light_all.deb
sudo apt install /tmp/pleatview-light.deb
sudo pleatview-light enable
```

It starts with the computer and takes over the screen; `sudo pleatview-light disable`
undoes that. On a computer that ran the PleatView Linux app, it keeps that app's identity
and pairings, so Home Assistant carries on without pairing again.

**A controller elsewhere** ([protocol v2](../docs/protocol-v2.md)): with
`pleatview-light connect wss://controller.example/api/pleatview/v2`, the screen connects out
to that controller and shows a code to add it there with. No port has to be open to the
screen. A controller on the local network that pairs with the PIN moves the screen to v2 by
itself.

## Settings

In `/etc/default/pleatview-light`, then `sudo systemctl restart pleatview-light`:

| Setting | Meaning |
| --- | --- |
| `PLEATVIEW_LIGHT_SCREEN=1280x720` | Have Home Assistant render at this size instead of the screen's |
| `PLEATVIEW_LIGHT_MPV_OPTIONS=...` | mpv's video options. The default decodes in hardware on Raspberry Pis and puts the video on its own display plane: `--vo=gpu --gpu-context=drm --hwdec=v4l2m2m --gpu-hwdec-interop=drmprime-overlay --drm-draw-plane=overlay --drm-drmprime-video-plane=primary` |
| `PLEATVIEW_LIGHT_PORT=8765` | The control port |

A screen hung in portrait or upside down needs no setting here: choose *Screen mounted* in
its settings in Home Assistant, and the renderer sends the picture already turned.

Only Python's standard library, `python3-cryptography` (for its key) and mpv: nothing else
is installed.
