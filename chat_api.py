"""ShopAssist Chat API: phone-app facing HTTP layer over the agent.

Exposes the exact same brain as the Streamlit chatbot (agent.run_turn:
abuse_guard -> LLM/rule planner -> action_gateway -> WARDEN governance)
as a JSON API so native apps (Android/iOS/any client) can chat with it.

  POST /api/chat  {customer_id, message, history?} -> {reply, badges, blocked}
  GET  /health    -> {status}

Auth: optional shared bearer token via APP_API_KEY env (empty = open, fine
for LAN demos; SET IT before exposing publicly). The WARDEN_API_KEY stays
server-side in .env and is never exposed to phones.

Run:
  cd /home/deadw4lker/agento
  python3 -m uvicorn chat_api:app --host 0.0.0.0 --port 8502
"""
from __future__ import annotations

import os
import secrets

import database  # noqa: F401  (loads .env into os.environ on import)
import agent
from action_gateway import gateway_mode, check_warden_connection

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional


class ChatMsg(BaseModel):
    role: str = "user"
    content: str = ""


class ChatRequest(BaseModel):
    customer_id: str = "CUST-01"
    message: str = ""
    history: Optional[List[ChatMsg]] = None


app = FastAPI(title="ShopAssist Chat API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _check_app_key(authorization: Optional[str]) -> None:
    expected = os.environ.get("APP_API_KEY", "").strip()
    if not expected:
        return  # open mode (LAN demo)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing app token")
    if not secrets.compare_digest(authorization[7:], expected):
        raise HTTPException(status_code=401, detail="Invalid app token")


def _badges(trace: list) -> List[str]:
    out = []
    for o in trace or []:
        gov = o.get("governance_decision", "")
        action = o.get("action", "")
        if gov == "ALLOW" and o.get("executed"):
            out.append(f"✅ {action}")
        elif gov == "REQUIRE_APPROVAL" or o.get("held_for_approval"):
            out.append(f"⏳ {action} held for approval")
        elif gov == "BLOCK":
            out.append(f"⛔ {action} blocked")
        elif gov and gov != "N/A (read-only)":
            out.append(f"❌ {action}: {gov}")
    return out


@app.get("/health")
def health():
    conn = check_warden_connection()
    return {
        "status": "ok",
        "warden_mode": gateway_mode(),
        "warden_connected": conn.get("connected", False),
        "warden_detail": conn.get("reason", ""),
    }


@app.get("/")
def root():
    return {
        "service": "ShopAssist Chat API",
        "chat": "POST /api/chat {customer_id, message, history?}",
        "health": "GET /health",
    }


@app.post("/api/chat")
def chat(req: ChatRequest, authorization: Optional[str] = Header(default=None)):
    _check_app_key(authorization)
    text = (req.message or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="message is required")
    customer_id = (req.customer_id or "CUST-01").strip() or "CUST-01"
    hist = [{"role": m.role, "content": m.content} for m in (req.history or [])][-10:]

    result = agent.run_turn(customer_id, text, hist)
    return {
        "reply": result.get("reply", ""),
        "badges": _badges(result.get("trace", [])),
        "blocked_user": bool(result.get("blocked_user", False)),
        "block_reason": result.get("block_reason", ""),
        "actions_attempted": result.get("actions_attempted", 0),
        "governance_blocked": result.get("blocked", 0),
        "mode": gateway_mode(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("chat_api:app", host="0.0.0.0", port=8502)
