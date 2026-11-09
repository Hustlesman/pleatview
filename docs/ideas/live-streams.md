# A wall of live streams

![Four videos playing in a grid, full screen on the TV](../../site/img/screens/usecase-streams.webp)

Four YouTube live streams on one TV, muted, all day: a beach cam, the space station, a
wildlife cam, the news. They live on one small page in Home Assistant, in a grid that fits
however many you list: one fills the screen, four make two rows of two, nine three of three.

Any PleatView version shows it. From 1.0.12 the TV opens the page by itself, without Home
Assistant's frontend running underneath, which leaves more of a streaming stick's memory
for the videos.

## Why not webpage cards?

Home Assistant's pages send no referrer, and YouTube now refuses to play embedded videos
without one: a webpage card with a YouTube address shows *Error 153: Video player
configuration error*. A page of your own can send one, so the videos go there.

## 1. The page

Save this as `/config/www/streams.html`, for example with the File editor or Studio Code
Server add-on. If you had to create the `www` folder, restart Home Assistant once so it
serves it at `/local`.

```html
<!doctype html>
<!--
  A wall of YouTube live streams (or videos), muted, for a PleatView TV.
  Save it as /config/www/streams.html and open /local/streams.html?v=ID,ID,ID,ID
  An ID is the part of a video's address after watch?v=, /live/ or youtu.be/.
-->
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Live streams</title>
<style>
  html, body { margin: 0; height: 100%; background: #000; overflow: hidden; }
  body { display: grid; gap: 4px; }
  iframe { width: 100%; height: 100%; border: 0; }
</style>
</head>
<body>
<script>
  const ids = (new URLSearchParams(location.search).get("v") || "").split(",").filter(Boolean);
  const columns = Math.ceil(Math.sqrt(ids.length));
  document.body.style.gridTemplateColumns = `repeat(${columns}, 1fr)`;
  document.body.style.gridTemplateRows = `repeat(${Math.ceil(ids.length / columns)}, 1fr)`;
  for (const id of ids) {
    const frame = document.createElement("iframe");
    frame.src = `https://www.youtube.com/embed/${id}?autoplay=1&mute=1&controls=0&playsinline=1&rel=0&loop=1&playlist=${id}`;
    frame.allow = "autoplay; encrypted-media; picture-in-picture";
    // Home Assistant sends no referrer, and YouTube won't play without one (error 153).
    frame.referrerPolicy = "strict-origin-when-cross-origin";
    document.body.appendChild(frame);
  }
</script>
</body>
</html>
```

## 2. A Webpage dashboard

*Settings → Dashboards → Add dashboard → Webpage*. Call it Live streams, and for the URL
put the page with your streams' IDs:

```
/local/streams.html?v=ID,ID,ID,ID
```

If Home Assistant wants a full address there, use the one your TV uses for Home Assistant,
such as `http://192.168.1.10:8123/local/streams.html?v=ID,ID,ID,ID`.

A stream's ID is the part of its address after `watch?v=`, `/live/` or `youtu.be/`. When a
live stream restarts, it can get a new ID: put the new one in the URL. The stream has to
allow embedding; most live cams do.

## 3. The zoom, for a sharper picture

YouTube picks each player's quality from its size, and a quarter of the screen is small.
In *Settings → Devices & services → PleatView → Configure* for the TV, set **Zoom** to
**67 %**: each quarter of a 1080p screen then asks for 720p.

What we measured on a Fire TV Stick (3rd gen, 1 GB), with four live streams:

| Zoom | What YouTube sent |
| --- | --- |
| 100 % | 480p or 720p for each stream |
| 67 % | 720p where the stream has it. After a few minutes it settled at 720p, 480p, 480p and 360p, all playing smoothly |
| 50 % | It asked for 1080p, more than the stick can decode: streams stalled |

Fewer streams, or a faster device, leave room for more of them in HD.

**If the picture stays blurry.** YouTube remembers a device that couldn't keep up. After
our Fire TV Stick struggled at 50 %, every stream stayed at 144p to 360p, even after a
restart and at 67 %. Clearing PleatView's data fixed it: on a Fire TV, *Settings →
Applications → Manage installed applications → PleatView → Clear data*, then enter the
new PIN in Home Assistant when it asks. The TV keeps its name and settings.

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

The picture shows four of the Blender Foundation's open movies standing in for live
streams: Big Buck Bunny, Caminandes: Gran Dillama, Spring and Sintel (CC BY).
