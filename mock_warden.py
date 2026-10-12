"""Local policy simulator used in Demo Mode.

Clearly labelled: this is NOT WARDEN. It mimics allow/block/hold
decisions so the demo works without a governance backend.
"""
from __future__ import annotations

import re

from tools import APPROVAL_THRESHOLD, MAX_REFUND_AMOUNT

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"system\s+override",
    r"refund\s+every\s+order",
    r"ignore\s+the\s+return\s+policy",
    r"override\s+(the\s+)?policy",
    r"approve\s+my\s+refund\s+(without|regardless)",
    r"do\s+not\s+ask",
]


def _has_injection(text: str) -> bool:
    t = (text or "").lower()
    return any(re.search(p, t) for p in INJECTION_PATTERNS)


def simulate_decision(action: str, args: dict, context: dict) -> dict:
    """Return {decision: ALLOW|BLOCK|REQUIRE_APPROVAL, reason}."""
    reason_text = str(args.get("reason", "")) + " " + str(context.get("user_text", ""))

    # 1. Malformed / invalid arguments -> BLOCK
    amt = args.get("amount", None)
    if amt is not None:
        try:
            amt_f = float(amt)
            if amt_f <= 0:
                return {"decision": "BLOCK", "reason": "Invalid amount: must be positive."}
            if amt_f > MAX_REFUND_AMOUNT:
                return {"decision": "BLOCK", "reason": f"Amount exceeds maximum refund limit Rs.{MAX_REFUND_AMOUNT:,.0f}."}
        except (TypeError, ValueError):
            return {"decision": "BLOCK", "reason": "Invalid amount: not a number."}
    if action in ("initiate_refund", "cancel_order", "request_return",
                  "request_replacement", "get_order_details", "track_shipment",
                  "get_refund_status", "check_refund_eligibility"):
        if not args.get("order_id"):
            # cancel/return/replace/refund need order; read tools too
            if action != "escalate_to_human":
                return {"decision": "BLOCK", "reason": "Missing order_id."}

    # 2. Ownership check at gateway layer (defence in depth)
    order_owner = context.get("order_owner")
    actor = context.get("customer_id")
    if order_owner and actor and order_owner != actor:
        return {"decision": "BLOCK",
                "reason": f"Cross-customer action blocked: {actor} cannot act on {order_owner}'s order."}

    # 3. Bulk / injection-driven destructive scope -> BLOCK
    if _has_injection(reason_text) or context.get("bulk_action"):
        if action in ("initiate_refund", "cancel_order"):
            return {"decision": "BLOCK",
                    "reason": "Blocked: instruction-override / bulk-action pattern detected. Governance does not allow policy override via chat."}

    # 4. High-value hold
    if action == "initiate_refund" and amt is not None:
        try:
            if float(amt) > APPROVAL_THRESHOLD:
                return {"decision": "REQUIRE_APPROVAL",
                        "reason": f"Amount Rs.{float(amt):,.0f} exceeds approval threshold Rs.{APPROVAL_THRESHOLD:,.0f}; held for human review."}
        except (TypeError, ValueError):
            pass

    return {"decision": "ALLOW", "reason": "Local demo policy: checks passed."}
