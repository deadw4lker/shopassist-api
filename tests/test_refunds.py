"""Refund eligibility, ownership, duplicates, amount validation."""
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
    # rebind module-level DB_PATH used via get_conn default
    import database
    monkeypatch.setattr(database, "DB_PATH", path)
    init_db(path)
    yield path
    os.remove(path)


def test_eligible_refund():
    r = tools.check_refund_eligibility("CUST-01", "ORD-1001")
    assert r["eligible"] is True


def test_outside_return_window_ineligible():
    r = tools.check_refund_eligibility("CUST-01", "ORD-1002")
    assert r["eligible"] is False and "window" in r["reason"].lower()


def test_unauthorized_customer_rejected():
    r = tools.check_refund_eligibility("CUST-01", "ORD-1008")  # belongs to CUST-04
    assert r["eligible"] is False and "authorization" in r["reason"].lower()
    d = tools.get_order_details("CUST-01", "ORD-1008")
    assert d["ok"] is False


def test_duplicate_refund_prevented():
    first = tools.initiate_refund("CUST-05", "ORD-1010", reason="t")
    assert first["executed"] is True
    second = tools.initiate_refund("CUST-05", "ORD-1010", reason="t")
    assert second["executed"] is False and "duplicate" in second["reason"].lower()


def test_negative_amount_rejected():
    r = tools.check_refund_eligibility("CUST-05", "ORD-1010", amount=-500)
    assert r["eligible"] is False


def test_excessive_amount_rejected():
    r = tools.check_refund_eligibility("CUST-05", "ORD-1010", amount=999999)
    assert r["eligible"] is False


def test_partial_refund_allowed():
    r = tools.check_refund_eligibility("CUST-04", "ORD-1007", amount=1000)
    assert r["eligible"] is True and r["partial"] is True


def test_high_value_no_local_hold():
    # Local Rs.10,000 hold removed: WARDEN alone decides approvals now.
    r = tools.check_refund_eligibility("CUST-04", "ORD-1008")
    assert r["eligible"] is True and r["needs_approval"] is False


def test_reset_demo_data_restores_fresh_state():
    import database
    first = tools.initiate_refund("CUST-05", "ORD-1010", reason="t")
    assert first["executed"] is True
    database.reset_demo_data()
    r = tools.check_refund_eligibility("CUST-05", "ORD-1010")
    assert r["eligible"] is True


def test_cancel_unshipped_ok_cancelled_twice_fails():
    ok = tools.cancel_order("CUST-02", "ORD-1003")
    assert ok["executed"] is True
    again = tools.cancel_order("CUST-02", "ORD-1003")
    assert again["executed"] is False


def test_cancel_delivered_fails():
    r = tools.cancel_order("CUST-01", "ORD-1001")
    assert r["executed"] is False
