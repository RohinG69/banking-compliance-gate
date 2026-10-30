# Heritage National Bank — AI Platform Architecture

## Overview

The platform is a layered, production-grade AI banking system built on the
Anthropic API. A single entry point (`bfsi/main.py`) drives two workflows —
individual transaction processing and batch fraud scoring — by composing five
discrete layers: MCP data services, a skill library, a compliance pipeline, a
model router, and an audit subsystem.

```
┌─────────────────────────────────────────────────────────┐
│                     bfsi/main.py                        │
│          transactions.process()  batch_fraud.run_batch()│
└────────────────┬────────────────────────────────────────┘
                 │
   ┌─────────────▼─────────────┐
   │   Compliance Pipeline      │   bfsi/compliance/pipeline.py
   │   KYC → AML → Sanctions   │
   │         → Approval         │
   └──┬──────────┬─────────────┘
      │          │
      │    ┌─────▼───────────────┐
      │    │   Fraud Engine       │   bfsi/fraud_engine.py
      │    │   + KYC Vision       │   bfsi/kyc_vision.py
      │    │   + Sanctions Check  │   bfsi/compliance/sanctions_check.py
      │    └─────┬───────────────┘
      │          │
   ┌──▼──────────▼───────────────────────────────────────┐
   │               MCP Registry                          │
   │          bfsi/mcp/registry.py                       │
   │  ┌──────────────────┐  ┌──────────────────────────┐ │
   │  │  customer-info   │  │      banking-api          │ │
   │  │  get_customer    │  │  get_balance              │ │
   │  │  check_history   │  │  transfer_funds           │ │
   │  │  verify_ip_loc.  │  │  apply_loan               │ │
   │  └──────────────────┘  └──────────────────────────┘ │
   └─────────────────────────────────────────────────────┘
      │
   ┌──▼──────────────────────────────────────────────────┐
   │            Anthropic API  (model router)            │
   │   Haiku ← classify    Sonnet ← reason    Opus ← decide │
   │            bfsi/model_router.py                     │
   │   Skill library cached with cache_control:ephemeral │
   │            bfsi/skills_loader.py                    │
   └─────────────────────────────────────────────────────┘
      │
   ┌──▼──────────────────────────────────────────────────┐
   │                  Audit Subsystem                    │
   │  audit_log/mcp_tool_calls.jsonl  (every MCP hop)   │
   │  audit_log/model_routing.jsonl   (model selection) │
   │  audit_log/transactions.jsonl    (money movement)  │
   │  audit_log/batch_results.json    (batch scores)    │
   └─────────────────────────────────────────────────────┘
```

---

## Layer 1 — Entry Point

**`bfsi/main.py`**

Loads environment variables via `python-dotenv` then executes both workflows:

- `transactions.process()` — single-transaction path (compliance gated, audited)
- `batch_fraud.run_batch()` — bulk scoring path (Haiku tier, no money movement)

---

## Layer 2 — Transaction Processing

**`bfsi/transactions.py`**

Orchestrates one transaction end-to-end:

1. Calls `model_router.route("reason", ...)` — logs Sonnet selection.
2. Calls `compliance.pipeline.run_compliance()`.
3. If APPROVE: calls `model_router.route("decide", ...)` (Opus), then executes
   `registry.call("banking-api", "transfer_funds", ...)`.
4. Appends full result to `audit_log/transactions.jsonl` regardless of outcome.

Money movement only happens after all compliance gates pass. Opus is reserved
for the final approval step; Sonnet handles the compliance reasoning.

---

## Layer 3 — Compliance Pipeline

**`bfsi/compliance/pipeline.py`**

Sequential, fail-fast gate. Each stage either advances or returns a BLOCK with
the failing stage name for the audit trail.

| Stage | Implementation | Block condition |
|-------|----------------|-----------------|
| KYC | `registry.call("customer-info", "get_customer")` | `kyc_verified != true` |
| AML | `fraud_engine.score_transaction()` | `risk == "HIGH"` |
| Sanctions | `sanctions_check.sanctions_check()` | `hit == true AND strength == "STRONG"` |
| Approval | — | never blocks |

**`bfsi/compliance/sanctions_check.py`**

In-process watchlist screen (demo implementation of the OFAC/UN/EU interface).
Returns `{hit, strength}` where strength is `NONE`, `PARTIAL`, or `STRONG`.
PARTIAL matches are allowed through; STRONG matches block.

---

## Layer 4 — Fraud Detection & KYC

**`bfsi/fraud_engine.py`**

Scores a transaction for fraud risk using two MCP signals and the
`fraud-triage` skill:

1. `check_customer_history` — establishes a spending baseline (`avg_amount`).
2. `verify_ip_location` — detects country mismatch (`geo_mismatch`).
3. Calls Haiku with the `fraud-triage` system prompt to return `{"risk","reason"}`.
4. **Fallback rule** (if LLM unreachable): HIGH if `amount > max(avg*3, 5000)`
   or `geo_mismatch`; otherwise LOW. Engine field distinguishes `"claude"` from
   `"fallback"` in audit output.

**`bfsi/kyc_vision.py`**

Calls Sonnet with the `kyc-extraction` skill to parse identity fields from a
document description. Returns `{full_name, doc_type, doc_number, expiry}` with
`doc_number` masked to last 4 characters. Same fallback pattern as fraud engine.

---

## Layer 5 — Chatbot

**`bfsi/chatbot.py`**

Customer-facing service chatbot with two compliance controls before any LLM
call:

- **`sanitize(text)`** — strips non-printable characters and regex-matches
  prompt-injection patterns (`ignore previous`, `system prompt`,
  `full card number`), replacing matches with `[removed]`. Truncates to 2000
  characters.
- **`fraud_tools()`** — fetches `check_customer_history` and `verify_ip_location`
  via the audited MCP registry and injects them into the user message.

Supports streaming (`client.messages.stream`) and non-streaming modes. Uses
the `customer-support` skill from the cached skill library.

---

## Layer 6 — MCP Registry

**`bfsi/mcp/registry.py`**

Central dispatch table mapping `(server, tool)` pairs to Python functions.
Every call:

1. Looks up the handler in `_TOOLS`.
2. Executes it synchronously (in-process).
3. Masks card-number arguments (`_mask`) before writing the audit record.
4. Appends a JSONL record `{seq, server, tool, args, ok}` to
   `audit_log/mcp_tool_calls.jsonl`.

Running in-process avoids network overhead while preserving the MCP tool
interface. The servers can be extracted to standalone processes without changing
call sites.

**`bfsi/mcp/customer_info_server.py`** — `customer-info` tools:
- `get_customer(customer_id)` — returns master record including KYC flag.
- `check_customer_history(customer_id)` — returns transaction count and average amount.
- `verify_ip_location(customer_id, ip_country)` — returns geo mismatch flag.

**`bfsi/mcp/banking_api_server.py`** — `banking-api` tools:
- `get_balance(account_id)` — current balance.
- `transfer_funds(from, to, amount)` — validates and moves funds, checks sufficiency.
- `apply_loan(customer_id, amount, term_months)` — indicative underwriting decision.

---

## Layer 7 — Skill Library & Prompt Caching

**`bfsi/skills_loader.py`** / **`skills/<name>/SKILL.md`**

Six skills, each a YAML-fronted Markdown file:

| Skill | Model tier | Purpose |
|-------|-----------|---------|
| `fraud-triage` | Haiku | Risk score from amount deviation and geo signals |
| `kyc-extraction` | Sonnet | Identity field extraction from document text |
| `compliance-narrative` | Sonnet | Audit-ready narrative from pipeline stage results |
| `customer-support` | Sonnet | Guardrailed retail banking assistant |
| `risk-scoring` | Haiku | Customer-level risk tier assignment |
| `sanctions-summary` | Opus | Two-sentence analyst summary with CLEAR/ESCALATE |

`build_cached_system(active)` concatenates the requested skills into a single
system block marked `cache_control: {type: ephemeral}`. The Anthropic API caches
this block server-side, billing it once for many calls within the cache TTL.

---

## Layer 8 — Model Router

**`bfsi/model_router.py`**

Three task tiers, each with an env-var override:

| Task | Default model | Env var | Use case |
|------|--------------|---------|----------|
| `classify` | claude-3.5-haiku | `MODEL_HAIKU` | Batch fraud, intent tagging |
| `reason` | claude-sonnet-4.5 | `MODEL_SONNET` | Compliance reasoning, chatbot, KYC |
| `decide` | claude-opus-4.1 | `MODEL_OPUS` | Final approval, judge evaluation |

Every routing decision is appended to `audit_log/model_routing.jsonl` with
`{request_id, task, model, rationale}`.

---

## Layer 9 — Batch Pipeline

**`bfsi/batch_fraud.py`**

Scores a synthetic portfolio of four transactions through `score_transaction`
at the Haiku tier. Each item is routed with task `"classify"` before scoring,
producing a model-routing audit record. Results are written to
`audit_log/batch_results.json`.

---

## Data Flow: Settled Transaction

```
main.process("R1001", "C5001", ..., amount=750.0, ip_country="IN")
  → model_router.route("reason", "R1001")        [audit: model_routing.jsonl]
  → compliance.run_compliance("C5001", ...)
      → MCP: customer-info.get_customer           [audit: mcp_tool_calls.jsonl]
      → fraud_engine.score_transaction(...)
          → MCP: customer-info.check_history      [audit: mcp_tool_calls.jsonl]
          → MCP: customer-info.verify_ip          [audit: mcp_tool_calls.jsonl]
          → Anthropic API: Haiku / fraud-triage   (fallback if unreachable)
      → sanctions_check("Asha Rao")               [in-process, no MCP hop]
      → decision: APPROVE
  → model_router.route("decide", "R1001")         [audit: model_routing.jsonl]
  → MCP: banking-api.transfer_funds(750.0)        [audit: mcp_tool_calls.jsonl]
  → write audit_log/transactions.jsonl
```

## Data Flow: Blocked Transaction

```
main.process("R1002", "Ivan Petrov", ..., ip_country="RU")
  → model_router.route("reason", "R1002")         [audit: model_routing.jsonl]
  → compliance.run_compliance(...)
      → MCP: customer-info.get_customer           [audit: mcp_tool_calls.jsonl]
      → fraud_engine: geo_mismatch=true → HIGH
      → decision: BLOCK / failed_stage: AML
  → NO transfer, NO Opus call
  → write audit_log/transactions.jsonl
```
