# DCTA Architecture

Three views of the same system. Interactive storyboard: see the *DCTA Demo Blueprint* artifact.

## 1. System map: trust zones, keys and data flow

```mermaid
flowchart TB
  subgraph C["Client · React app on phone"]
    UI["Chat · mic · text box"]
    OV["Confirm screen<br/>drawn from canonical bytes"]
    PK["Passkey<br/>secure chip · Face ID"]
  end

  subgraph A["Agent service · untrusted AI zone · no ledger write access"]
    ASR["Speech to text<br/>Web Speech → Tencent ASR"]
    WF["LangGraph workflow<br/>12 fixed nodes"]
    X1["Extractor LLM<br/>Hunyuan hy3"]
    X2["Validator LLM<br/>DeepSeek-V4-Flash"]
    RP["Intent Packs · agent half<br/>READ + DRAFT skills"]
  end

  subgraph G["Execution gateway · deterministic zone"]
    CH["Challenge builder<br/>Intent Envelope + nonce"]
    VF["Verifier<br/>WebAuthn · hash · nonce · expiry"]
    PO["Policy engine<br/>mandate · KYC · sanctions · velocity · scam"]
    SU["Step-up<br/>TOTP / RM callback mock"]
    EX["Intent Packs · exec half<br/>EXECUTE skills"]
  end

  subgraph B["Mock core bank"]
    LD[("Ledger<br/>accepts gateway-signed orders only")]
    RO[("Read-only view<br/>payees · balances · quotes")]
  end

  subgraph O["Trace and audit"]
    AU[("Audit log<br/>hash chain + signed checkpoints")]
    LF["Langfuse / Tencent APM<br/>OpenTelemetry traces"]
  end

  REG["Signed pack registry<br/>release key"]

  UI -->|"audio / text"| ASR --> WF
  WF <-->|"slots only"| X1
  WF <-->|"match / mismatch"| X2
  WF --> RP -->|"read"| RO
  WF -->|"draft · agent key"| CH
  CH -->|"envelope + challenge"| OV
  OV --> PK
  PK -->|"signature over SHA-256 of envelope"| VF
  VF --> PO
  PO -->|"PASS"| EX
  PO -->|"STEP_UP"| SU --> EX
  EX -->|"order · gateway key"| LD
  EX -->|"signed receipt"| UI
  WF -.->|"every step"| AU
  EX -.->|"every decision"| AU
  WF -.-> LF
  REG -.->|"allowlisted pack hashes"| EX
  WF --x|"no route · no credential"| LD
```

## 2. Agent workflow: the LLM appears in two nodes only

```mermaid
flowchart LR
  IN["ingest<br/>voice or text"] --> FW["firewall<br/>rules + quarantine"]
  FW --> EXT[/"extract · LLM<br/>schema-only slots"/]
  EXT --> NM["normalise<br/>number parser cross-check"]
  NM --> RS["resolve<br/>READ skills"]
  RS -->|"ambiguous or missing"| CL["clarify<br/>template question"]
  CL -->|"user answers"| EXT
  RS --> CP["compile<br/>canonical draft + provenance"]
  CP --> VL[/"validate · LLM<br/>transcript vs draft"/]
  VL -->|"mismatch"| FR(["FREEZE + alert"])
  VL -->|"match"| PC["pre-check<br/>mandate · KYC · risk"]
  PC -->|"BLOCK"| BK(["blocked + reason"])
  PC --> CF["confirm<br/>Face ID signs envelope"]
  CF --> GW["gateway<br/>separate process"]
  GW --> RC["receipt + read-back"]
```

Parallelogram nodes call an LLM. Every other node is plain Python.

## 3. Sign and execute: one payment end to end

```mermaid
sequenceDiagram
  autonumber
  actor U as User
  participant P as Phone app
  participant A as Agent service
  participant M as LLMs
  participant G as Gateway
  participant L as Ledger
  participant T as Audit log

  U->>P: "Send fifty bucks to Ah Ma"
  P->>A: audio or text
  A->>M: extract slots (schema only)
  M-->>A: transfer · "Ah Ma" · "fifty bucks"
  A->>A: normalise · resolve · compile (code only)
  A->>M: validate transcript against draft
  M-->>A: match
  A->>G: draft, signed with agent key
  G->>G: pre-check policy · build Intent Envelope · nonce valid 120 s
  G-->>P: envelope + challenge (relayed by agent)
  P->>U: confirm screen + check code
  U->>P: Face ID
  P->>G: WebAuthn signature over challenge
  G->>G: rebuild hash from own copy · verify signature · nonce · expiry · policy again
  alt amount over mandate or new payee
    G-->>P: 428 step-up required
    U->>P: one-time code
    P->>G: step-up proof
  end
  G->>L: execution order signed with gateway key
  L-->>G: committed (all legs or none)
  G->>T: append rows + signed checkpoint
  G-->>P: receipt signed with gateway key
  Note over A,L: The agent has no write path to the ledger
```
