# Brag Plan: SaySo

## What is this app?
SaySo is a voice payment agent: you say routine payments in your own words, two independent AI models read the request, and only deterministic code, after your passkey, can move (simulated) money.

## The angle
Watch one real spoken request go all the way through, from words to a receipt, and see that the AI never gets to touch the money. The video *is* the deck's demo walk-through ("Pay my card balance from my current account and send 15 to John"), compressed into one continuous take: the same sentence stays on screen while it is read, questioned, approved and executed.

## Hook (first 2-3 seconds)
A voice waveform (SaySo's own waveform-into-check mark) swells in amber and the spoken request types itself out word by word under a "YOU SAY" label. The viewer reads a real, slightly messy banking sentence before they know what the product is.

## Key moments (the middle)
- The extractor highlights only the customer's own words ("card balance", "15", "John"), then code turns each into an exact value: SGD 642.30, SGD 15.00, and "two matches, ask".
- A short clarification card: "Which “John” do you mean?" A tap picks John Doe ·4521.
- The confirmation card: two payments, total SGD 657.30, "Independent check: MATCH", then Approve with Touch ID → one-time code → Executed, balance 3,500.00 → 2,842.70.
- Proof beat: 46 / 46 Rogue Agent Mode attacks held; 0 / 30 wrong drafts reached the approval screen.

## Outro / punchline
Amber end card, as in the deck: **Nothing moves without your say-so.** then "AI reads your words. Code decides. Your passkey approves."

## User flow worth showing
Speak a request → answer one clarifying question → approve the exact payments with a passkey → executed with a receipt.

## Tone
- Preset: app-store
- Creative direction: calm, precise fintech product film. Trustworthy, not hype.
- Interpretation: 6 short scenes with clean slides and soft fades, one idea per scene, no jokes; the "wow" is the precision of the flow. Restrained SFX at about 0.6–0.75.

## Format: landscape — 1920x1080
## Duration: 24.5s

## Visual identity (from the project: `references/SaySo How a Spoken Payment Flows.html`)
- Background: #101826 (deck navy); cards #1A2536; light UI surface #F5F2EB
- Accent: amber #E8A33D = AI / untrusted; teal #45B5AC (dark teal #1C6E68 on light) = trusted code
- Text: #F5F2EB on navy; #101826 on cream; muted #B8C2D0
- Display font: Space Grotesk · Body font: IBM Plex Sans · Labels: JetBrains Mono
- Strongest visual element: the waveform-that-ends-in-a-check-mark from the title slide; the confirmation card from the "You sign exactly what you see" slide.

## Share copy (draft)
SaySo: say your payments in your own words. Two AI models read them, plain code decides, and nothing moves without your passkey.

## Audio direction
- Role: warm bed with sparse, motion-matched accents
- Music: `happy-beats-business-moves-vol-11-by-ende-dot-app.mp3` (warm, business-y, 114.8 BPM)
- Music treatment: starts at 0 at bed level (~0.32), gentle fade-in over 0.4s, fades out over the last 1.5s under the end card
- Music cue guidance: preset `assets/music/cues/happy-beats-business-moves-vol-11-by-ende-dot-app.music-cues.json`. Strong cues to lock: 8.96s (clarify card lands), 12.65s (confirmation card lands), 20.02s beat for the end-card wipe. Beat grid ~0.52s: highlights in scene 2 on every other beat (readable text).
- Audio-reactive treatment: subtle; music RMS breathes the background glows and the hook waveform bars. No equalizer visuals beyond the product's own waveform mark.
- SFX posture: sparse (6–8 cues), low-HF-risk files only
- Audio-coupled moments: soft key ticks while the request types; soft drops as values resolve; a click on the John Doe tap and the Approve press; a bell when Executed lands; a soft impact on the end card.
- Restraint rule: no stacked hits, nothing above 0.8 volume, no typing sound on every character.

## Storyboard

### Scene 1 — You say — 0.00–4.23s (4.2s)
Navy canvas, amber waveform bars rise and breathe; label "YOU SAY"; the request types out: “Pay my card balance from my current account and send 15 to John”. Fully typed by ~2.7s, then held.
Sequential/interaction: yes — typed request, word-paced.
Audio intent: open warm and curious. Audio-coupled idea: sparse keypress ticks during typing.
Transition mood: soft → the sentence itself carries into Scene 2 (shrinks and rises).

### Scene 2 — AI reads, code decides — 4.23–8.96s (4.7s)
Header: "AI copies your words. Code turns them into money." The sentence sits up top; amber highlight boxes land on “card balance”, “15”, “John”. Three rows resolve one by one: “card balance” → SGD 642.30; “15” → SGD 15.00; “John” → two matches · ASK (amber). Every row then holds together.
Sequential/interaction: yes — 3 highlights, then 3 rows (every other beat), held ≥1.5s together.
Audio-coupled idea: soft drop per resolved row.
Transition mood: clean slide → Scene 3.

### Scene 3 — One short question — 8.96–12.65s (3.7s)
Cream SaySo card: "Which “John” do you mean?" with two options, John Doe · account ending 4521 and John Tan · account ending 8892. A cursor taps John Doe; it fills teal with a check. Side caption: "One short question instead of a guess."
Sequential/interaction: yes — simulated tap.
Audio-coupled idea: click on tap. Beat-lock card entrance at 8.96s.
Transition mood: clean slide → Scene 4.

### Scene 4 — You sign exactly what you see — 12.65–16.86s (4.2s)
Confirmation card (from the deck): CONFIRM 2 PAYMENTS · Credit card ·4242 SGD 642.30 · John Doe ·4521 SGD 15.00 · Total debit SGD 657.30; badge "Independent check: MATCH" pops; button "Approve with Touch ID" is pressed; status chips: Touch ID ✓ → One-time code ✓ → Executed; "Current 3,500.00 → 2,842.70".
Sequential/interaction: yes — simulated press, status chips in sequence.
Audio-coupled idea: click on press; bell when Executed lands.
Transition mood: soft fade → Scene 5.

### Scene 5 — Proof — 16.86–20.02s (3.2s)
Two big counters on navy: "46 / 46" Rogue Agent Mode attacks held · "0 / 30" wrong drafts reached approval. Small source line: measured on live runs.
Sequential/interaction: yes — count-ups.
Audio-coupled idea: one soft impact as the numbers settle.
Transition mood: amber wipe on the 20.02s beat → Scene 6.

### Scene 6 — Say-so — 20.02–24.50s (4.5s)
Amber end card: waveform-check mark + "SaySo"; "Nothing moves without your say-so."; then "AI reads your words. Code decides. Your passkey approves."; footer disclaimer "Hackathon prototype on a simulated bank with synthetic data."
Audio-coupled idea: one warm logo hit; music fades out.

**Music mood for this video:** upbeat, warm, corporate-clean
**Audio summary:** a warm bed that starts curious under the typed request, picks up with the flow's clicks and resolves into a single bell on Executed and a soft hit on the end card.

## Grounding notes
Every product claim on screen is copied or lightly trimmed from the deck: slide 4 (request, balances, two Johns), slide 5–6 (copy-only extraction; resolve table), slide 7 (clarify card), slide 10 (confirmation card, step-up over SGD 500, 3,500.00 → 2,842.70), slide 12 (46/46, 0/30), slide 13 (tagline, disclaimer). All bank data is the synthetic seed; no real customer data. No DBS logo or branding is used.
