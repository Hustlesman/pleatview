# Page steps: clicks, drags and scrolls after a page loads

Some pages need a hand before they're worth showing: a cookie banner to refuse, a pop-up to
close, a side bar to collapse, a map to turn. Nothing in their address can do that. PleatView
can, each time the page loads: add the steps to the end of the page's address, after
`#pleatview=`, separated by `;`.

```text
https://news.example/#pleatview=click:Reject all;click:css=#newsletter .close
```

Use the address for a Webpage dashboard (*Settings → Dashboards → Add dashboard → Webpage*)
or with `pleatview.show_url`. The TV app and PleatView for Linux take the steps from 1.0.19,
and [the renderer](render-on-home-assistant.md) from 1.0.18 (clicks by CSS from 1.0.19).
PleatView Light shows pages through the renderer.

## Steps

| Step | What it does |
| --- | --- |
| `wait:3` | Waits 3 seconds (60 at most) |
| `click:Reject all` | Clicks the first button or link whose text is exactly that, whatever its capitals, or else any other element showing only that text (a `div` included). A button's `aria-label` or title counts as its text, so an icon labelled *Close* is `click:Close` |
| `click:css=#consent .accept` | Clicks the first element on screen that the CSS selector matches, for a button without text. With a list, such as `css=.close, [aria-label="Dismiss"]`, the first of them in the page that shows |
| `click:50,20` | Clicks at 50 % of the screen's width and 20 % of its height |
| `drag:80,60,30,45` | Drags from one place to another with the left mouse button, in percent |
| `rightdrag:50,60,50,50` | The same with the right button, which turns or tilts many 3D maps |
| `scroll:50,50,-300` | Turns the mouse wheel there by 300 pixels: negative is up (zoom in on many maps), positive is down |

Up to 30 steps. Clicks on text or CSS wait up to 20 seconds for their target to show, since
pages draw banners late, and the steps go on without it if it never does. Presses are real
touches and mouse input, which the page can't tell from a hand.

## Examples

A cookie banner, then a newsletter pop-up whose close button is only an icon:

```text
https://news.example/#pleatview=click:Reject all;click:css=#newsletter button.close
```

Radware's live threat map with its side bar collapsed, turned left and tilted slightly, so
New Zealand shows with Europe, Africa and Asia:

```text
https://livethreatmap.radware.com/#pleatview=wait:5;click:Collapse;wait:2;drag:80,65,45,43;wait:2;click:Reject all
```

## Finding the right step

- **Text first.** `click:` with the words on the button works on every screen size, wherever
  the button sits.
- **CSS when there's no text.** In a desktop browser, right-click the button and choose
  *Inspect*: an `id` gives `css=#that-id`, a class `css=.that-class`, a label
  `css=[aria-label="Close"]`. A space, `#` or `"` can stay as it is in the address.
- **A place last.** `click:x,y` presses whatever shows there. Places are percentages, so the
  same steps fit every screen, but a TV at a higher density gets the page's narrower layout
  (a 1080p TV at density 2 shows it 960 pixels wide), where a button can be elsewhere.

## Good to know

- The steps run again after every load of the page, because a reload loses what they did. A
  click that reloads the page, as many cookie banners do, starts them again: put such clicks
  last.
- Clicks on text and CSS look in the page itself, not inside its frames. Many consent
  services draw their banner in a frame of their own: click that one by place.
- With a wrong step, such as an unknown one or a wait over 60 seconds, the TV app and
  PleatView for Linux open the page without its steps and say why in their log; the
  renderer refuses the page, and Home Assistant's log says why.
