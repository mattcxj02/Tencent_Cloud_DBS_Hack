# web

React 19 + Vite 6 + TypeScript (strict) + Tailwind 3 client for the DCTA prototype. The customer types or speaks payment requests, answers short clarification questions, corrects transcripts, reviews the **exact signing envelope stored by the gateway** and approves it with a passkey (Touch ID). It also manages the spending mandate, shows the evidence trail, and hosts a conventional baseline flow for the usability comparison. Everything is labelled as a **simulated bank**.

**Trust boundary.** This code runs in the user's browser and is untrusted. It talks **only to the gateway** (`/api/*`, proxied by Vite to `http://localhost:8002`; see `vite.config.ts`); internal routes and the agent/validator are never reachable. It holds no keys or tokens: the session is an HttpOnly cookie and passkey private keys stay in the authenticator. The UI cannot approve anything by itself. It signs the WebAuthn options the gateway derived from the stored envelope. The gateway then re-verifies the signature, the owner, the envelope hash and all policy before committing. Model-written text (validator reasons, clarification questions, transcripts) is rendered as plain text through React escaping. There is no `dangerouslySetInnerHTML`. Trace links are rendered only for `http(s)` URLs. Money is parsed and added in integer cents.

## Pages (hash routes)

| Route | What it does |
|---|---|
| `#/signin` (shown whenever signed out) | Sign in with a passkey (`/api/auth/login/*`); register a passkey with a one-time enrollment code (`/api/auth/enroll/*`). The credential that signs in is remembered for the tab and used to bind approvals |
| `#/chat` (default) | Conversation captured by the gateway (text, voice, answers, corrections). After each user turn: `POST /api/conversations/{id}/draft` → clarification (chips for "…: A, B or C?" questions; answers go to `/answer`), a draft that needs correction (validator issues, no approve button), or the confirmation. "Correct this" on any user turn calls `/transcript-edit` and redrafts; per-leg changes between drafts are listed in words ("Amount: ~~SGD 50.00~~ → SGD 15.00"). After a receipt the conversation is closed and the next message starts a new one |
| (in chat) Confirmation | For a `ready` draft (validated MATCH, `next_action` CONFIRM) it issues `POST /api/drafts/{id}/authorizations` and renders the returned **envelope** legs (action, recipient + "ending 4521", source account label, large amount, reference, equity quantity/price/debit/residual + "whole shares only"), total debit, independent-check verdict, 2-minute countdown, changes vs the previous draft, and an expandable "what your passkey signs" panel. **Approve** signs `webauthn_options` with `startAuthentication` and posts `/approve`. Handles denied biometrics (NotAllowedError), expiry (re-issue), superseded/stale (redraft), BLOCKED (reason codes → plain English), `AWAITING_STEP_UP` (separate one-time-code screen with attempts left, expiry, resend, cancel), COMMITTED (receipt), and lost responses (reads `GET /api/authorizations/{id}/receipt`). It never shows success without a stored receipt |
| `#/mandate` | Current settings, pending increase with activation time and remaining cooling, pause/resume, limit and payment-type changes (one direction per request, checked before submit), each signed as an `update_mandate` envelope with the passkey. States that pausing affects this assistant only |
| `#/evidence[/<conversation_id>]` | "You said → We clarified → You approved → Executed" from `GET /api/conversations/{id}/evidence`. Corrections are shown as replaced, and failed authorizations show their status, not a receipt. Expandable technical details: bundle/envelope/payload hashes, validator model and prompt hash, audit-chain result and events, and trace links (new tab) |
| `#/baseline` | **Baseline prototype (no money moves)**: a conventional pick-payee → amount → account → review → confirm flow over an in-memory copy of the seed payees/accounts. It makes no gateway calls. Includes the facilitator timer |

The footer toggles a **facilitator timer** on the chat page (local only). It records time and control activations per run and stops by itself when a receipt appears, or on "Mark completed".

## Files

| Path | Purpose |
|---|---|
| `src/main.tsx`, `src/App.tsx` | Entry; providers, header, hash routing, skip link, page titles |
| `src/api.ts` | Typed client for every gateway user route; `ApiError` carries the stable `code`, `reasonCodes`, body; `network` marks "no usable answer" (recover by reading state) |
| `src/messages.ts` | The single code → plain-English table (policy, approval, step-up, mandate, validation, orchestration, transport; voice codes spread in from `src/audio/messages.ts`). Unknown codes are shown as the code |
| `src/money.ts` | Decimal-string ↔ integer-cents parsing, sums and `SGD 1,234.50` formatting |
| `src/legs.ts` | Normalized leg view from a draft leg or an envelope leg; per-position diff between draft revisions |
| `src/format.ts` | SGT time formatting, countdown/duration text, hash shortening, `http(s)`-only link guard |
| `src/hooks.ts` | `useCountdown` (server-clock corrected), `useFocusWhen` |
| `src/passkey.ts` | `signWithPasskey`: wraps `startAuthentication` and maps NotAllowed/Security errors to user messages |
| `src/router.ts` | Hash routes |
| `src/session.tsx` | Session (`/api/auth/me`, bound credential, balances via `/api/accounts`, 401 handling) and a polite `aria-live` announcer |
| `src/components/ui.tsx` | Button (≥44 px), Card, Notice (text label + symbol; errors are `role=alert` and take focus), Badge, PageTitle (focus on open), Field |
| `src/components/Header.tsx` | Simulated-bank banner, user, balances, gateway health, nav (`aria-current`) |
| `src/components/Auth.tsx` | Sign-in / enrollment page |
| `src/components/Chat.tsx` | Chat orchestration: turn capture, draft requests, stale-response guard (epoch + conversation revision), no double submits |
| `src/components/Transcript.tsx` | Visible turns (latest correction at the original's position, earlier versions disclosed) and the inline correction editor |
| `src/components/Composer.tsx` | Text input and microphone button: one-time recording consent, 60 s auto-send, Esc to discard |
| `src/components/ClarifyCard.tsx` | Clarification question + option chips |
| `src/components/DraftReview.tsx` | Draft that cannot be approved: validator field issues with the user's quoted words, guidance by `next_action` |
| `src/components/LegList.tsx` | Leg rendering (large amounts, equity terms, changes in words) and total debit |
| `src/components/Confirmation.tsx` | Envelope issuance, review, passkey approval and every outcome/recovery path |
| `src/components/StepUp.tsx` | Simulated one-time code entry ("sent to your registered phone (simulated)") |
| `src/components/Receipt.tsx` | Stored receipt view (simulated) with link to the evidence trail |
| `src/components/MandatePage.tsx` | Mandate view, change forms and signed confirmation |
| `src/components/EvidencePage.tsx` | Evidence timeline and technical details |
| `src/components/BaselinePage.tsx` | UI-only baseline form flow (no gateway calls) |
| `src/components/FacilitatorPanel.tsx` | Usability timer / activation counter (page-local) |
| `src/audio/` | **Voice task (T-12), not this UI task:** `useRecorder` (AudioWorklet → 16 kHz mono WAV, 60 s limit), `api.ts` `uploadVoice` (multipart + `consent=true`), `messages.ts` voice codes, `wav.ts`; see `src/audio/README.md` |
| `public/audio-recorder-worklet.js` | Voice task: the `dcta-recorder` AudioWorkletProcessor loaded by `useRecorder` |

## Run

```bash
cd web && npm install && npm run dev     # http://localhost:5173 (gateway must run on 8002)
npm run build                            # tsc -b (strict) + vite build
```

WebAuthn works on `http://localhost` on the laptop. The gateway's `rp_origin` must equal the page origin (`http://localhost:5173`), because mutating routes check `Origin`. A phone needs an HTTPS origin and re-enrollment. The step-up code comes from the gateway's operator console (simulated channel; not a real SMS).

## Accessibility

Labelled controls and fieldsets, a skip link, and visible 3 px focus outlines. Focus moves to the page heading on navigation, to the confirmation/clarification/receipt heading when it appears, and to error messages. Status updates go to one polite `aria-live` region; the countdown is `role=timer` (not chatty), with one 30-second warning. Nothing is conveyed by colour alone: badges and notices carry words and symbols, changes show "before → after" text with strikethrough, and the current page is marked with `aria-current` and underline. Amounts are 30 px and tabular. Touch targets are at least 44 px. Layout is single-column and works at 375 px. Reduced motion is respected.

## Limitations

- There is no automated browser test or test runner (by instruction). Checks are `tsc` strict, the production build, and an ad-hoc Node render of the main pages and helpers. A real passkey, microphone and step-up walkthrough must be done manually in a browser (T-11 acceptance).
- Choice chips are parsed from the question's trailing "…: A, B or C?" list. Options containing ", " or " or " would split wrongly. Typing an answer always works.
- Draft-to-draft changes are matched by payment position, so reordering shows as changes.
- The source-account label on the confirmation comes from `/api/accounts`, because the envelope carries account IDs only.
- The baseline covers transfers, bills and card payments (no equities). Its facilitator timer counts control activations, not keystrokes.
