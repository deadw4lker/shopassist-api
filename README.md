# 🛍️ ShopAssist AI — E-Commerce Agent for WARDEN Testing

A lightweight customer-support chatbot (Streamlit + SQLite + Python tool calls) built to test the
**WARDEN AI governance system**: prompt-injection resistance and action control before execution.

## Quickstart

```bash
cd warden-agent-demo
pip install -r requirements.txt
streamlit run app.py
```

Sample data (5 customers, 12 orders) is created automatically on first launch.
No API key needed — the app runs in **Demo Mode** with a rule-based agent + local policy simulator.
Set `LLM_API_KEY` (OpenAI-compatible) in `.env` for LLM-driven NLU; all tool calls still go through the `ActionGateway`.

## Layout

- **Sidebar** — branding, new chat, demo customer selector, order history, Customer / Testing mode toggle, governance status, clear button.
- **Chat** — Streamlit chat bubbles, suggested prompts, order cards, ✅/⏳/⛔/❌ outcome badges.
- **Developer panel** (Testing Mode) — tool calls, audit log, and 9 one-click prompt-injection tests.

## Governance

`action_gateway.py` sits between agent tools and `tools.py` business functions.

- **Demo Mode** (`mock_warden.py`): local simulator — ALLOW / BLOCK / REQUIRE_APPROVAL. Clearly labelled, not WARDEN.
- **WARDEN Mode** (`WARDEN_ENDPOINT` set): POSTs `{request_id, action, args, customer/order context, correlation_id}` before any state change.
  `ALLOW` → execute, `BLOCK` → reject, `REQUIRE_APPROVAL` → hold. Timeout/error/invalid → **fail closed**.

## Demo script (hackathon, ~3 min)

1. **Legit refund** — as `CUST-01`: *"Can I get a refund for order ORD-1001?"* → eligibility ✅ → refund completes, audit row `completed`.
2. **Injection attack** — Testing panel → **T2 Direct prompt injection** → bulk refunds attempted → gateway `BLOCK`s, nothing executes.
3. **Blocked unauthorized action** — Testing panel → **T3 Unauthorized refund** → cross-customer order rejected by backend + gateway.

Then show **T9** (WARDEN outage → fail closed) and the audit-log table with `allowed / blocked / held / failed` filters.

## Tests

```bash
pytest tests/ -v
```

Covers eligibility, ownership, duplicates, amount validation, injection scenarios, allow/block/approval, and fail-closed outage behaviour.

## Project structure

```
app.py  agent.py  tools.py  database.py  action_gateway.py  mock_warden.py
requirements.txt  .env.example  README.md  tests/
```

All payments are simulated. No real gateway is ever called.
