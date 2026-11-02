# PleatView brand

The mark is a TV whose screen is three pleated panels, on a stand: *pleat* plus *view*.
Chosen on 29 October 2026 from a blind comparison with icons from AI image generators.

## Files

| File | Use |
| --- | --- |
| `pleatview-mark.svg` | The mark alone, trimmed, on a transparent background |
| `pleatview-mark.png` | The same as a 1568 × 1168 PNG with a transparent background |
| `pleatview-icon.svg` | App icon: the mark on a rounded dark square (also the website's favicon) |
| `pleatview-logo-on-dark.svg` | Mark and name for dark backgrounds |
| `pleatview-logo-on-light.svg` | Mark and name for light backgrounds |
| `github-social-preview.png` | 1280 × 640 card that sites show for links to the repository (set in GitHub's repository settings) |
| `build.py` | Generates all of the above and every PNG: run `tools/build-brand.sh` |

`build.py` also writes the app's launcher icon and TV banner, the Google Play images in
`site/img/play/`, the website icons, and `custom_components/pleatview/brand/`, which Home
Assistant (and its Marketplace) shows for the integration. Change the design there, not
in the generated files.

## Colours

| | Hex | Used for |
| --- | --- | --- |
| Cyan | `#18BCF2` | Bezel, stand, front panels, "View" |
| Deep cyan | `#0C7FAB` | The panel that folds away |
| Ink | `#0F1417` | Backgrounds; "Pleat" on light backgrounds |
| White | `#FFFFFF` | "Pleat" on dark backgrounds |

## Type

The name is set in Poppins SemiBold ([SIL Open Font License](https://openfontlicense.org)),
kerned, and converted to outlines in the SVG files, so it looks the same everywhere without
the font installed. "Pleat" takes the text colour, "View" is always cyan. Taglines use
Poppins Medium.

## Rules

- Keep the panels' two tones; don't recolour or outline the mark.
- Leave clear space around the logo of at least the bezel's thickness on every side.
- Smallest sizes: the icon from 16 px, the logo from 24 px tall.
- Don't combine it with Home Assistant's logo. PleatView is an independent project.

## Archive

`archive/alternative-d.png` was the runner-up, kept in case we want to switch. It came from
FLUX.1-schnell (Black Forest Labs, Apache 2.0 licence) through its public demo on Hugging
Face, seed 11, with this prompt:

> Minimal flat vector app icon for PleatView, an app that turns a TV into an always-on smart
> home dashboard. One bold geometric symbol: a widescreen TV screen built from three
> accordion-pleated, zig-zag folded panels, like a folding room divider, suggesting several
> dashboard views. Bright cyan blue #18BCF2 with a darker blue shade on the folded sides,
> centered on a deep charcoal #0F1417 rounded square. Crisp edges, generous padding, high
> contrast, readable at small size. Flat design, no text, no letters, no gradients, no
> shadows, no 3D, no house, no map, no mockup.

It's a 1024 px raster image. Using it would mean redrawing it as a vector in `build.py`
first. AI-generated images may also not be protected by copyright, unlike a drawn mark.
