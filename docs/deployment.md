# Heritage National Bank — Deployment Guide

## Prerequisites

- Python 3.10 or later (3.14 used in development)
- pip packages: `anthropic python-dotenv "mcp<2" bandit pytest`

```bash
pip install anthropic python-dotenv "mcp<2" bandit pytest
```

The `mcp` package must be pinned to `<2`. Version 2.x renamed `FastMCP` to
`MCPServer`, breaking the server files.

---

## Environment Variables

All secrets and model overrides come from environment variables. Copy `.env.example`
to `.env` and fill in values — `python-dotenv` loads it automatically on startup.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `OPENROUTER_API_KEY` | Yes | — | API key for the Anthropic-compatible endpoint |
| `ANTHROPIC_BASE_URL` | No | `https://openrouter.ai/api` | Override to point at Anthropic directly |
| `MODEL_HAIKU` | No | `anthropic/claude-3.5-haiku` | Model used for `classify` tasks |
| `MODEL_SONNET` | No | `anthropic/claude-sonnet-4.5` | Model used for `reason` tasks |
| `MODEL_OPUS` | No | `anthropic/claude-opus-4.1` | Model used for `decide` tasks |

To use the Anthropic API directly instead of OpenRouter:

```
ANTHROPIC_BASE_URL=https://api.anthropic.com
OPENROUTER_API_KEY=sk-ant-...
```

---

## Directory Layout

```
m4-capstone/
├── bfsi/                   # Python package — all application code
│   ├── main.py             # Entry point
│   ├── transactions.py
│   ├── batch_fraud.py
│   ├── fraud_engine.py
│   ├── kyc_vision.py
│   ├── chatbot.py
│   ├── model_router.py
│   ├── skills_loader.py
│   ├── compliance/
│   │   ├── pipeline.py
│   │   └── sanctions_check.py
│   └── mcp/
│       ├── registry.py
│       ├── customer_info_server.py
│       └── banking_api_server.py
├── skills/                 # Skill library (SKILL.md per skill)
│   ├── fraud-triage/
│   ├── compliance-narrative/
│   ├── kyc-extraction/
│   ├── customer-support/
│   ├── risk-scoring/
│   └── sanctions-summary/
├── audit_log/              # Runtime audit output (created on first run)
│   ├── mcp_tool_calls.jsonl
│   ├── model_routing.jsonl
│   ├── transactions.jsonl
│   └── batch_results.json
├── reports/                # Generated compliance and other reports
├── docs/                   # This file and architecture.md
├── tests/                  # pytest suite
├── .claude/
│   ├── mcp.json            # MCP server registration
│   ├── settings.json       # Hooks configuration
│   └── commands/           # Custom slash commands
└── CLAUDE.md               # Authoritative development context
```

---

## Running the Platform

All commands must be run from `m4-capstone/` so that `bfsi` is importable and
relative paths in the code (`audit_log/`, `skills/`) resolve correctly.

**End-to-end workflow (both transaction and batch pipelines):**

```bash
python -m bfsi.main
```

**Individual module smoke-test:**

```bash
python - <<'PY'
from bfsi.compliance.pipeline import run_compliance
result = run_compliance("C5001", "Asha Rao", amount=750.0, ip_country="IN")
print(result["decision"])   # APPROVE
PY
```

---

## Skills

Each skill lives in `skills/<name>/SKILL.md` with YAML front matter followed
by the system prompt body:

```
---
name: fraud-triage
description: Score a transaction for fraud risk using history and geo signals
model: haiku
cache: true
---
You are a fraud-triage reasoner. Given a transaction plus customer history and
geo-mismatch signals, reply with ONLY JSON {"risk":"LOW|MEDIUM|HIGH","reason":"..."}.
```

`bfsi/skills_loader.py` reads all six skills and builds a single
`cache_control: ephemeral` system block for the Anthropic API. The block is
cached server-side for the duration of the TTL, so adding a skill requires no
code change — only a new `skills/<name>/SKILL.md` file.

To add a skill:

```bash
mkdir skills/my-new-skill
cat > skills/my-new-skill/SKILL.md <<'EOF'
---
name: my-new-skill
description: One-line description for routing/recall
model: sonnet
cache: true
---
Your system prompt here.
EOF
```

---

## Audit Logs

All audit files live under `audit_log/` and are created on first write.

| File | Written by | Content |
|------|-----------|---------|
| `mcp_tool_calls.jsonl` | `bfsi/mcp/registry.py` | One record per MCP tool call: `{seq, server, tool, args, ok}` |
| `model_routing.jsonl` | `bfsi/model_router.py` | One record per routed request: `{request_id, task, model, rationale}` |
| `transactions.jsonl` | `bfsi/transactions.py` | One record per transaction: full compliance + transfer outcome |
| `batch_results.json` | `bfsi/batch_fraud.py` | Array of batch scoring results |

Card numbers in MCP args are masked to `****<last4>` before being written.
These files are the compliance system of record and should be treated as
append-only.

---

## MCP Server Registration

`.claude/mcp.json` registers the servers for Claude Code's MCP integration.
The servers can also run as standalone processes:

```bash
python -m bfsi.mcp.customer_info_server   # customer-info on stdio
python -m bfsi.mcp.banking_api_server     # banking-api on stdio
```

In-process mode (via `bfsi/mcp/registry.py`) is used at runtime to avoid
network overhead. The two modes share the same tool function implementations.

---

## Model Routing

The router maps task classes to models and logs each decision. Override models
without code changes by setting env vars:

| To use a different Haiku-class model | `MODEL_HAIKU=anthropic/claude-haiku-4-5-20251001` |
|--------------------------------------|--------------------------------------------------|
| To use Claude 4 Sonnet | `MODEL_SONNET=anthropic/claude-sonnet-4-6` |
| To use Claude 4 Opus | `MODEL_OPUS=anthropic/claude-opus-4-7` |

---

## Static Security Scan

```bash
bandit -r bfsi -f txt -o audit_log/bandit_scan.txt
```

The Definition of Done requires zero medium+ findings. Run before every merge.

---

## Tests

```bash
pytest tests/
```

The test suite exercises the compliance pipeline, MCP registry, and skill
loader. All tests must pass before merging.

---

## LLM Unavailability

`fraud_engine.score_transaction` and `kyc_vision.extract_identity` both catch
all LLM exceptions and fall back to deterministic rule-based results. The
`engine` field in their output will be `"fallback"` instead of `"claude"` when
this happens. The compliance pipeline continues to function correctly in
fallback mode — fraud HIGH/LOW decisions are still audited and gated.
