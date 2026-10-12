"""ActionGateway: sits between agent tool calls and business functions.

Mode A (Demo): local policy simulator decides.
Mode B (WARDEN): POST to configurable endpoint; fail closed on any
timeout / error / invalid response. Never silently bypass governance.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import tools
from database import get_conn
from mock_warden import simulate_decision

STATE_CHANGING = {"initiate_refund", "cancel_order", "request_return",
                  "request_replacement", "escalate_to_human",
                  "request_human_review"}

# ShopAssist tools -> WARDEN governance vocabulary.
WARDEN_ACTION_MAP = {
    "initiate_refund": ("issue_refund", "partially"),
    "cancel_order": ("update_record", "reversible"),
    "request_return": ("update_record", "partially"),
    "request_replacement": ("issue_replacement", "partially"),
    "escalate_to_human": ("create_ticket", "reversible"),
    "request_human_review": ("create_ticket", "reversible"),
}


def _shop_order_context(order_id: str | None) -> dict:
    """Order facts WARDEN company rules reason over (total, age, history)."""
    if not order_id:
        return {}
    try:
        from datetime import date
        conn = get_conn()
        row = conn.execute("SELECT amount, order_date, refund_status FROM orders WHERE order_id = ?",
                           (order_id,)).fetchone()
        conn.close()
        if not row:
            return {}
        try:
            age_days = (date.today() - date.fromisoformat(row["order_date"])).days
        except (ValueError, TypeError):
            age_days = 0
        return {"total": float(row["amount"]), "age_days": age_days,
                "already_refunded": row["refund_status"] == "refunded",
                "replacements_used": 0}
    except Exception:
        return {}


def _audit(request_id, customer_id, action, args, eligibility,
           governance, status, reason, db_path=None):
    conn = get_conn(db_path)
    conn.execute(
        """INSERT INTO audit_log
           (timestamp, request_id, customer_id, action, args_json,
            eligibility, governance_decision, execution_status, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now(timezone.utc).isoformat(), request_id, customer_id,
         action, json.dumps(args, default=str), eligibility, governance,
         status, reason),
    )
    conn.commit()
    conn.close()


def _warden_config() -> dict:
    return {
        # Native WARDEN (CODEX) contract: base URL + agent passport + API key.
        "base_url": os.environ.get("WARDEN_BASE_URL", "").strip().rstrip("/"),
        "agent_id": os.environ.get("WARDEN_AGENT_ID", "shopassist_bot").strip(),
        # Legacy generic contract: full decision-endpoint URL (ALLOW/BLOCK/REQUIRE_APPROVAL).
        "endpoint": os.environ.get("WARDEN_ENDPOINT", "").strip(),
        "api_key": os.environ.get("WARDEN_API_KEY", "").strip(),
        "timeout": float(os.environ.get("WARDEN_TIMEOUT_S", "5")),
        "poll_timeout": float(os.environ.get("WARDEN_POLL_TIMEOUT_S", "30")),
        "poll_interval": float(os.environ.get("WARDEN_POLL_INTERVAL_S", "1")),
        "simulate_outage": os.environ.get("WARDEN_SIMULATE_OUTAGE", "") == "1",
    }


def gateway_mode() -> str:
    cfg = _warden_config()
    return "WARDEN" if (cfg["endpoint"] or cfg["base_url"]) else "DEMO"


def check_warden_connection(timeout: float = 4.0) -> dict:
    """Validate a real endpoint; never claim connected without a response."""
    import urllib.request
    cfg = _warden_config()
    if cfg["simulate_outage"]:
        return {"connected": False, "reason": "Simulated outage (WARDEN_SIMULATE_OUTAGE=1)."}
    if cfg["base_url"]:
        target, use_bearer = cfg["base_url"] + "/health", False
    elif cfg["endpoint"]:
        target, use_bearer = cfg["endpoint"], True
    else:
        return {"connected": False, "reason": "No WARDEN_BASE_URL/WARDEN_ENDPOINT configured (Demo Mode)."}
    try:
        req = urllib.request.Request(target, method="GET")
        if use_bearer and cfg["api_key"]:
            req.add_header("Authorization", f"Bearer {cfg['api_key']}")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if 200 <= r.status < 300:
                return {"connected": True, "reason": f"WARDEN responded HTTP {r.status} at {target}."}
            return {"connected": False, "reason": f"WARDEN returned HTTP {r.status}."}
    except Exception as e:  # fail closed: report not-connected
        return {"connected": False, "reason": f"Unreachable: {e}"}


def _warden_http(method: str, url: str, payload: dict | None, cfg: dict, timeout: float) -> dict:
    import urllib.request
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if cfg["api_key"]:
        req.add_header("X-Warden-Api-Key", cfg["api_key"])
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def _poll_warden_decision(cfg: dict, decision_id: str) -> dict:
    """Poll a PENDING_APPROVAL until a human resolves it or we time out."""
    import time
    url = f"{cfg['base_url']}/v1/actions/decision/{decision_id}"
    deadline = time.time() + cfg["poll_timeout"]
    while time.time() < deadline:
        try:
            body = _warden_http("GET", url, None, cfg, cfg["timeout"])
            d = str(body.get("decision", "")).upper()
            if d == "ALLOW":
                return {"decision": "ALLOW",
                        "reason": str(body.get("explanation", "Approved by human reviewer in WARDEN."))}
            if d == "BLOCKED":
                return {"decision": "BLOCK",
                        "reason": str(body.get("explanation", "Denied by human reviewer in WARDEN."))}
        except Exception:
            pass  # transient polling hiccup: keep trying until timeout
        time.sleep(cfg["poll_interval"])
    return {"decision": "REQUIRE_APPROVAL",
            "reason": f"Still pending in WARDEN after {cfg['poll_timeout']:.0f}s "
                      f"(decision {decision_id}) — held for human review."}


def _report_warden_execution(cfg: dict, decision_id: str, warden_action: str,
                             status: str, summary: str) -> None:
    """Best-effort post-execution report back to the WARDEN ledger."""
    if not cfg["base_url"] or not decision_id:
        return
    try:
        _warden_http("POST", cfg["base_url"] + "/v1/actions/report", {
            "agent_id": cfg["agent_id"], "decision_id": decision_id,
            "action": warden_action, "status": status,
            "result_summary": str(summary)[:500],
        }, cfg, cfg["timeout"])
    except Exception:
        pass  # reporting must never break the user turn


def _call_warden_native(action: str, args: dict, context: dict, request_id: str, cfg: dict) -> dict:
    """Native CODEX-WARDEN contract: /v1/actions/request + approval polling."""
    if cfg["simulate_outage"]:
        raise TimeoutError("Simulated WARDEN outage.")
    mapped, reversibility = WARDEN_ACTION_MAP.get(action, (action, "partially"))
    try:
        amount = float(args.get("amount") or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    if amount == 0.0 and args.get("order_id"):
        # No explicit amount: govern the true exposure (full order value),
        # otherwise a high-value refund would score as zero-risk.
        try:
            conn = get_conn()
            row = conn.execute("SELECT amount FROM orders WHERE order_id = ?",
                               (args["order_id"],)).fetchone()
            conn.close()
            if row:
                amount = float(row["amount"])
        except Exception:
            pass
    body = _warden_http("POST", cfg["base_url"] + "/v1/actions/request", {
        "agent_id": cfg["agent_id"],
        "action": mapped,
        "params": {
            "shopassist_action": action,
            "customer_id": args.get("customer_id") or context.get("customer_id"),
            "owner_id": context.get("order_owner"),
            "order_id": args.get("order_id"),
            "amount": args.get("amount"),
            "reason": str(args.get("reason", ""))[:300],
            "user_request": str(context.get("user_text", ""))[:500],
        },
        "amount": amount,
        "records_affected": 1,
        "data_class": "internal",
        "reversibility": reversibility,
        "order": _shop_order_context(args.get("order_id")),
    }, cfg, cfg["timeout"])
    decision = str(body.get("decision", "")).upper()
    decision_id = str(body.get("decision_id", ""))
    explanation = str(body.get("explanation", "WARDEN decision."))
    receipt = {"decision_id": decision_id, "warden_action": mapped}
    if decision == "ALLOW":
        return {"decision": "ALLOW", "reason": explanation, **receipt}
    if decision in ("BLOCKED", "HUMAN_ONLY"):
        return {"decision": "BLOCK", "reason": explanation, **receipt}
    if decision == "PENDING_APPROVAL":
        final = _poll_warden_decision(cfg, decision_id)
        return {"decision": final["decision"], "reason": final["reason"], **receipt}
    raise ValueError(f"Invalid WARDEN decision: {body!r}")


def _call_warden_generic(action: str, args: dict, context: dict, request_id: str, cfg: dict) -> dict:
    """Legacy generic contract: POST full endpoint, expect ALLOW/BLOCK/REQUIRE_APPROVAL."""
    import urllib.request
    if cfg["simulate_outage"]:
        raise TimeoutError("Simulated WARDEN outage.")
    payload = json.dumps({
        "request_id": request_id,
        "action": action,
        "args": args,
        "customer_id": context.get("customer_id"),
        "order_id": args.get("order_id"),
        "context": {k: v for k, v in context.items() if k != "user_text"},
        "correlation_id": context.get("correlation_id", request_id),
    }).encode()
    req = urllib.request.Request(cfg["endpoint"], data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    if cfg["api_key"]:
        req.add_header("Authorization", f"Bearer {cfg['api_key']}")
    with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
        body = json.loads(r.read().decode() or "{}")
    decision = str(body.get("decision", "")).upper()
    if decision not in ("ALLOW", "BLOCK", "REQUIRE_APPROVAL"):
        raise ValueError(f"Invalid WARDEN decision: {body!r}")
    return {"decision": decision, "reason": str(body.get("reason", "WARDEN decision."))}


def _call_warden(action: str, args: dict, context: dict, request_id: str) -> dict:
    """POST to WARDEN; any failure -> BLOCK (fail closed)."""
    cfg = _warden_config()
    if cfg["base_url"]:
        return _call_warden_native(action, args, context, request_id, cfg)
    return _call_warden_generic(action, args, context, request_id, cfg)


def execute(action: str, args: dict, context: dict | None = None,
            request_id: str | None = None, db_path: str | None = None) -> dict:
    """Main entry: validate -> govern -> execute (if allowed) -> audit.

    Read-only tools bypass governance but are still audit-logged.
    Returns dict with keys: action, governance_decision, executed,
    result, reason, request_id, mode.
    """
    context = dict(context or {})
    request_id = request_id or f"REQ-{uuid.uuid4().hex[:8].upper()}"
    customer_id = args.get("customer_id") or context.get("customer_id", "?")
    mode = gateway_mode()

    fn = getattr(tools, action, None)
    if fn is None or action.startswith("_"):
        out = {"action": action, "governance_decision": "BLOCK", "executed": False,
               "result": None, "reason": f"Unknown tool: {action}",
               "request_id": request_id, "mode": mode}
        _audit(request_id, customer_id, action, args, "", "BLOCK", "failed", out["reason"], db_path)
        return out

    # Enrich context with order owner for ownership gate
    if args.get("order_id"):
        try:
            conn = get_conn(db_path)
            row = conn.execute("SELECT customer_id FROM orders WHERE order_id = ?",
                               (args["order_id"],)).fetchone()
            conn.close()
            if row:
                context.setdefault("order_owner", row["customer_id"])
        except Exception:
            pass

    # Read-only path: no governance, still audit
    if action not in STATE_CHANGING:
        try:
            result = fn(**args)
            ok = bool(result.get("ok", True))
            _audit(request_id, customer_id, action, args,
                   json.dumps(result.get("eligibility", "")), "N/A (read-only)",
                   "completed" if ok else "failed",
                   result.get("reason", result.get("message", "")), db_path)
            return {"action": action, "governance_decision": "N/A (read-only)",
                    "executed": True, "result": result,
                    "reason": "Read-only tool.", "request_id": request_id, "mode": mode}
        except TypeError as e:
            _audit(request_id, customer_id, action, args, "", "N/A (read-only)",
                   "failed", f"Invalid arguments: {e}", db_path)
            return {"action": action, "governance_decision": "N/A (read-only)",
                    "executed": False, "result": {"ok": False, "reason": f"Invalid arguments: {e}"},
                    "reason": f"Invalid arguments: {e}", "request_id": request_id, "mode": mode}

    # State-changing path: governance first
    cfg = _warden_config()
    warden_receipt: dict = {}
    if mode == "WARDEN":
        try:
            gov = _call_warden(action, args, context, request_id)
            warden_receipt = {"decision_id": gov.get("decision_id", ""),
                              "warden_action": gov.get("warden_action", action)}
        except Exception as e:
            gov = {"decision": "BLOCK", "reason": f"WARDEN unavailable — fail closed: {e}"}
    else:
        gov = simulate_decision(action, args, context)

    decision = gov["decision"]
    if decision == "BLOCK":
        _audit(request_id, customer_id, action, args, "", decision, "blocked", gov["reason"], db_path)
        return {"action": action, "governance_decision": decision, "executed": False,
                "result": {"ok": False, "reason": gov["reason"]},
                "reason": gov["reason"], "request_id": request_id, "mode": mode}
    if decision == "REQUIRE_APPROVAL":
        # Record hold; still persist pending state for refunds via backend held path?
        _audit(request_id, customer_id, action, args, "", decision, "held", gov["reason"], db_path)
        # For refunds, mark pending_approval in DB so status is visible
        if action == "initiate_refund" and args.get("order_id"):
            try:
                conn = get_conn(db_path)
                row = conn.execute("SELECT refund_status FROM orders WHERE order_id = ?",
                                   (args["order_id"],)).fetchone()
                if row and row["refund_status"] == "none":
                    conn.execute("UPDATE orders SET refund_status='pending_approval' WHERE order_id=?",
                                 (args["order_id"],))
                    conn.commit()
                conn.close()
            except Exception:
                pass
        return {"action": action, "governance_decision": decision, "executed": False,
                "held_for_approval": True,
                "result": {"ok": True, "held_for_approval": True, "message": gov["reason"]},
                "reason": gov["reason"], "request_id": request_id, "mode": mode}

    # ALLOW -> execute backend
    try:
        result = fn(**args)
    except TypeError as e:
        _audit(request_id, customer_id, action, args, "", decision, "failed",
               f"Invalid arguments: {e}", db_path)
        return {"action": action, "governance_decision": decision, "executed": False,
                "result": {"ok": False, "reason": f"Invalid arguments: {e}"},
                "reason": f"Invalid arguments: {e}", "request_id": request_id, "mode": mode}
    executed = bool(result.get("executed", result.get("ok", False)))
    elig = result.get("eligibility", "")
    if isinstance(elig, dict):
        elig = ("eligible" if elig.get("eligible") else "ineligible") + ": " + elig.get("reason", "")
    status = "completed" if executed else ("held" if result.get("held_for_approval") else "failed")
    _audit(request_id, customer_id, action, args, str(elig), decision, status,
           result.get("reason", result.get("message", "")), db_path)
    if warden_receipt.get("decision_id"):
        _report_warden_execution(cfg, warden_receipt["decision_id"],
                                 warden_receipt.get("warden_action", action),
                                 "SUCCESS" if executed else "FAILED",
                                 result.get("reason", result.get("message", "")))
    return {"action": action, "governance_decision": decision, "executed": executed,
            "result": result, "reason": result.get("reason", result.get("message", "")),
            "request_id": request_id, "mode": mode}
