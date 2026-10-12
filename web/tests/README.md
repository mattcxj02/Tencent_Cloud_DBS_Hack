# Clarification browser regressions

`clarification.html` runs six browser checks against the real `ClarifyCard` and API
serializer with synthetic records and a stubbed `fetch`. It covers grouped selection,
question/field/leg/revision bindings, question reset/focus, none-of-these fallback,
single-choice IDs, disabled submission and legacy chips. No gateway, model, session,
passkey, microphone or ledger is used; this fixture is outside the production entry.

From `web/`, run:

```sh
npx tsc --noEmit -p tests/tsconfig.json
npm run dev -- --host 127.0.0.1
```

Open `http://localhost:5173/tests/clarification.html`; the page must show
`6 PASS`. No extra dependencies are required. This is a browser fixture, not a full
payment/voice walkthrough or a headless CI runner.

## Progress and recovery

`progress.html` runs 14 checks against the real transport, polling, progress notice,
chat, transcript correction and existing-payment recovery components. Responses,
recorded turns, identity and receipts are synthetic; no model, gateway, ledger,
passkey or microphone is used. It covers browser/response-body timeout, bound polling and completion,
late cancellation, quiet elapsed ticks, one automatic latest-revision rebuild, bounded
rebuild attempts, voice correction during drafting, permanent setup errors, reload of
stored progress, unavailable payment-state recovery, and receipt retrieval without
issuing a replacement authorization.

Use the same type-check/development command above, then open
`http://localhost:5173/tests/progress.html`; it must show `14 PASS`. For read-only
symlinked dependencies, start Vite with `npm run dev -- --configLoader runner`.
This fixture is outside the production entry. Fixture type checking and production
building do not substitute for executing the fixture in a browser. Real microphone,
passkey, small viewport and navigation walkthroughs remain separate operator checks.
