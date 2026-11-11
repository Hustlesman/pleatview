# PleatView Renderer

Runs the dashboards of TVs that are too slow for them, here on the Home Assistant machine,
and streams each TV's screen to it as one H.264 video.

1. Start the app. Its *Log* shows its address, usually
   `http://ce7dee09-pleatview-renderer:8780`.
2. In *Settings → Devices & services → PleatView*, open the gear next to a TV and put that
   address in **Render on Home Assistant**. The TV needs PleatView 1.0.14 or later.

**Max screens** is how many TVs this app renders at once; each one shows a browser here.
A screen whose TV is off or in another app stops after 90 seconds.

Pages that need a click or a drag after they load (a map's view, a side bar, a cookie
banner) can carry the steps in their address, after `#pleatview=`: see *Clicks and drags
after a page loads* in the guide.

The whole guide, with pictures of each step and what to expect on the TV:
https://github.com/Hustlesman/pleatview/blob/main/docs/render-on-home-assistant.md
