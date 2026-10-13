# docs/cover

The 16:9 cover image for the hackathon submission (handbook p. 37: "A 16:9 cover image … Recommended size: 380×216px").

| File | Purpose |
|---|---|
| `cover.svg` | Source, 1920×1080. Plain shapes and text only: no third-party logos, images or external fonts. Shows the name, the blurb, "DBS Track · Scenario 1" and the agent → validator → gateway (with passkey) motif |
| `cover.png` | 1920×1080 export (headless Chrome, 13 Oct 2026, after the rename to SaySo) |
| `cover-380x216.png` | The same image scaled to the recommended 380×216 for the showcase |

The text falls back through `Inter`, `Helvetica Neue`, `Segoe UI` and `Arial`. The PNGs were rendered on macOS, so they use the first of these that is installed. Check that the title and blurb fit after any edit. The blurb ends at about x = 900 and the right-hand column starts at x = 1090.

## Export to PNG

macOS with Google Chrome (this is how the committed PNGs were made):

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu \
  --hide-scrollbars --force-device-scale-factor=1 --window-size=1920,1080 \
  --screenshot="$PWD/docs/cover/cover.png" "file://$PWD/docs/cover/cover.svg"
sips -z 216 380 docs/cover/cover.png --out docs/cover/cover-380x216.png
```

Alternatives: `rsvg-convert -w 1920 -h 1080 docs/cover/cover.svg -o docs/cover/cover.png` (librsvg); `inkscape docs/cover/cover.svg --export-type=png --export-width=1920`; or open the SVG in a browser and take a 1920×1080 screenshot. Miora can restyle the cover, but it is optional. Keep the 16:9 ratio, keep the blurb exactly as submitted, and add no bank or vendor logos.
