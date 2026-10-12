"""Human-review ticket tool: ownership, duplicates, status, governance."""
import os
import tempfile

import pytest

import tools
from database import init_db


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setenv("SHOPASSIST_DB", path)
    import database
    monkeypatch.setattr(database, "DB_PATH", path)
    init_db(path)
    yield path
    os.remove(path)


def test_request_creates_ticket():
    r = tools.request_human_review("CUST-01", "ORD-1001", reason="overcharged?")
    assert r["executed"] is True and r["ticket_id"].startswith("REV-")


def test_duplicate_open_prevented():
    tools.request_human_review("CUST-01", "ORD-1001", reason="t")
    again = tools.request_human_review("CUST-01", "ORD-1001", reason="t")
    assert again["executed"] is False and "already open" in again["reason"]


def test_unauthorized_rejected():
    r = tools.request_human_review("CUST-01", "ORD-1008", reason="t")
    assert r["executed"] is False and "authorization" in r["reason"].lower()


def test_missing_order_id_rejected():
    r = tools.request_human_review("CUST-01", "", reason="t")
    assert r["executed"] is False


def test_status_flow():
    before = tools.get_human_review_status("CUST-05", "ORD-1010")
    assert before["ok"] is True and before["status"] == "none"
    made = tools.request_human_review("CUST-05", "ORD-1010", reason="late delivery")
    after = tools.get_human_review_status("CUST-05", "ORD-1010")
    assert after["status"] == "open" and after["ticket_id"] == made["ticket_id"]


def test_gateway_governs_review_request():
    import action_gateway
    out = action_gateway.execute(
        "request_human_review",
        {"customer_id": "CUST-02", "order_id": "ORD-1003", "reason": "demo"},
        context={"customer_id": "CUST-02", "user_text": "review my order"})
    assert out["executed"] is True


def test_agent_end_to_end_review_turn():
    import agent
    r = agent.run_turn("CUST-01", "Please send ORD-1001 for human review.", [])
    assert any(o.get("executed") and o.get("action") == "request_human_review"
               for o in r["trace"])
    r2 = agent.run_turn("CUST-01", "Check my review status for ORD-1001.", [])
    assert "open" in r2["reply"].lower()
