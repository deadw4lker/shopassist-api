"""Mock e-commerce backend: pure Python functions over SQLite.

All business rules live here — never in the LLM prompt alone.
Every function enforces customer-ownership checks.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from database import get_conn

MAX_REFUND_AMOUNT = float(os.environ.get("MAX_REFUND_AMOUNT", "100000"))
# Local approval hold is OFF by default (threshold above any realistic order):
# WARDEN is the sole approver now. Lower via env to re-enable a local hold.
APPROVAL_THRESHOLD = float(os.environ.get("APPROVAL_THRESHOLD", "10000000"))
RETURN_WINDOW_DAYS = int(os.environ.get("RETURN_WINDOW_DAYS", "30"))
# Demo convenience: any well-formed but unknown order ID is auto-created as an
# eligible demo order for the requesting customer, so "refund ORD-9999" just works.
AUTO_CREATE_ORDERS = os.environ.get("DEMO_AUTO_CREATE_ORDERS", "1") == "1"
ORDER_ID_RE = re.compile(r"^ORD-\d{4}$", re.IGNORECASE)


def _row_to_dict(row) -> dict:
    return dict(row) if row is not None else {}


def _create_demo_order(conn, customer_id: str, order_id: str) -> None:
    """Insert a plausible eligible order so demos never hit 'Order not found'."""
    today = date.today()
    conn.execute(
        """INSERT OR IGNORE INTO orders
           (order_id, customer_id, product, amount, payment_status,
            delivery_status, order_date, return_deadline, refund_status, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_id, customer_id, f"Demo Product {order_id}", 4999.0, "paid",
         "delivered", (today - timedelta(days=10)).isoformat(),
         (today + timedelta(days=20)).isoformat(), "none",
         "Demo order auto-created for testing."),
    )
    conn.commit()


def _fetch_order(conn, order_id: str, customer_id: str | None = None) -> dict:
    row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()
    if row is None and customer_id and AUTO_CREATE_ORDERS and ORDER_ID_RE.match(order_id or ""):
        _create_demo_order(conn, customer_id, order_id.upper())
        row = conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id.upper(),)).fetchone()
    return _row_to_dict(row)


def _check_ownership(order: dict, customer_id: str) -> dict | None:
    if not order:
        return {"eligible": False, "ok": False, "reason": "Order not found."}
    if order["customer_id"] != customer_id:
        return {"eligible": False, "ok": False,
                "reason": f"Authorization failed: order {order['order_id']} belongs to another customer."}
    return None


# ---------------- READ-ONLY TOOLS ----------------

def search_orders(customer_id: str) -> dict:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM orders WHERE customer_id = ? ORDER BY order_date DESC", (customer_id,)
    ).fetchall()
    conn.close()
    orders = [_row_to_dict(r) for r in rows]
    return {"ok": True, "customer_id": customer_id, "orders": orders, "count": len(orders)}


def get_order_details(customer_id: str, order_id: str) -> dict:
    conn = get_conn()
    order = _fetch_order(conn, order_id, customer_id)
    conn.close()
    err = _check_ownership(order, customer_id)
    if err:
        return {"ok": False, **err}
    return {"ok": True, "order": order}


def track_shipment(customer_id: str, order_id: str) -> dict:
    conn = get_conn()
    order = _fetch_order(conn, order_id, customer_id)
    conn.close()
    err = _check_ownership(order, customer_id)
    if err:
        return {"ok": False, **err}
    status = order["delivery_status"]
    tracking = {
        "processing": "Order is being packed at the warehouse. Expected dispatch in 1-2 days.",
        "shipped": "Order is in transit with Delhivery. Expected delivery in 2-3 days.",
        "delivered": f"Order was delivered. Delivery status: {status}.",
        "cancelled": "Order was cancelled and will not be shipped.",
    }
    return {"ok": True, "order_id": order_id, "delivery_status": status,
            "tracking_info": tracking.get(status, status), "order": order}


def get_refund_status(customer_id: str, order_id: str) -> dict:
    conn = get_conn()
    order = _fetch_order(conn, order_id, customer_id)
    conn.close()
    err = _check_ownership(order, customer_id)
    if err:
        return {"ok": False, **err}
    return {"ok": True, "order_id": order_id,
            "refund_status": order.get("refund_status", "none"),
            "amount": order["amount"]}


# ---------------- VALIDATION ----------------

def check_refund_eligibility(customer_id: str, order_id: str, amount: float | None = None) -> dict:
    """Authoritative policy check. Returns {eligible, reason, ...}."""
    conn = get_conn()
    order = _fetch_order(conn, order_id, customer_id)
    conn.close()
    err = _check_ownership(order, customer_id)
    if err:
        return {"eligible": False, "reason": err["reason"]}

    try:
        amt = float(order["amount"]) if amount is None else float(amount)
    except (TypeError, ValueError):
        return {"eligible": False, "reason": "Invalid refund amount."}

    if amt <= 0:
        return {"eligible": False, "reason": "Refund amount must be positive."}
    if amt > MAX_REFUND_AMOUNT:
        return {"eligible": False, "reason": f"Amount Rs.{amt:,.0f} exceeds maximum refund limit Rs.{MAX_REFUND_AMOUNT:,.0f}."}
    if amt > float(order["amount"]) + 1e-6:
        return {"eligible": False, "reason": f"Amount Rs.{amt:,.0f} exceeds order value Rs.{float(order['amount']):,.0f}."}
    if order["payment_status"] != "paid":
        return {"eligible": False, "reason": f"Payment status is '{order['payment_status']}'. Only paid orders can be refunded."}
    if order["delivery_status"] == "cancelled":
        return {"eligible": False, "reason": "Order is cancelled. No refund applicable (payment was never captured)."}
    if order.get("refund_status") in ("refunded", "processing", "pending_approval"):
        return {"eligible": False, "reason": f"Duplicate prevented: refund already '{order['refund_status']}' for {order_id}."}
    try:
        dl = date.fromisoformat(order["return_deadline"])
        if dl < date.today():
            return {"eligible": False, "reason": f"Return window expired on {order['return_deadline']}. Policy ({RETURN_WINDOW_DAYS} days) cannot be overridden by the assistant."}
    except ValueError:
        return {"eligible": False, "reason": "Invalid return deadline in record."}

    needs_approval = amt > APPROVAL_THRESHOLD
    partial = amt < float(order["amount"]) - 1e-6
    return {"eligible": True,
            "reason": f"Eligible for {'partial' if partial else 'full'} refund of Rs.{amt:,.0f}."
                      + (" High-value: requires approval." if needs_approval else ""),
            "needs_approval": needs_approval, "partial": partial,
            "order_amount": float(order["amount"]), "amount": amt}


# ---------------- STATE-CHANGING TOOLS (gated by ActionGateway) ----------------

def initiate_refund(customer_id: str, order_id: str, amount: float | None = None, reason: str = "") -> dict:
    elig = check_refund_eligibility(customer_id, order_id, amount)
    if not elig["eligible"]:
        return {"ok": False, "executed": False, "eligibility": elig,
                "reason": elig["reason"]}
    amt = elig["amount"]
    conn = get_conn()
    try:
        if elig.get("needs_approval"):
            conn.execute("UPDATE orders SET refund_status = 'pending_approval' WHERE order_id = ?", (order_id,))
            conn.commit()
            return {"ok": True, "executed": False, "held_for_approval": True,
                    "eligibility": elig,
                    "message": f"Refund of Rs.{amt:,.0f} for {order_id} is HELD for human approval (exceeds Rs.{APPROVAL_THRESHOLD:,.0f} threshold)."}
        conn.execute("UPDATE orders SET refund_status = 'refunded' WHERE order_id = ?", (order_id,))
        conn.commit()
        return {"ok": True, "executed": True, "eligibility": elig,
                "message": f"Refund of Rs.{amt:,.0f} for {order_id} completed (simulated). Reason: {reason or 'customer request'}."}
    finally:
        conn.close()


def cancel_order(customer_id: str, order_id: str) -> dict:
    conn = get_conn()
    order = _fetch_order(conn, order_id, customer_id)
    err = _check_ownership(order, customer_id)
    if err:
        conn.close()
        return {"ok": False, "executed": False, "reason": err["reason"]}
    if order["delivery_status"] == "cancelled":
        conn.close()
        return {"ok": False, "executed": False, "reason": f"{order_id} is already cancelled."}
    if order["delivery_status"] not in ("processing",):
        conn.close()
        return {"ok": False, "executed": False,
                "reason": f"{order_id} cannot be cancelled (status: {order['delivery_status']}). Only unshipped orders can be cancelled."}
    conn.execute("UPDATE orders SET delivery_status = 'cancelled' WHERE order_id = ?", (order_id,))
    conn.commit()
    conn.close()
    return {"ok": True, "executed": True, "message": f"{order_id} cancelled successfully. No charge captured."}


def request_return(customer_id: str, order_id: str, reason: str = "") -> dict:
    conn = get_conn()
    order = _fetch_order(conn, order_id, customer_id)
    err = _check_ownership(order, customer_id)
    if err:
        conn.close()
        return {"ok": False, "executed": False, "reason": err["reason"]}
    if order["delivery_status"] != "delivered":
        conn.close()
        return {"ok": False, "executed": False,
                "reason": f"Return requires delivered status (current: {order['delivery_status']})."}
    try:
        if date.fromisoformat(order["return_deadline"]) < date.today():
            conn.close()
            return {"ok": False, "executed": False,
                    "reason": f"Return window expired on {order['return_deadline']}."}
    except ValueError:
        conn.close()
        return {"ok": False, "executed": False, "reason": "Invalid return deadline."}
    conn.close()
    return {"ok": True, "executed": True,
            "message": f"Return pickup scheduled for {order_id}. Reason: {reason or 'not specified'}. Refund issued after quality check (simulated)."}


def request_replacement(customer_id: str, order_id: str, reason: str = "") -> dict:
    conn = get_conn()
    order = _fetch_order(conn, order_id, customer_id)
    err = _check_ownership(order, customer_id)
    if err:
        conn.close()
        return {"ok": False, "executed": False, "reason": err["reason"]}
    if order["delivery_status"] != "delivered":
        conn.close()
        return {"ok": False, "executed": False,
                "reason": f"Replacement requires delivered status (current: {order['delivery_status']})."}
    conn.close()
    return {"ok": True, "executed": True,
            "message": f"Replacement approved for {order_id} ({order['product']}). Reason: {reason or 'not specified'}. New unit ships in 2-3 days (simulated)."}


def escalate_to_human(customer_id: str, reason: str = "") -> dict:
    return {"ok": True, "executed": True,
            "message": f"Escalated to human support for {customer_id}. Reason: {reason or 'customer request'}. Ticket raised (simulated)."}


def request_human_review(customer_id: str, order_id: str = "", reason: str = "") -> dict:
    """Open a human-review ticket on a specific order (governed action)."""
    if not order_id:
        return {"ok": False, "executed": False,
                "reason": "Please share the order ID you want reviewed (e.g. ORD-1001)."}
    conn = get_conn()
    order = _fetch_order(conn, order_id, customer_id)
    err = _check_ownership(order, customer_id)
    if err:
        conn.close()
        return {"ok": False, "executed": False, "reason": err["reason"]}
    existing = conn.execute(
        """SELECT ticket_id FROM review_requests
           WHERE customer_id = ? AND order_id = ? AND status = 'open'
           ORDER BY created_at DESC LIMIT 1""",
        (customer_id, order["order_id"])).fetchone()
    if existing:
        conn.close()
        return {"ok": False, "executed": False,
                "reason": f"Review already open for {order['order_id']} (ticket {existing['ticket_id']})."}
    ticket_id = f"REV-{uuid.uuid4().hex[:8].upper()}"
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO review_requests
           (ticket_id, customer_id, order_id, reason, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'open', ?, ?)""",
        (ticket_id, customer_id, order["order_id"], reason or "customer request", now, now))
    conn.commit()
    conn.close()
    return {"ok": True, "executed": True, "ticket_id": ticket_id,
            "message": f"Order {order['order_id']} sent for human review (ticket {ticket_id}). "
                       f"Our team will look into it shortly."}


def get_human_review_status(customer_id: str, order_id: str = "") -> dict:
    """Read-only: latest human-review ticket status for an order."""
    if not order_id:
        return {"ok": False, "reason": "Please share the order ID (e.g. ORD-1001)."}
    conn = get_conn()
    order = _fetch_order(conn, order_id, customer_id)
    err = _check_ownership(order, customer_id)
    if err:
        conn.close()
        return {"ok": False, **err}
    row = conn.execute(
        """SELECT ticket_id, status, reason, created_at FROM review_requests
           WHERE customer_id = ? AND order_id = ?
           ORDER BY created_at DESC LIMIT 1""",
        (customer_id, order["order_id"])).fetchone()
    conn.close()
    if not row:
        return {"ok": True, "order_id": order["order_id"], "status": "none",
                "message": f"No human review requested for {order['order_id']} yet."}
    return {"ok": True, "order_id": order["order_id"], "ticket_id": row["ticket_id"],
            "status": row["status"],
            "message": f"Review {row['ticket_id']} for {order['order_id']}: **{row['status']}**."}


# Tool registry consumed by the agent + UI
READ_TOOLS = {"search_orders", "get_order_details", "check_refund_eligibility",
              "track_shipment", "get_refund_status"}
STATE_CHANGING_TOOLS = {"initiate_refund", "cancel_order", "request_return",
                        "request_replacement", "escalate_to_human"}

TOOL_SCHEMAS = {
    "search_orders": {"customer_id": "str"},
    "get_order_details": {"customer_id": "str", "order_id": "str"},
    "check_refund_eligibility": {"customer_id": "str", "order_id": "str", "amount": "float|optional"},
    "initiate_refund": {"customer_id": "str", "order_id": "str", "amount": "float|optional", "reason": "str|optional"},
    "cancel_order": {"customer_id": "str", "order_id": "str"},
    "request_return": {"customer_id": "str", "order_id": "str", "reason": "str|optional"},
    "request_replacement": {"customer_id": "str", "order_id": "str", "reason": "str|optional"},
    "track_shipment": {"customer_id": "str", "order_id": "str"},
    "get_refund_status": {"customer_id": "str", "order_id": "str"},
    "escalate_to_human": {"customer_id": "str", "reason": "str|optional"},
    "request_human_review": {"customer_id": "str", "order_id": "str", "reason": "str|optional"},
    "get_human_review_status": {"customer_id": "str", "order_id": "str"},
}
