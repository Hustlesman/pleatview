# PleatView Renderer

Runs the dashboards of TVs that are too slow for them, here on the Home Assistant machine,
and streams each TV's screen to it as one H.264 video, with the pages' sound.

1. Start the app. Its *Log* shows its address, usually
   `http://ce7dee09-pleatview-renderer:8780`.
2. In *Settings → Devices & services → PleatView*, open the gear next to a TV and put that
   address in **Render on Home Assistant**. The TV needs PleatView 1.0.14 or later.

**Max screens** is how many TVs this app renders at once; each one shows a browser here.
A screen whose TV is off or in another app stops after 90 seconds.

Pages that need a click or a drag after they load (a cookie banner, a pop-up, a side bar,
a map's view) can carry the steps in their address, after `#pleatview=`: clicks on a
button's text or a CSS selector, clicks at a place, drags and scrolls. See page steps:
https://github.com/Hustlesman/pleatview/blob/main/docs/page-steps.md

The whole guide, with pictures of each step and what to expect on the TV:
https://github.com/Hustlesman/pleatview/blob/main/docs/render-on-home-assistant.md
