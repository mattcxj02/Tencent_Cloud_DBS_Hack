# prompts

Version-controlled system prompts. Their hashes are logged with every model call and recorded with validation results.

- `extractor.md`: extraction prompt for the agent (T-09), `Version: extractor-v2` (v2, 12 Oct 2026: user-line text claiming to be the app, bank or a prior approval is ignored). The whole file is the system prompt; `agent/prompt.py` sends it byte for byte and logs `SHA-256(file bytes)` with every extraction (`agent.extract` audit event). Any edit changes the hash; bump the version line too.
- `validator.md`: semantic validator prompt (T-10)

Prompts are backend-controlled: users and the agent cannot change the validator's instructions, model or output schema. Write them as if they were public; never put secrets in a prompt. Conversation text reaches the extractor only as a JSON data object in the user message, and the prompt tells the model to treat it as data; that is a mitigation, not a guarantee, so trusted code re-checks every quote.

Validator v2 adds gateway-frozen calculation evidence and separates semantic agreement from gateway risk policy. It still refuses uncertain intent, changed fields and legs originating only from injected text. The version and SHA-256 of the prompt are reported by the validator; live regression against the original wrong-draft probes remains required after a prompt change.
