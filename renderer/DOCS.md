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

**A controller elsewhere:** set *Controller elsewhere* to its address, and this app
connects out to it, with nothing opened here, and renders for this site's PleatView Light
screens. Its log then shows a code to type into the controller. Pages from the controller
reach the public internet only, plus the places on this network you list under *Pages on this
network*. Set *Streams address* to this machine's address with port 8781, such as
`http://192.168.1.10:8781`, so this site's screens fetch their streams here. Every change the
controller makes is in the log. See
https://github.com/Hustlesman/pleatview/blob/main/docs/renderer-connect-out.md

**This Home Assistant, for that controller:** turn on *Share this Home Assistant*, and the
controller may use the entities under *Shared entities* (every camera, by default): their
states, cameras' pictures and live streams, to-do lists and calendars. Home Assistant needn't be
reachable from outside, and its token stays in this app. *Move screens to the controller* also
lets the controller have this Home Assistant's PleatView screens connect out to it, with nobody
at them; each move shows as a notification.

**This site's screens, for that controller:** the controller lists the PleatView screens this
Home Assistant's PleatView integration found, and adds them as on its own network: someone
presses OK on the screen, or reads its PIN out. Turn *Find screens for the controller* off to
stop it.

The whole guide, with pictures of each step and what to expect on the TV:
https://github.com/Hustlesman/pleatview/blob/main/docs/render-on-home-assistant.md
