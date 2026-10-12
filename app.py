"""ShopAssist — pure black & white chatbot.

Chat only: no sidebar, no panels, no colour. The full pipeline
(agent -> ActionGateway -> business tools -> SQLite audit log)
keeps running invisibly in the background.
"""
from __future__ import annotations

import html
import os

import streamlit as st

import agent
from database import init_db, reset_demo_data

st.set_page_config(page_title="ShopAssist", page_icon="⬛", layout="centered")

st.markdown("""
<style>
  /* app chrome off — just the chatbot */
  header[data-testid="stHeader"] { visibility: hidden; }
  div[data-testid="stToolbar"] { visibility: hidden; }
  footer { visibility: hidden; }

  .stApp { background: #FFFFFF; }
  section.main .block-container { max-width: 720px; padding-top: 1.5rem; padding-bottom: 6rem; }
  h1, h2, h3, p, span, div, li, label { color: #000000; }

  /* top bar */
  .topbar { display: flex; align-items: center; justify-content: space-between;
            border-bottom: 1px solid #E5E5E5; padding: 10px 2px 14px; margin-bottom: 6px; }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand-mark { width: 32px; height: 32px; border-radius: 8px; background: #000000;
                color: #FFFFFF; font-weight: 800; font-size: 17px;
                display: flex; align-items: center; justify-content: center; }
  .brand-name { font-size: 1.05rem; font-weight: 800; letter-spacing: -0.01em; }
  .brand-sub { font-size: 0.72rem; color: #6B7280; }

  /* buttons: black / white only */
  .stButton > button { background: #FFFFFF; color: #000000; border: 1px solid #000000;
                       border-radius: 999px; font-weight: 600; font-size: 0.85rem;
                       padding: 0.45rem 1rem; transition: all .15s ease; }
  .stButton > button:hover { background: #000000; color: #FFFFFF; border-color: #000000; }
  .stButton > button:focus { outline: none; box-shadow: none; }

  /* bottom message bar: pill input + round black send button */
  div[data-testid="stForm"] { position: sticky; bottom: 0; background: #FFFFFF;
                              padding: 12px 0 6px; z-index: 10; border: 0; }
  div[data-testid="stTextInput"] input { border-radius: 999px !important; border: 1px solid #000000 !important;
                                         background-color: #FFFFFF !important; color: #000000 !important;
                                         -webkit-text-fill-color: #000000 !important; caret-color: #000000 !important;
                                         padding: 12px 20px !important; font-size: 0.95rem !important; }
  div[data-testid="stTextInput"] input::placeholder { color: #9CA3AF !important;
                                                     -webkit-text-fill-color: #9CA3AF !important; }
  div[data-testid="stTextInput"] input:focus { box-shadow: none !important; border-width: 2px !important; }
  div[data-testid="stFormSubmitButton"] > button { border-radius: 50% !important; width: 48px !important;
      height: 48px !important; min-height: 48px !important; padding: 0 !important;
      background: #000000 !important; color: #FFFFFF !important; border: 1px solid #000000 !important;
      font-size: 1.3rem !important; font-weight: 800 !important; line-height: 1 !important; }
  div[data-testid="stFormSubmitButton"] > button:hover { background: #333333 !important; color: #FFFFFF !important; }
  div[data-testid="stFormSubmitButton"] > button:focus { outline: none !important; box-shadow: none !important; }

  /* user bubble: black */
  .user-row { display: flex; justify-content: flex-end; margin: 12px 0; }
  .user-bubble { background: #000000; color: #FFFFFF; border-radius: 16px 16px 4px 16px;
                 padding: 10px 16px; max-width: 85%; font-size: 0.95rem; line-height: 1.5;
                 white-space: pre-wrap; word-wrap: break-word; }
  .user-bubble * { color: #FFFFFF; }

  /* assistant card: white, grey border */
  .asst { display: flex; gap: 10px; margin: 12px 0; }
  .asst-mark { flex-shrink: 0; width: 30px; height: 30px; border-radius: 50%;
               background: #000000; color: #FFFFFF; font-weight: 800; font-size: 14px;
               display: flex; align-items: center; justify-content: center; margin-top: 2px; }
  .asst-body { background: #FFFFFF; border: 1px solid #E5E5E5; border-radius: 4px 16px 16px 16px;
               padding: 12px 16px; max-width: 92%; flex-grow: 1; }
  .asst-body p, .asst-body li { font-size: 0.93rem; line-height: 1.6; color: #000000; }

  /* monochrome outcome banners: grey fill, thick black edge, bold label */
  .note { background: #F5F5F5; border: 1px solid #E5E5E5; border-left: 4px solid #000000;
          border-radius: 8px; padding: 10px 14px; margin: 8px 0; }
  .note.plain { background: #FFFFFF; border-left-width: 1px; }
  .tag { display: inline-block; background: #000000; color: #FFFFFF; font-size: 0.68rem;
         font-weight: 800; letter-spacing: 0.08em; border-radius: 4px;
         padding: 2px 8px; margin-bottom: 6px; }
  .note p, .note li { color: #000000; margin: 0.3em 0; }

  .who { font-size: 0.75rem; color: #6B7280; text-align: center; margin-bottom: 4px; }
</style>
""", unsafe_allow_html=True)

init_db()

_BOOT_FILE = "/tmp/shopassist_boot.pid"


def _fresh_process_boot() -> bool:
    """True only on server (re)start — same PID means a Streamlit rerun."""
    try:
        pid = str(os.getpid())
        if os.path.exists(_BOOT_FILE):
            with open(_BOOT_FILE) as f:
                if f.read().strip() == pid:
                    return False
        with open(_BOOT_FILE, "w") as f:
            f.write(pid)
        return True
    except OSError:
        return False


if _fresh_process_boot():
    reset_demo_data()

if "messages" not in st.session_state:
    st.session_state.messages = []
if "customer_id" not in st.session_state:
    st.session_state.customer_id = "CUST-01"

CUSTOMERS = {
    "CUST-01": "Priya Sharma",
    "CUST-02": "Rahul Verma",
    "CUST-03": "Ananya Iyer",
    "CUST-04": "Vikram Mehta",
    "CUST-05": "Sneha Patel",
}

# emoji marker -> monochrome verdict label (colour emojis never reach the screen)
LABELS = [("⛔", "NOT APPROVED"), ("⏳", "ON HOLD"), ("✅", "APPROVED"),
          ("❌", "FAILED"), ("🔒", "NOTE"), ("⚠️", "NOTE"), ("📦", "TRACKING")]


def render_assistant(content: str) -> None:
    st.markdown('<div class="asst"><div class="asst-mark">S</div><div class="asst-body">',
                unsafe_allow_html=True)
    for block in [b for b in content.split("\n\n") if b.strip()]:
        s = block.strip()
        label = next((lab for emo, lab in LABELS if s.startswith(emo)), None)
        if label:
            body = s[1:].strip()  # drop the emoji marker
            st.markdown(f'<div class="note"><span class="tag">{label}</span>',
                        unsafe_allow_html=True)
            st.markdown(body)
            st.markdown("</div>", unsafe_allow_html=True)
        elif s.startswith("**ORD") or s.startswith("•") or s.startswith("Your orders"):
            st.markdown('<div class="note plain">', unsafe_allow_html=True)
            st.markdown(block)
            st.markdown("</div>", unsafe_allow_html=True)
        else:
            st.markdown(block)
    st.markdown("</div></div>", unsafe_allow_html=True)


def render_user(text: str) -> None:
    st.markdown(f'<div class="user-row"><div class="user-bubble">{html.escape(text)}</div></div>',
                unsafe_allow_html=True)


# ---------- top bar: brand + customer only ----------
left, right = st.columns([1, 1])
with left:
    st.markdown('<div class="brand"><div class="brand-mark">S</div>'
                '<div><div class="brand-name">ShopAssist</div>'
                '<div class="brand-sub">Customer support</div></div></div>',
                unsafe_allow_html=True)
with right:
    st.selectbox("Customer", list(CUSTOMERS.keys()),
                 format_func=lambda c: f"{CUSTOMERS[c]}",
                 index=list(CUSTOMERS.keys()).index(st.session_state.customer_id),
                 key="customer_select", label_visibility="collapsed")
    if st.session_state.customer_select != st.session_state.customer_id:
        st.session_state.customer_id = st.session_state.customer_select
        st.session_state.messages = []
        st.rerun()

st.markdown(f'<div class="who">Chatting as {CUSTOMERS[st.session_state.customer_id]} '
            f'({st.session_state.customer_id})</div>', unsafe_allow_html=True)

# ---------- chat only: free conversation, no preset options ----------
if not st.session_state.messages:
    render_assistant("Hi! I'm **ShopAssist**. Just tell me what you need — a refund, "
                     "an order replacement, cancellation, return, or tracking — "
                     "and I'll check it and get it **approved** right here in the chat.")

for m in st.session_state.messages:
    if m["role"] == "user":
        render_user(m["content"])
    else:
        render_assistant(m["content"])


def handle_user_text(text: str) -> None:
    st.session_state.messages.append({"role": "user", "content": text})
    render_user(text)
    with st.spinner("Checking…"):
        hist = [{"role": m["role"], "content": m["content"]}
                for m in st.session_state.messages[:-1]]
        result = agent.run_turn(st.session_state.customer_id, text, hist)
    render_assistant(result["reply"])
    st.session_state.messages.append({"role": "assistant", "content": result["reply"]})
    st.rerun()


with st.form("chat_form", clear_on_submit=True, border=False):
    in_col, send_col = st.columns([7, 1])
    with in_col:
        draft = st.text_input("Message", placeholder="Type your message…",
                              label_visibility="collapsed", key="chat_box")
    with send_col:
        send = st.form_submit_button("↑")
if send and draft.strip():
    handle_user_text(draft.strip())
