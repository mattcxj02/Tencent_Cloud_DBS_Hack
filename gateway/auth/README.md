# gateway/auth

Who is calling the gateway.

| File | Purpose |
|---|---|
| `delegation.py` | Short-lived HMAC-SHA256 tokens (`dcta_<payload>.<sig>`) the gateway issues to the agent. Binds user, optional conversation, scopes and expiry. The user comes only from the verified token, never from a header or query parameter. Secret: `GATEWAY_DELEGATION_SECRET` (≥32 chars) or a random per-process secret |
| `session.py` | `require_user_session()` validates the browser's session cookie (HttpOnly, SameSite=Strict) against the server-side session table, including expiry and idle timeout. Fails closed on any doubt; tests substitute it only through FastAPI `dependency_overrides` |

There is no runtime bypass flag or header.
