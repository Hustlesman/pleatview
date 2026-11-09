# PleatView Renderer

Runs the dashboards of TVs that are too slow for them, here on the Home Assistant machine,
and streams each TV's screen to it as one H.264 video.

1. Start the add-on. Its *Log* shows its address, such as
   `http://a0d7b954-pleatview-renderer:8780`.
2. In *Settings → Devices & services → PleatView → Configure* on a TV, put that address in
   **Render on Home Assistant**. The TV needs PleatView 1.0.14 or later.

**Max screens** is how many TVs this add-on renders at once; each one shows a browser
here. A screen whose TV is off or in another app stops after 90 seconds.

The whole guide, with what to expect on the TV:
https://github.com/Hustlesman/pleatview/blob/main/docs/render-on-home-assistant.md
