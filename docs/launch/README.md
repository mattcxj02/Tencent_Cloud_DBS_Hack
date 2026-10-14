# Launch video

A 24.5-second launch video for SaySo, made from the demo walk-through deck. It follows one spoken request ("Pay my card balance from my current account and send 15 to John") from words to a receipt: the AI copies the words, code turns them into exact values, SaySo asks one question, the customer approves with a passkey and the gateway executes.

[![SaySo launch video](poster.jpg)](sayso-launch.mp4)

| File | What it is |
|---|---|
| [`sayso-launch.mp4`](sayso-launch.mp4) | The video: 1920×1080, 30 fps, 24.5 s, with music and sound effects |
| [`poster.jpg`](poster.jpg) | Poster frame (also baked in as the video's first frame, so platforms use it as the thumbnail) |
| [`cover-380x216.jpg`](cover-380x216.jpg), [`cover-1140x648.jpg`](cover-1140x648.jpg) | 16:9 end-card covers for the submission form (380×216 is the recommended size) |
| [`stills/`](stills/) | One settled frame per scene |
| [`share-copy.txt`](share-copy.txt) | One-line caption for posting |
| [`brag-plan.md`](brag-plan.md) | Storyboard: angle, scenes, timing, audio plan and where each on-screen claim comes from |
| [`composition-brief.md`](composition-brief.md) | The hand-off brief to Hyperframes |
| [`composition/`](composition/) | The Hyperframes project (one `index.html`, local fonts, GSAP, sound effects, audio-reactive data) |

Every product claim on screen is taken from the deck and the evidence in [`docs/evidence/`](../evidence/README.md). All bank data is the synthetic seed. The video carries the "simulated bank, synthetic data" disclaimer and uses no bank logo.

## Re-rendering

Requires Node.js 22+ and FFmpeg. The music track is not committed (see Credits): copy `happy-beats-business-moves-vol-11-by-ende-dot-app.mp3` from the [brag skill's bundled assets](https://github.com/latent-spaces/brag/tree/main/skills/brag/assets/music) into `composition/assets/music/` first.

```bash
cd docs/launch/composition
npx hyperframes check                                   # lint, layout and contrast gate
npx hyperframes preview                                 # open in the Studio to review or edit
npx hyperframes render --quality delivery --output ../sayso-launch.mp4
```

## Credits

| What | Used for | Licence |
|---|---|---|
| [brag](https://github.com/latent-spaces/brag) by latent-spaces | The launch-video workflow: plan, storyboard, composition brief, poster and share copy | MIT |
| [Hyperframes](https://github.com/heygen-com/hyperframes) by HeyGen | Composition checks, audio-reactive extraction and rendering to MP4 | Apache-2.0 |
| [GSAP](https://gsap.com) 3.14.2 | Animation timeline | [GSAP Standard License](https://gsap.com/standard-license) |
| "Happy Beats / Business Moves" vol. 11 by [ende.app](https://ende.app/en) | Music bed (bundled with brag) | Verify terms before redistributing |
| [Kenney](https://kenney.nl/) sound effects | Clicks, drops, impacts and bell (bundled with brag) | CC0 |
| [Keyboard Soundpack #1](https://opengameart.org/content/keyboard-soundpack-1-typing-and-single-keystrokes) by unicae_games | Typing ticks (bundled with brag) | CC0 |
| [Space Grotesk](https://fonts.google.com/specimen/Space+Grotesk), [IBM Plex Sans](https://fonts.google.com/specimen/IBM+Plex+Sans), [JetBrains Mono](https://fonts.google.com/specimen/JetBrains+Mono) | Display, body and label type (from Google Fonts) | SIL Open Font License 1.1 |
| [Claude Code](https://claude.com/claude-code) | Followed the brag workflow, wrote the composition and rendered it | — |
