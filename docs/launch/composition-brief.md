# Hyperframes Composition Brief: SaySo

## Objective
Create a short launch-style brag video for SaySo.

## Output
- Composition directory: `docs/launch/composition/`
- Rendered video: `docs/launch/sayso-launch.mp4`
- Format: landscape — 1920x1080, 30fps
- Duration: 24.5 seconds

## Source Material
- Project root: repository root
- Primary files read: `references/SaySo How a Spoken Payment Flows.html` (13-slide deck, the storyboard source), `README.md`, `web/src/components/*` (Confirmation, ClarifyCard, LegList naming)
- Product name: SaySo
- Tagline / strongest claim: "Nothing moves without your say-so."
- Key UI to recreate: the clarification card and the confirmation card (deck slides 7 and 10), the resolve table (slide 6), the waveform-check mark (slide 1)
- Copy that must appear verbatim:
  - “Pay my card balance from my current account and send 15 to John”
  - Which “John” do you mean?
  - You sign exactly what you see
  - Independent check: MATCH
  - Nothing moves without your say-so.
  - AI reads your words. Code decides. Your passkey approves.

## Creative Direction
- Tone preset: app-store
- Creative direction: calm, precise fintech product film. Trustworthy, not hype.
- Interpretation: one idea per scene, clean slides and soft fades, generous holds; precision is the wow.
- Angle: one real spoken request travels the whole pipeline in one continuous take; the AI only reads, code decides, the customer approves.
- Hook: the waveform-check mark swells and the spoken request types out under "YOU SAY".
- Outro / punchline: amber end card, "Nothing moves without your say-so."
- Avoid: generic SaaS language, abstract filler, any DBS logo or bank branding, invented numbers.

## Visual Identity
- Background: #101826 (cards #1A2536, light UI #F5F2EB)
- Text: #F5F2EB on navy, #101826 on cream, muted #B8C2D0
- Accent: amber #E8A33D (AI / untrusted), teal #45B5AC / #1C6E68 (trusted code)
- Display font: Space Grotesk · Body: IBM Plex Sans · Labels: JetBrains Mono (local woff2 in `assets/fonts/`)
- Visual references: waveform-check mark, resolve table, clarify card, confirmation card, stat cards, amber closing slide

## Storyboard
Use the storyboard in `docs/launch/brag-plan.md` as the creative contract.

1. You say — 4.2s — waveform + typed request
2. AI reads, code decides — 4.7s — highlights + 3 resolved rows
3. One short question — 3.7s — clarify card, tap John Doe
4. You sign exactly what you see — 4.2s — confirmation card, MATCH, approve, executed
5. Proof — 3.2s — 46 / 46 and 0 / 30
6. Say-so — 4.5s — amber end card

## Audio
- Audio role: warm bed with sparse accents
- Audio arc: curious under the typing, lifts through the clicks, single bell on Executed, soft hit on the end card, fade out
- Music: `assets/music/happy-beats-business-moves-vol-11-by-ende-dot-app.mp3`, bed ~0.32, fade-in 0.4s, fade-out over the last 1.5s
- Music cue guidance: `assets/music/cues/happy-beats-business-moves-vol-11-by-ende-dot-app.music-cues.json`; lock 8.96s, 12.65s, 20.02s
- Audio-reactive treatment: subtle; RMS drives background glow and the hook waveform bars (`assets/audio-data.js`, extracted with hyperframes-creative `extract-audio-data.py`)
- Audio-coupled moments: typing ticks (scene 1), soft drops (scene 2 rows), click (scene 3 tap, scene 4 press), bell (Executed), soft impact (stats settle, end card)
- SFX selection guidance: low-HF-risk picks from `sfx-analysis.md`; volumes 0.45–0.75
- Audio files: copied into `composition/assets/`

## Hyperframes Instructions
Standalone `index.html`, one paused GSAP timeline registered as `window.__timelines["main"]`, local GSAP and fonts, no network. Run `npx hyperframes check` before render; render locally only.
