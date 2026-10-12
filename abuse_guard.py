"""Pre-chat abuse guard: spam + manipulation detection with automatic chat block.

Every inbound chat turn passes through pre_check() before the agent runs:
  * active block  -> refused immediately (with minutes remaining)
  * spam burst    -> > SPAM_MAX_MSGS messages in SPAM_WINDOW_S seconds
  * repeat flood  -> same text sent REPEAT_MAX times in a row
  * manipulation  -> prompt-injection / policy-override phrasing in user text

post_check() runs after the turn: >= BULK_BLOCK_THRESHOLD governance BLOCKs
in one turn (e.g. "refund every order") also triggers a block.

A block refuses all further chat for that customer_id until blocked_until.
Every block is:
  * written to chat_blocks (enforcement),
  * audit-logged locally (audit_log, execution_status='blocked'),
  * reported to the WARDEN ledger as ACTION_EXECUTED/block_user_chat
    (best-effort, so it is visible in Warden Activity Logs).

Thresholds are env-tunable; the guard never raises (fail-open for chat
availability would defeat it, fail-closed would brick chat on DB errors, so
on unexpected DB errors it allows the turn and skips accounting).
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone

import database

# Manipulation signals: attempts to override instructions / policy via chat.
_INJECTION_RES = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", re.IGNORECASE),
    re.compile(r"ignore\s+the\s+return\s+policy", re.IGNORECASE),
    re.compile(r"system\s+override", re.IGNORECASE),
    re.compile(r"refund\s+every\s+order", re.IGNORECASE),
    re.compile(r"override\s+(the\s+)?policy", re.IGNORECASE),
    re.compile(r"approve\s+my\s+refund\s+(without|regardless)", re.IGNORECASE),
    re.compile(r"do\s+not\s+ask", re.IGNORECASE),
    re.compile(r"disregard\s+.*instructions", re.IGNORECASE),
    re.compile(r"jailbreak|prompt\s+inject", re.IGNORECASE),
]


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _has_injection(text: str) -> bool:
    t = text or ""
    return any(r.search(t) for r in _INJECTION_RES)


def _get_block(conn, customer_id: str):
    return conn.execute(
        "SELECT customer_id, reason, blocked_until, strikes FROM chat_blocks WHERE customer_id = ?",
        (customer_id,)).fetchone()


def _remaining_minutes(blocked_until: str) -> int:
    try:
        delta = datetime.fromisoformat(blocked_until) - _now()
        return max(1, int(delta.total_seconds() // 60) + 1)
    except (ValueError, TypeError):
        return 1


def _blocked_reply(reason: str, mins: int) -> str:
    return (f"⛔ Chat temporarily blocked: {reason} "
            f"Please try again in about {mins} minute{'s' if mins != 1 else ''}. "
            f"This attempt has been logged with WARDEN governance.")


def _report_to_warden(customer_id: str, reason: str, detail: str) -> None:
    """Best-effort: record the chat block in the WARDEN ledger for visibility."""
    base = os.environ.get("WARDEN_BASE_URL", "").strip().rstrip("/")
    if not base:
        return
    try:
        payload = json.dumps({
            "agent_id": os.environ.get("WARDEN_AGENT_ID", "shopassist_bot"),
            "decision_id": "",
            "action": "block_user_chat",
            "status": "BLOCKED",
            "result_summary": f"Customer {customer_id} auto-blocked from chat: {reason}. {detail}"[:500],
        }).encode()
        req = urllib.request.Request(
            base + "/v1/actions/report", data=payload, method="POST",
            headers={"Content-Type": "application/json"})
        api_key = os.environ.get("WARDEN_API_KEY", "").strip()
        if api_key:
            req.add_header("X-Warden-Api-Key", api_key)
        with urllib.request.urlopen(req, timeout=4) as r:
            r.read()
    except Exception:
        pass  # reporting must never break the chat turn


def _apply_block(conn, customer_id: str, reason: str, minutes: int,
                 user_text: str, request_id: str = "") -> dict:
    until = (_now() + timedelta(minutes=minutes)).isoformat()
    row = _get_block(conn, customer_id)
    strikes = int(row["strikes"]) + 1 if row else 1
    conn.execute(
        """INSERT INTO chat_blocks (customer_id, reason, blocked_until, strikes, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(customer_id) DO UPDATE SET
             reason = excluded.reason, blocked_until = excluded.blocked_until,
             strikes = excluded.strikes, updated_at = excluded.updated_at""",
        (customer_id, reason, until, strikes, _now().isoformat()))
    conn.execute(
        """INSERT INTO audit_log
           (timestamp, request_id, customer_id, action, args_json,
            eligibility, governance_decision, execution_status, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (_now().isoformat(), request_id or f"BLK-{customer_id}",
         customer_id, "chat_message", json.dumps({"user_text": (user_text or "")[:300]}),
         "", "BLOCK", "blocked",
         f"Chat blocked ({strikes} strike{'s' if strikes != 1 else ''}): {reason}"))
    conn.commit()
    _report_to_warden(customer_id, reason, f"Blocked for ~{minutes} min.")
    return {"blocked": True, "reason": reason,
            "reply": _blocked_reply(reason, minutes)}


def pre_check(customer_id: str, user_text: str) -> dict | None:
    """Return None if the turn may proceed, else a blocked-result dict."""
    text = (user_text or "").strip()
    try:
        conn = database.get_conn()
        try:
            now = _now()
            now_iso = now.isoformat()

            row = _get_block(conn, customer_id)
            if row and str(row["blocked_until"]) > now_iso:
                mins = _remaining_minutes(str(row["blocked_until"]))
                return {"blocked": True, "reason": str(row["reason"]),
                        "reply": _blocked_reply(str(row["reason"]), mins)}

            window_s = _int("SPAM_WINDOW_S", 60)
            cutoff = (now - timedelta(seconds=window_s)).isoformat()
            conn.execute("DELETE FROM chat_events WHERE created_at < ?", (cutoff,))
            conn.execute(
                "INSERT INTO chat_events (customer_id, user_text, created_at) VALUES (?, ?, ?)",
                (customer_id, text[:500], now_iso))

            recent = conn.execute(
                """SELECT user_text FROM chat_events
                   WHERE customer_id = ? AND created_at >= ?
                   ORDER BY id DESC LIMIT 32""",
                (customer_id, cutoff)).fetchall()
            recent_texts = [r["user_text"] for r in recent]

            max_msgs = _int("SPAM_MAX_MSGS", 15)
            if len(recent_texts) > max_msgs:
                conn.commit()
                return _apply_block(
                    conn, customer_id,
                    f"spam detected ({len(recent_texts)} messages in {window_s}s)",
                    _int("SPAM_BLOCK_MIN", 5), text)

            repeat_max = _int("REPEAT_MAX", 4)
            if text and len(recent_texts) >= repeat_max and \
                    all(t == text for t in recent_texts[:repeat_max]):
                conn.commit()
                return _apply_block(
                    conn, customer_id,
                    "repeat-message flood detected",
                    _int("SPAM_BLOCK_MIN", 5), text)

            if _has_injection(text):
                conn.commit()
                return _apply_block(
                    conn, customer_id,
                    "manipulation attempt detected (instruction override)",
                    _int("ABUSE_BLOCK_MIN", 30), text)

            conn.commit()
            return None
        finally:
            conn.close()
    except Exception:
        return None  # never brick chat on guard errors


def post_check(customer_id: str, user_text: str, trace: list) -> str | None:
    """Catch bulk/blocked-out turns (e.g. refund-every-order). Returns notice or None."""
    try:
        blocked = sum(1 for o in (trace or []) if o.get("governance_decision") == "BLOCK")
        if blocked < _int("BULK_BLOCK_THRESHOLD", 3):
            return None
        conn = database.get_conn()
        try:
            res = _apply_block(
                conn, customer_id,
                f"bulk manipulation blocked ({blocked} denied actions in one turn)",
                _int("ABUSE_BLOCK_MIN", 30), user_text or "")
            return res["reply"]
        finally:
            conn.close()
    except Exception:
        return None
