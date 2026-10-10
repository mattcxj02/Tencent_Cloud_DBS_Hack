# web

React + Vite + TypeScript + Tailwind client. It talks **only to the gateway**: the dev server proxies `/api/*` to `http://localhost:8002`, where the gateway's user routes live (see `vite.config.ts`). Internal routes (`/internal/*`) and the agent/validator are never reachable from the browser.

| Path | Purpose |
|---|---|
| `src/main.tsx`, `src/App.tsx` | App entry and shell (shows gateway health) |
| `src/components/Auth.tsx` | Enroll a passkey with a one-time code, sign in, sign out |
| `src/components/Approve.tsx` | Loads an authorization and renders every leg of the **stored envelope** (action, recipient + masked suffix, amount, source, reference, equity terms) and the total debit, then signs the envelope's own challenge with the passkey |
| `src/components/Chat.tsx` | Chat / text input placeholder (voice arrives in T-12) |
| `src/components/Confirmation.tsx` | Placeholder; T-11 builds the full confirmation, mandate and evidence views |

```bash
cd web && npm install && npm run dev     # http://localhost:5173 (gateway must run on 8002)
npm run build
```

WebAuthn works on `http://localhost` on the laptop; a phone needs an HTTPS origin and re-enrollment. Money is added in integer cents, never floats. No keys or session tokens in frontend code.
