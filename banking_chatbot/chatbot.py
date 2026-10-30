"""
Module 3 — Banking Chatbot with PCI DSS compliance guardrails (Module 6 Project 3).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from llm_client import get_llm_client, resolve_model

load_dotenv()

FINANCIAL_KEYWORDS = frozenset({
    "balance", "transfer", "account", "statement", "loan",
    "credit", "debit", "transaction", "payment",
})

AUDIT_LOG_PATH = Path(__file__).parent.parent / "audit_log" / "chatbot_tool_calls.jsonl"

RATE_LIMIT = 20
_session_counts: dict[str, int] = {}


class RateLimitExceeded(Exception):
    pass


# ---------------------------------------------------------------------------
# PCI DSS guardrails
# ---------------------------------------------------------------------------

def pre_response_pii_filter(response: str) -> str:
    """Redact card numbers (13-19 digits) and account numbers (8-12 digits)."""
    response = re.sub(r"\b\d{13,19}\b", "[REDACTED-CARD]", response)
    response = re.sub(r"\b\d{8,12}\b", "[REDACTED-ACCT]", response)
    return response


def rate_limit_check(session_id: str) -> None:
    """Increment per-session counter; raise RateLimitExceeded when limit is hit."""
    _session_counts[session_id] = _session_counts.get(session_id, 0) + 1
    if _session_counts[session_id] > RATE_LIMIT:
        raise RateLimitExceeded(f"Session '{session_id}' exceeded {RATE_LIMIT} financial queries.")


def _hash_for_log(value: str) -> str:
    """One-way hash a value so it can be correlated without storing plaintext PII."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def write_audit_log_entry(
    user_message: str,
    response: str,
    *,
    rate_limited: bool = False,
    pii_was_redacted: bool = False,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append a JSONL audit record; no plaintext PII is written."""
    AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "query_hash": _hash_for_log(user_message),
        "is_financial": is_financial_query(user_message),
        "session_count": _session_counts.get("default", 0),
        "rate_limited": rate_limited,
        "response_len": len(response),
        "pii_redacted": pii_was_redacted,
    }
    if extra:
        entry.update(extra)
    with AUDIT_LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Core LLM call
# ---------------------------------------------------------------------------

def is_financial_query(user_message: str) -> bool:
    """Return True if the message looks like a financial query."""
    tokens = set(re.findall(r"\w+", user_message.lower()))
    return bool(tokens & FINANCIAL_KEYWORDS)


def call_claude(user_message: str, tools: list[dict[str, Any]] | None = None) -> str:
    """Send a user message to Claude and return the text response."""
    client = get_llm_client()
    response = client.messages.create(
        model=resolve_model("claude-sonnet-4-5"),
        max_tokens=1024,
        tools=tools or [],
        messages=[{"role": "user", "content": user_message}],
    )
    for block in response.content:
        if block.type == "text":
            return block.text
    return ""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_with_guardrails(user_message: str, session_id: str = "default") -> str:
    """Chain rate-limit check → LLM call → PII filter → audit log."""
    if is_financial_query(user_message):
        try:
            rate_limit_check(session_id)
        except RateLimitExceeded:
            response = "Rate limit reached. Please start a new session to continue."
            write_audit_log_entry(user_message, response, rate_limited=True)
            return response

    raw = call_claude(user_message)
    safe = pre_response_pii_filter(raw)
    write_audit_log_entry(user_message, safe, pii_was_redacted=(raw != safe))
    return safe


def handle_message(user_message: str, session_id: str = "default") -> str:
    return run_with_guardrails(user_message, session_id)


if __name__ == "__main__":
    print(handle_message("What services does Heritage National Bank offer?"))
