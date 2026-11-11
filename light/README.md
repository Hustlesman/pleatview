# PleatView Light

For screens on computers too small for a browser, such as a Raspberry Pi Zero 2 W (512 MB).
Home Assistant renders the dashboards ([Render on Home Assistant](../docs/render-on-home-assistant.md))
and PleatView Light only plays that stream, full screen, with mpv and the board's video
decoder. Media and notifications from Home Assistant play on top.

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

## Settings

In `/etc/default/pleatview-light`, then `sudo systemctl restart pleatview-light`:

| Setting | Meaning |
| --- | --- |
| `PLEATVIEW_LIGHT_SCREEN=1280x720` | Have Home Assistant render at this size instead of the screen's |
| `PLEATVIEW_LIGHT_MPV_OPTIONS=...` | mpv's video options; the default, `--vo=gpu --gpu-context=drm --hwdec=v4l2m2m`, decodes in hardware on Raspberry Pis |
| `PLEATVIEW_LIGHT_PORT=8765` | The control port |

Only Python's standard library and mpv: nothing else is installed.
