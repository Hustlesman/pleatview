# A wall of live streams

![Four videos playing in a grid, full screen on the TV](../../site/img/screens/usecase-streams.webp)

YouTube live streams on one TV, muted, all day: station cams, a city skyline, the space
station, the news. One address holds them, in a grid that fits however many you list: one
fills the screen, four make two rows of two, nine three of three.

```
https://pleatview.hustlesman.com/wall?v=ID,ID,ID,ID
```

PleatView 1.0.14 and later show that address themselves, each row of the wall in a process
of its own, which spreads the players over the TV's processor cores. Anywhere else, such as
a browser or an older PleatView, the address opens a page that shows the same wall.

Every stream is a YouTube player that the TV runs and decodes, and a streaming stick
manages only a few. For more, let [Home Assistant render the wall](../render-on-home-assistant.md):
its machine runs the players and sends the TV one video, which any TV plays smoothly.

## 1. The address

Put the IDs of your streams after `?v=`, separated by commas, up to 16:

```
https://pleatview.hustlesman.com/wall?v=2tgHBRFHMm8,FHJH2yMe6Hw,1phWWCgzXgM,wxPi28clpk8
```

A stream's ID is the part of its address after `watch?v=`, `/live/` or `youtu.be/`. When a
live stream restarts, it can get a new ID: put the new one in the address. The stream has
to allow embedding; most live cams do. The players come from YouTube's privacy-enhanced
mode (youtube-nocookie.com).

## 2. A Webpage dashboard

*Settings → Dashboards → Add dashboard → Webpage*. Call it Live streams, and for the URL put
the wall's address. Home Assistant shows the wall in its own pages too, from a browser.

YouTube addresses in webpage cards don't play: Home Assistant's pages send no referrer, and
YouTube refuses embedded players without one (*Error 153: Video player configuration
error*). The wall's page sends one.

## 3. How many streams, how sharp

YouTube picks each player's quality from its size on the screen, and the TV has to decode
every stream at once. PleatView's **Zoom** (*Settings → Devices & services → PleatView →
Configure*) makes the players larger in YouTube's eyes: at 67 %, each quarter of a 1080p
screen asks for 720p instead of 480p. Sharper, but more for the TV to decode.

What we measured, with nine live streams of Amsterdam's station cams and others like them:

| TV | Streams | Result |
| --- | --- | --- |
| Fire TV Stick (3rd gen, 1 GB), zoom 100 % | 4 | 360p and 480p, all playing; about one frame in four dropped |
| Fire TV Stick (3rd gen, 1 GB) | 9 | Too many: the stick runs out of memory |
| Chromecast with Google TV (HD), zoom 67 %, one page | 9 | Buffering most of the time |
| Chromecast with Google TV (HD), zoom 67 %, wall address | 9 | Playing about two thirds of the time, most frames dropped |
| Both, **rendered on Home Assistant** | 9 | Every stream moving, about 3 seconds behind live |

On the TV itself, keep a wall to about four streams. For more, render it on Home Assistant.

YouTube remembers a device that dropped frames, and used to keep it at low quality for a
month, even once it would play well. From 1.0.14, PleatView has every player start afresh,
so a struggle on one day doesn't leave the wall blurry the next.

## 4. On the TV

To keep the wall up all day, set **Home dashboard** to `/dashboard-streams` (the
dashboard's address) in the same *Configure* dialog. Or show it for a while from an
automation:

```yaml
actions:
  - action: pleatview.show_dashboard
    target:
      entity_id: media_player.living_room_tv
    data:
      path: /dashboard-streams
      duration: 600
```

`path` can also be a wall's address, for a wall that isn't a dashboard.

The picture shows four of the Blender Foundation's open movies standing in for live
streams: Big Buck Bunny, Caminandes: Gran Dillama, Spring and Sintel (CC BY).
