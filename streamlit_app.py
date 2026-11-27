"""HR Policy Navigator — Light-Slate Editorial SaaS UI (Sprint 3).

Preserves all Sprint 1-3 & Evaluation capabilities in pure Python + Streamlit:
  - Multi-persona auth + one-click Demo Account login
  - Role & tenure personalization, persistent chat history (last 3 + rename)
  - On-demand full policy section reader, verified claims inspector, offline Voice/TTS
  - RLHF feedback loop, HITL Human Review Queue state machine
  - SysAdmin Evaluation Runner (Week 1-4) & Bird's Eye View
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st

from hr_policy_navigator.agent import Copilot
from hr_policy_navigator.auth import UserProfile, authenticate, demo_users
from hr_policy_navigator.chat_history import ChatHistory
from hr_policy_navigator.config import Settings
from hr_policy_navigator.feedback import FeedbackStore
from hr_policy_navigator.retrieval import PolicyCorpus
from hr_policy_navigator.serving import RateLimiter, ResponseCache
from hr_policy_navigator.state import HandoffStore, VALID_TRANSITIONS
from hr_policy_navigator.tracing import load_traces, summarize
from hr_policy_navigator.voice import stt_available, text_to_speech, tts_available

S = Settings()

# ---------------------------------------------------------------------------
# Role metadata & inline SVG assets (zero emojis, zero external URLs)
# ---------------------------------------------------------------------------
_ROLE_META: Dict[str, Dict[str, Any]] = {
    "sys_admin": {
        "code": "SA",
        "label": "System Administrator",
        "accent": "#334155",
        "bg": "#F1F5F9",
        "hint": "Unrestricted superuser access · All tabs, traces, feedback, HITL queue & evaluations",
        "sample_queries": [
            "What is the full termination policy with all escalation steps?",
            "How many PTO days do I get?",
            "Can I fire John Doe?",
        ],
    },
    "hr_ops": {
        "code": "HR",
        "label": "HR Operations",
        "accent": "#0F172A",
        "bg": "#F1F5F9",
        "hint": "Full policy verbatim · Global traces & feedback · Complete HITL queue",
        "sample_queries": [
            "What is the full termination policy with all escalation steps?",
            "How do I process a parental leave request?",
            "Can I fire John Doe?",
        ],
    },
    "manager": {
        "code": "PM",
        "label": "People Manager",
        "accent": "#1E3A8A",
        "bg": "#EFF6FF",
        "hint": "Team-scoped HITL queue · Manager-layer policy context",
        "sample_queries": [
            "How do I approve a leave request from my team?",
            "What is my responsibility in a disciplinary process?",
            "How is a performance improvement plan initiated?",
        ],
    },
    "associate": {
        "code": "AS",
        "label": "Associate",
        "accent": "#14532D",
        "bg": "#F0FDF4",
        "hint": "Policy Q&A with personalized answer depth based on your tenure",
        "sample_queries": [
            "How many PTO days do I get?",
            "What is the remote work policy?",
            "How do I request parental leave?",
        ],
    },
}

_BRAND_SVG = """
<svg width="34" height="34" viewBox="0 0 36 36" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <rect width="36" height="36" rx="9" fill="#0F172A"/>
  <path d="M11 10.5C11 9.67157 11.6716 9 12.5 9H20.5L25 13.5V25.5C25 26.3284 24.3284 27 23.5 27H12.5C11.6716 27 11 26.3284 11 25.5V10.5Z" stroke="#F8FAFC" stroke-width="1.75" stroke-linejoin="round"/>
  <path d="M20 9V14H25" stroke="#94A3B8" stroke-width="1.75" stroke-linejoin="round"/>
  <path d="M14.5 18H21.5M14.5 22H19" stroke="#F8FAFC" stroke-width="1.75" stroke-linecap="round"/>
</svg>
""".strip()

_EMPTY_STATE_SVG = """
<svg width="52" height="52" viewBox="0 0 56 56" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <rect width="56" height="56" rx="12" fill="#F1F5F9" stroke="#CBD5E1" stroke-width="1.2"/>
  <path d="M18 19C18 17.8954 18.8954 17 20 17H32L38 23V37C38 38.1046 37.1046 39 36 39H20C18.8954 39 18 38.1046 18 37V19Z" fill="#FFFFFF" stroke="#0F172A" stroke-width="1.6" stroke-linejoin="round"/>
  <path d="M31 17V23H37" stroke="#64748B" stroke-width="1.6" stroke-linejoin="round"/>
  <path d="M23 28H33M23 33H29" stroke="#0F172A" stroke-width="1.6" stroke-linecap="round"/>
</svg>
""".strip()


def _role_monogram_svg(code: str, accent: str = "#0F172A", bg: str = "#F1F5F9") -> str:
    safe_code = _html.escape(code[:2].upper())
    return (
        f'<svg width="32" height="32" viewBox="0 0 32 32" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
        f'<rect x="0.75" y="0.75" width="30.5" height="30.5" rx="8" fill="{bg}" stroke="#CBD5E1" stroke-width="1.2"/>'
        f'<text x="16" y="20.5" text-anchor="middle" font-family="Plus Jakarta Sans, sans-serif" '
        f'font-size="11.5" font-weight="700" fill="{accent}">{safe_code}</text></svg>'
    )


def _inject_css() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&family=Plus+Jakarta+Sans:wght@600;700&display=swap');

        html, body, [class*="css"] {
            font-family: 'DM Sans', -apple-system, BlinkMacSystemFont, sans-serif;
            color: #0F172A;
        }
        h1, h2, h3, h4, .jakarta {
            font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, sans-serif !important;
            letter-spacing: -0.015em;
            color: #0F172A;
        }
        code, pre, .mono, [data-testid="stMetricValue"] {
            font-family: 'JetBrains Mono', monospace !important;
            font-variant-numeric: tabular-nums !important;
        }
        .block-container {
            padding-top: 1.4rem;
            padding-bottom: 2rem;
            max-width: 1320px;
        }
        [data-testid="stSidebar"] {
            background-color: #FFFFFF;
            border-right: 1px solid #E2E8F0;
        }
        div[data-testid="stVerticalBlockBorderWrapper"] {
            border-color: #E2E8F0 !important;
            border-radius: 10px !important;
            background-color: #FFFFFF;
        }
        .stButton > button {
            border-radius: 8px;
            font-weight: 500;
            border-color: #CBD5E1;
        }
        header[data-testid="stHeader"] {
            background-color: #F8FAFC !important;
        }
        [data-testid="stMainBlockContainer"],
        [data-testid="stMainBlockContainer"] > div > [data-testid="stVerticalBlock"],
        [data-testid="stVerticalBlock"]:has(> div > .st-key-sticky_top_nav) {
            overflow: visible !important;
        }
        .st-key-chat_scroll_box {
            overflow-y: auto !important;
            overflow-x: hidden !important;
        }
        [data-testid="stVerticalBlock"] > div:has(.st-key-sticky_top_nav),
        .st-key-sticky_top_nav {
            position: sticky !important;
            top: 2.85rem !important;
            z-index: 99 !important;
            background-color: #F8FAFC !important;
            padding-top: 0.35rem;
            padding-bottom: 0.25rem;
        }
        .st-key-sticky_nav_tabs {
            border-bottom: 1px solid #E2E8F0;
            margin-top: 0.15rem;
        }
        .st-key-sticky_nav_tabs button[data-testid="stBaseButton-secondary"],
        .st-key-sticky_nav_tabs button[data-testid="stBaseButton-primary"] {
            background: transparent !important;
            border: none !important;
            border-radius: 6px 6px 0 0 !important;
            box-shadow: none !important;
            padding: 6px 6px !important;
            min-height: 2.2rem !important;
            font-weight: 500 !important;
            color: #475569 !important;
            white-space: nowrap !important;
        }
        .st-key-sticky_nav_tabs button p {
            white-space: nowrap !important;
            overflow: visible !important;
        }
        .st-key-sticky_nav_tabs button[data-testid="stBaseButton-secondary"]:hover {
            background: #F1F5F9 !important;
            color: #0F172A !important;
        }
        .st-key-sticky_nav_tabs button[data-testid="stBaseButton-primary"] {
            color: #0F172A !important;
            font-weight: 600 !important;
            border-bottom: 2.5px solid #0F172A !important;
        }
        .stTabs [data-baseweb="tab"] {
            font-family: 'DM Sans', sans-serif;
            font-weight: 500;
            padding: 8px 14px;
            border-radius: 8px 8px 0 0;
        }
        .telemetry-line {
            font-family: 'JetBrains Mono', monospace;
            font-variant-numeric: tabular-nums;
            font-size: 0.76rem;
            color: #64748B;
            margin-top: 0.35rem;
        }
        .status-ok { color: #15803D; font-weight: 600; font-size: 0.88rem; }
        .status-warn { color: #B45309; font-weight: 600; font-size: 0.88rem; }
        .status-err { color: #B91C1C; font-weight: 600; font-size: 0.88rem; }
        .stat-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin: 6px 0 10px 0;
        }
        .stat-cell {
            background: #F8FAFC;
            border: 1px solid #E2E8F0;
            border-radius: 8px;
            padding: 7px 10px;
        }
        .stat-lbl {
            font-size: 0.70rem;
            color: #64748B;
            text-transform: uppercase;
            letter-spacing: 0.04em;
        }
        .stat-val {
            font-family: 'JetBrains Mono', monospace;
            font-variant-numeric: tabular-nums;
            font-size: 0.83rem;
            font-weight: 600;
            color: #0F172A;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _render_wrapped_table(
    grid_rows: List[Dict[str, Any]],
    col_styles: Optional[Dict[str, str]] = None,
    semantic_cols: Optional[set[str]] = None,
) -> None:
    """Render a light-slate editorial HTML table with word-wrapping and tabular numbers."""
    if not grid_rows:
        return
    col_styles = col_styles or {}
    semantic_cols = semantic_cols or set()
    headers = list(grid_rows[0].keys())

    ths = "".join(
        f"<th style='padding:9px 11px; border-bottom:1px solid #CBD5E1; background:#F1F5F9; "
        f"color:#334155; font-size:0.78rem; font-weight:600; text-align:left; white-space:nowrap; "
        f"font-family:Plus Jakarta Sans, sans-serif;'>{_html.escape(h)}</th>"
        for h in headers
    )

    def _cell_style(col: str, val: str) -> str:
        base = col_styles.get(col, "")
        if col in semantic_cols:
            low = val.lower()
            if any(k in low for k in ("pass", "blocked", "resolved", "valid", "hit")):
                base += " color:#15803D; font-weight:600;"
            elif any(k in low for k in ("fail", "breached", "escalated", "invalid", "miss", "refused")):
                base += " color:#B91C1C; font-weight:600;"
            elif any(k in low for k in ("pending", "abstained", "warn")):
                base += " color:#B45309; font-weight:600;"
        return base

    trs = []
    for idx, row in enumerate(grid_rows):
        row_bg = "#FFFFFF" if idx % 2 == 0 else "#F8FAFC"
        tds = []
        for h in headers:
            val_str = str(row[h])
            tds.append(
                f"<td style='padding:8px 11px; border-bottom:1px solid #E2E8F0; vertical-align:top; "
                f"color:#0F172A; {_cell_style(h, val_str)}'>{_html.escape(val_str)}</td>"
            )
        trs.append(f"<tr style='background:{row_bg};'>{''.join(tds)}</tr>")

    table_html = (
        "<div style='overflow-x:auto; width:100%; border:1px solid #E2E8F0; border-radius:10px; margin-bottom:14px;'>"
        "<table style='width:100%; border-collapse:collapse; font-size:0.83rem; line-height:1.45;'>"
        f"<thead><tr>{ths}</tr></thead>"
        f"<tbody>{''.join(trs)}</tbody>"
        "</table></div>"
    )
    st.markdown(table_html, unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Cached heavy resources
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner="Indexing policy corpus...")
def load_agent() -> Copilot:
    return Copilot(PolicyCorpus(S.data_root, reranker=S.reranker), S)


@st.cache_resource
def shared():
    return RateLimiter(S.rate_limit_per_min, 60), ResponseCache(S.cache_ttl_s)


@st.cache_resource
def get_history() -> ChatHistory:
    return ChatHistory(S.state_dir)


@st.cache_resource
def get_feedback() -> FeedbackStore:
    return FeedbackStore(S.state_dir)


@st.cache_resource
def get_handoff_store() -> HandoffStore:
    return HandoffStore(S.state_dir)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------
def render(
    res: dict,
    idx: int = 0,
    feedback_store: Optional[FeedbackStore] = None,
    user_role: str = "",
    user_id: str = "",
    strategy: str = "",
    tts_enabled: bool = True,
    corpus: Optional[PolicyCorpus] = None,
) -> None:
    """Render a single agent response with citations, section reader, claims, TTS, and RLHF."""
    status = res.get("status", "")
    status_cfg = {
        "success": ("Answer", "status-ok"),
        "refused": ("Refused — Human review / safety boundary", "status-err"),
        "abstained": ("Not found in policy documents", "status-warn"),
    }
    label, css_cls = status_cfg.get(status, (status.replace("_", " ").title(), "status-warn"))
    cached_tag = " · Cached" if res.get("cached") else ""
    st.markdown(
        f"<div class='{css_cls}'>{_html.escape(label)}"
        f"<span style='color:#64748B; font-weight:400;'>{_html.escape(cached_tag)}</span></div>",
        unsafe_allow_html=True,
    )
    st.write(res.get("answer", ""))

    if res.get("citations"):
        st.markdown("**Sources**")
        for c_i, c in enumerate(res["citations"]):
            with st.expander(f"[{c['id']}] {c['path']} · {c['section'][:72]}"):
                st.write(c["snippet"])
                if corpus is not None:
                    sec_key = f"fullsec_{idx}_{c_i}_{res.get('trace_id', 'x')}"
                    if st.toggle("Read full section", key=sec_key):
                        full_text = corpus.read_section(c["path"], c["section"])
                        if full_text:
                            st.markdown(full_text)
                        else:
                            st.caption("Full section text unavailable for this path.")

    if res.get("claims"):
        with st.expander(f"Verified claims ({len(res['claims'])})"):
            for cl in res["claims"]:
                srcs = ", ".join(cl.get("sources", []))
                st.markdown(f"- {cl.get('text', '')} `[{srcs}]`")

    if status == "refused" and res.get("refusal_category") in ("consequential_action", "legal_advice"):
        st.info("A request for human follow-up was recorded in the Human Review Queue. Nothing has been approved or changed.")

    # Telemetry + TTS + RLHF row
    tele_col, tts_col, rlhf_col = st.columns([3.2, 1.0, 2.0])
    with tele_col:
        st.markdown(
            f"<div class='telemetry-line'>{res.get('latency_ms', 0):.0f} ms · "
            f"{res.get('tool_calls', 0)} tools · {res.get('tokens', 0)} tokens · "
            f"${res.get('cost_usd', 0):.5f} · trace {res.get('trace_id', '-')}</div>",
            unsafe_allow_html=True,
        )

    with tts_col:
        if tts_enabled and tts_available() and res.get("answer") and status in ("success", "abstained"):
            tts_btn_key = f"tts_{idx}_{res.get('trace_id', 'x')}"
            tts_wav_key = f"tts_wav_{idx}_{res.get('trace_id', 'x')}"
            just_generated = False
            if st.button("Read", icon=":material/volume_up:", key=tts_btn_key,
                         help="Read answer aloud"):
                with st.spinner("Generating audio..."):
                    wav = text_to_speech(res["answer"])
                if wav:
                    st.session_state[tts_wav_key] = wav
                    just_generated = True
                else:
                    st.warning("TTS unavailable on this machine.")
            if st.session_state.get(tts_wav_key):
                st.audio(st.session_state[tts_wav_key], format="audio/wav", autoplay=just_generated)

    with rlhf_col:
        if feedback_store is not None and res.get("trace_id"):
            trace_id = res["trace_id"]
            rating_key = f"rated_{trace_id}"
            if st.session_state.get(rating_key):
                lbl = "Helpful" if st.session_state[rating_key] > 0 else "Needs improvement"
                st.caption(f"Rated: {lbl}")
            else:
                r_col1, r_col2 = st.columns(2)
                if r_col1.button("Helpful", icon=":material/thumb_up:", key=f"up_{idx}_{trace_id}",
                                 help="This answer was helpful", use_container_width=True):
                    feedback_store.save(
                        trace_id=trace_id,
                        query=st.session_state.get("last_query", ""),
                        answer=res.get("answer", ""),
                        rating=1,
                        user_role=user_role,
                        user_id=user_id,
                        strategy=strategy,
                    )
                    st.session_state[rating_key] = 1
                    st.rerun()
                if r_col2.button("Improve", icon=":material/thumb_down:", key=f"dn_{idx}_{trace_id}",
                                 help="This answer needs improvement", use_container_width=True):
                    st.session_state[f"show_correction_{trace_id}"] = True
                    st.rerun()

        trace_id = res.get("trace_id", "")
        if trace_id and st.session_state.get(f"show_correction_{trace_id}"):
            with st.form(key=f"correction_form_{trace_id}", clear_on_submit=True):
                corr = st.text_area("What should the answer have been? (optional)", height=68)
                submitted = st.form_submit_button("Submit feedback", icon=":material/send:")
                if submitted and feedback_store is not None:
                    feedback_store.save(
                        trace_id=trace_id,
                        query=st.session_state.get("last_query", ""),
                        answer=res.get("answer", ""),
                        rating=-1,
                        correction=corr,
                        user_role=user_role,
                        user_id=user_id,
                        strategy=strategy,
                    )
                    st.session_state[f"rated_{trace_id}"] = -1
                    st.session_state.pop(f"show_correction_{trace_id}", None)
                    st.rerun()


# ---------------------------------------------------------------------------
# Login screen
# ---------------------------------------------------------------------------
def render_login() -> None:
    st.set_page_config(page_title="HR Policy Navigator — Sign In", layout="centered")
    _inject_css()

    st.markdown(
        f"""
        <div style='text-align:center; padding: 1.5rem 0 1.25rem 0;'>
            <div style='display:inline-block; margin-bottom:8px;'>{_BRAND_SVG}</div>
            <h1 style='font-size:2.0rem; margin:0;'>HR Policy Navigator</h1>
            <p style='color:#64748B; font-size:0.98rem; margin-top:6px;'>
                Multi-Agent Operations Copilot · Sourcegraph Handbook · Qwen3-8B
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col_form, col_hint = st.columns([1.0, 1.15], gap="large")

    with col_form:
        with st.container(border=True):
            st.markdown("#### Sign in")
            st.caption("Use your workspace credentials or select a demo persona.")
            with st.form("login_form"):
                username = st.text_input("Username", placeholder="e.g. emp_bob")
                password = st.text_input("Password", type="password", placeholder="••••••")
                submitted = st.form_submit_button("Sign in", icon=":material/login:", type="primary", use_container_width=True)
            if submitted:
                profile = authenticate(username, password)
                if profile:
                    st.session_state.user = profile
                    st.session_state.sid = uuid.uuid4().hex[:10]
                    st.session_state.log = []
                    st.rerun()
                else:
                    st.error("Invalid username or password.")

    with col_hint:
        with st.container(border=True):
            st.markdown("#### Demo Accounts")
            st.caption("One-click sign in for capstone evaluation.")
            for u in demo_users():
                rl = u["role"].lower()
                role_key = (
                    "sys_admin" if "system" in rl
                    else ("hr_ops" if "hr" in rl else ("manager" if "manager" in rl else "associate"))
                )
                meta = _ROLE_META[role_key]
                mono_svg = _role_monogram_svg(meta["code"], meta["accent"], meta["bg"])
                c_svg, c_info, c_btn = st.columns([0.55, 2.5, 1.1], vertical_alignment="center")
                with c_svg:
                    st.markdown(mono_svg, unsafe_allow_html=True)
                with c_info:
                    st.markdown(
                        f"<div style='line-height:1.3;'>"
                        f"<strong style='font-size:0.88rem;'>{_html.escape(u['name'])}</strong> "
                        f"<span style='color:#64748B; font-size:0.80rem;'>· {_html.escape(u['role'])}</span><br/>"
                        f"<span class='mono' style='font-size:0.75rem; color:#475569;'>"
                        f"{_html.escape(u['username'])} / {_html.escape(u['hint_password'])}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                with c_btn:
                    if st.button("Enter", key=f"demo_enter_{u['username']}", use_container_width=True):
                        profile = authenticate(u["username"], u["hint_password"])
                        if profile:
                            st.session_state.user = profile
                            st.session_state.sid = uuid.uuid4().hex[:10]
                            st.session_state.log = []
                            st.rerun()

    st.caption("This tool explains Sourcegraph HR policy. It cannot approve, decide, or act on HR matters.")


# ---------------------------------------------------------------------------
# Main authenticated application
# ---------------------------------------------------------------------------
def main() -> None:
    user: UserProfile = st.session_state.user
    meta = _ROLE_META.get(user.role, _ROLE_META["associate"])

    st.set_page_config(page_title=f"{S.app_title} — {user.name}", layout="wide")
    _inject_css()

    # Sticky top header card + role scope caption
    with st.container(key="sticky_top_nav"):
        with st.container(border=True):
            hdr_col, out_col = st.columns([6, 1.15], vertical_alignment="center")
            with hdr_col:
                st.markdown(
                    f"""
                    <div style='display:flex; align-items:center; gap:12px;'>
                        {_BRAND_SVG}
                        <div>
                            <div class='jakarta' style='font-size:1.25rem; font-weight:700; line-height:1.2;'>{_html.escape(S.app_title)}</div>
                            <div style='font-size:0.83rem; color:#475569; margin-top:2px;'>
                                Signed in as <strong>{_html.escape(user.name)}</strong> · {_html.escape(user.display_role)} · {_html.escape(user.tenure)} tenure
                            </div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            with out_col:
                if st.button("Sign out", icon=":material/logout:", key="top_sign_out", use_container_width=True):
                    for key in ["user", "sid", "log", "active_tab", "queued_sample_query", "last_eval_results", "last_eval_report", "birds_eye_result"]:
                        st.session_state.pop(key, None)
                    st.rerun()

        st.caption(f"{meta['hint']} · Answers are grounded in policy documents and cited.")

        # Role-Gated Navigation Bar pinned inside sticky_top_nav
        nav_items = [
            ("ask", "Ask", ":material/chat:"),
            ("traces", "Traces", ":material/monitoring:"),
            ("feedback", "Feedback", ":material/thumb_up:"),
        ]
        if user.role in ("hr_ops", "manager", "sys_admin"):
            nav_items.append(("review", "Human Review Queue", ":material/verified_user:"))
        if user.role == "sys_admin":
            nav_items.append(("eval_run", "Evaluation Runner", ":material/science:"))
            nav_items.append(("birds_eye", "Bird's Eye View", ":material/compare:"))

        valid_tab_ids = {t_id for t_id, _, _ in nav_items}
        if st.session_state.get("active_tab") not in valid_tab_ids:
            st.session_state["active_tab"] = "ask"
        active_tab = st.session_state["active_tab"]

        with st.container(key="sticky_nav_tabs"):
            col_weights = [max(1.5, len(lbl) * 0.14 + 0.95) for _, lbl, _ in nav_items]
            if len(nav_items) < 5:
                col_weights.append(max(1.0, (5 - len(nav_items)) * 1.4))
            nav_cols = st.columns(col_weights, gap="small")
            for idx, (t_id, t_lbl, t_ico) in enumerate(nav_items):
                with nav_cols[idx]:
                    if st.button(
                        t_lbl,
                        icon=t_ico,
                        key=f"nav_tab_{t_id}",
                        type="primary" if active_tab == t_id else "secondary",
                        use_container_width=True,
                    ):
                        st.session_state["active_tab"] = t_id
                        st.rerun()

    agent          = load_agent()
    limiter, cache = shared()
    history_store  = get_history()
    feedback_store = get_feedback()

    st.session_state.setdefault("sid", uuid.uuid4().hex[:10])
    st.session_state.setdefault("log", [])

    user_profile_dict = {
        "user_id": user.user_id,
        "name": user.name,
        "role": user.role,
        "tenure": user.tenure,
        "team_id": user.team_id,
    }

    # ------------------------------------------------------------------
    # Sidebar
    # ------------------------------------------------------------------
    with st.sidebar:
        mono_svg = _role_monogram_svg(meta["code"], meta["accent"], meta["bg"])
        st.markdown(
            f"""
            <div style='display:flex; align-items:center; gap:10px; margin-bottom:10px;'>
                {mono_svg}
                <div>
                    <div class='jakarta' style='font-weight:700; font-size:0.98rem;'>{_html.escape(user.name)}</div>
                    <div style='font-size:0.78rem; color:#64748B;'>{_html.escape(user.display_role)} · {_html.escape(user.tenure)}</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        # Compact 2-column corpus/engine stats grid
        model_short = S.model.split("/")[-1] if S.has_model else "Not set"
        st.markdown(
            f"""
            <div class='stat-grid'>
                <div class='stat-cell'><div class='stat-lbl'>Documents</div><div class='stat-val'>{len(agent.corpus.docs)}</div></div>
                <div class='stat-cell'><div class='stat-lbl'>Indexed chunks</div><div class='stat-val'>{len(agent.corpus.chunks)}</div></div>
                <div class='stat-cell'><div class='stat-lbl'>Reranker</div><div class='stat-val'>{_html.escape(agent.corpus.reranker_name)}</div></div>
                <div class='stat-cell'><div class='stat-lbl'>Model</div><div class='stat-val'>{_html.escape(model_short)}</div></div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        with st.expander("System & Options", icon=":material/tune:", expanded=False):
            opt_personalization = st.toggle(
                "Personalization", value=True, key="toggle_personalization",
                help="Tailor answers based on your role and tenure.",
            )
            opt_tts = st.toggle(
                "Read Out Loud (TTS)", value=True, key="toggle_read_aloud",
                help="Show Read button under answers.",
            )
            opt_voice = st.toggle(
                "Voice Input (Mic)", value=True, key="toggle_voice_input",
                help="Enable microphone recording input widget.",
            )
            st.divider()
            st.caption(f"Chunking: `{agent.corpus.chunking}`")
            st.caption(f"Dense: `{agent.corpus.embedder.name if agent.corpus.embedder else 'off'}`")
            st.caption(f"Cache hits/misses: `{cache.hits}/{cache.misses}`")
            st.caption(f"Voice STT: {'Ready' if stt_available() else 'Off'} · TTS: {'Ready' if tts_available() else 'Unavailable'}")
            for note in agent.corpus.notes:
                st.warning(note)

        strategy = st.selectbox(
            "Prompt strategy",
            ["structured", "selfconsistency", "decomposition", "baseline"],
            index=0,
            help="structured = best balanced quality; decomposition = multi-hop; selfconsistency = majority vote",
        )

        st.divider()

        st.markdown("#### Current Session")
        curr_title = history_store.get_title(user.user_id, st.session_state.sid, default=f"Session {st.session_state.sid[:6]}")
        st.caption(f"Title: **{curr_title}**")
        c_new, c_ren = st.columns(2)
        with c_new:
            if st.button("New chat", icon=":material/add:", use_container_width=True):
                st.session_state.sid = uuid.uuid4().hex[:10]
                st.session_state.log = []
                st.rerun()
        with c_ren:
            with st.popover("Rename", icon=":material/edit:", use_container_width=True):
                new_curr_name = st.text_input("Chat title", value=curr_title, key="rename_curr_input")
                if st.button("Save title", key="btn_save_curr_title", use_container_width=True):
                    if new_curr_name.strip():
                        history_store.rename_session(user.user_id, st.session_state.sid, new_curr_name.strip())
                        st.toast("Chat renamed.")
                        st.rerun()

        st.markdown("#### Previous Sessions (Last 3)")
        sessions = history_store.list_sessions(user.user_id, limit=3)
        if not sessions:
            st.caption("No previous sessions in the last 7 days.")
        else:
            session_options = {
                s["session_id"]: f"{s['title']} ({s['date']} · {s['turn_count']} turns)"
                for s in sessions
            }
            chosen_sid = st.selectbox(
                "Select a past session",
                [""] + list(session_options.keys()),
                format_func=lambda sid: "— choose session —" if sid == "" else session_options[sid],
                key="past_session_select",
            )
            if chosen_sid:
                chosen_s = next(s for s in sessions if s["session_id"] == chosen_sid)
                b_load, b_ren = st.columns(2)
                with b_load:
                    if st.button("Load", icon=":material/folder_open:", key=f"load_{chosen_sid}", use_container_width=True):
                        st.session_state.log = history_store.load_session(chosen_s["file"])
                        st.session_state.sid = chosen_s["session_id"]
                        st.rerun()
                with b_ren:
                    with st.popover("Rename", icon=":material/edit:", use_container_width=True):
                        new_past_name = st.text_input("Title", value=chosen_s["title"], key=f"rename_input_{chosen_sid}")
                        if st.button("Save", key=f"save_past_{chosen_sid}", use_container_width=True):
                            if new_past_name.strip():
                                history_store.rename_session(user.user_id, chosen_sid, new_past_name.strip())
                                st.toast("Session renamed.")
                                st.rerun()

        st.divider()

        st.markdown("#### Sample Queries")
        for s_idx, sq in enumerate(meta["sample_queries"]):
            if st.button(sq, key=f"sb_sample_{s_idx}", use_container_width=True):
                st.session_state["queued_sample_query"] = sq
                st.session_state["active_tab"] = "ask"
                st.rerun()

        with st.expander("Quick Guide", icon=":material/help:", expanded=False):
            st.markdown(
                """
                **Capabilities**
                - Answers sourced strictly from policy documents with `[S#]` citations
                - On-demand full section reader inside each citation
                - Voice recording input and offline Read Out Loud (TTS)
                - Helpful / Improve RLHF feedback logging

                **Boundaries**
                - Cannot approve or deny HR requests
                - Cannot access personal records or salaries
                - Escalates consequential requests to HR Operations
                """
            )

    # ------------------------------------------------------------------
    # Role-Gated Tab Contexts (controlled by sticky_nav_tabs above)
    # ------------------------------------------------------------------
    active_tab = st.session_state.get("active_tab", "ask")
    tab_chat      = st.container() if active_tab == "ask" else None
    tab_traces    = st.container() if active_tab == "traces" else None
    tab_fb        = st.container() if active_tab == "feedback" else None
    tab_review    = st.container() if (active_tab == "review" and user.role in ("hr_ops", "manager", "sys_admin")) else None
    tab_eval_run  = st.container() if (active_tab == "eval_run" and user.role == "sys_admin") else None
    tab_birds_eye = st.container() if (active_tab == "birds_eye" and user.role == "sys_admin") else None

    # ------------------------------------------------------------------
    # Voice Input helper
    # ------------------------------------------------------------------
    def _voice_input_widget() -> Optional[str]:
        """Render audio recorder inside an expander with confirmation controls."""
        st.session_state.setdefault("mic_idx", 0)
        mic_key = f"mic_{st.session_state.mic_idx}"

        with st.expander("Voice Input (Record from microphone)", icon=":material/mic:", expanded=False):
            try:
                audio_val = st.audio_input("Record your question", key=mic_key)
            except (AttributeError, Exception):
                return None

            if audio_val is not None:
                audio_bytes = audio_val.getvalue()
                if audio_bytes:
                    cur_hash = hashlib.md5(audio_bytes).hexdigest()[:12]
                    if st.session_state.get("cached_audio_hash") != cur_hash:
                        with st.spinner("Transcribing audio..."):
                            from hr_policy_navigator.voice import transcribe
                            st.session_state["pending_voice_text"] = transcribe(audio_bytes)
                            st.session_state["cached_audio_hash"] = cur_hash

            pending = st.session_state.get("pending_voice_text", "").strip()
            if pending:
                st.info(f"Transcribed: \"{pending}\"")
                b1, b2 = st.columns(2)
                with b1:
                    if st.button("Ask this question", icon=":material/send:", type="primary", key="btn_send_voice", use_container_width=True):
                        q_to_send = pending
                        st.session_state["pending_voice_text"] = ""
                        st.session_state["cached_audio_hash"] = ""
                        st.session_state["mic_idx"] += 1
                        return q_to_send
                with b2:
                    if st.button("Clear voice", icon=":material/close:", key="btn_clear_voice", use_container_width=True):
                        st.session_state["pending_voice_text"] = ""
                        st.session_state["cached_audio_hash"] = ""
                        st.session_state["mic_idx"] += 1
                        st.rerun()
        return None

    # ==================================================================
    # Tab 1: Ask
    # ==================================================================
    if tab_chat is not None:
        with tab_chat:
            tenure_labels = {"new": "New Hire (<1 yr)", "mid": "1-3 yrs", "senior": "Tenured (3+ yrs)"}
            persona_text = (
                f"{user.display_role} · {tenure_labels.get(user.tenure, user.tenure)}"
                if opt_personalization else "Standard (Personalization disabled)"
            )
            st.caption(f"Persona: **{persona_text}** · Strategy: `{strategy}`")

            chat_container = st.container(height=520, border=True, key="chat_scroll_box")
            with chat_container:
                if not st.session_state.log:
                    st.markdown(
                        f"""
                        <div style='text-align:center; padding: 2.2rem 1rem 1.2rem 1rem;'>
                            <div style='display:inline-block; margin-bottom:10px;'>{_EMPTY_STATE_SVG}</div>
                            <div class='jakarta' style='font-size:1.08rem; font-weight:600; color:#0F172A;'>
                                Ask any Sourcegraph HR policy question
                            </div>
                            <div style='font-size:0.86rem; color:#64748B; margin-top:4px;'>
                                Select a sample question below or type in the input bar.
                            </div>
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )
                    sample_cols = st.columns(len(meta["sample_queries"]))
                    for i_s, sq in enumerate(meta["sample_queries"]):
                        with sample_cols[i_s]:
                            if st.button(sq, key=f"empty_sample_{i_s}", use_container_width=True):
                                st.session_state["queued_sample_query"] = sq
                                st.rerun()
                else:
                    for i, (turn_q, turn_r) in enumerate(st.session_state.log):
                        with st.chat_message("user"):
                            st.write(turn_q)
                        with st.chat_message("assistant"):
                            render(
                                turn_r,
                                idx=i,
                                feedback_store=feedback_store,
                                user_role=user.role,
                                user_id=user.user_id,
                                strategy=strategy,
                                tts_enabled=opt_tts,
                                corpus=agent.corpus,
                            )

            voice_query = _voice_input_widget() if (opt_voice and stt_available()) else None
            queued_q = st.session_state.pop("queued_sample_query", None)
            q = st.chat_input("Ask about benefits, leave, remote work, conduct...")
            final_q = queued_q or q or voice_query

            if final_q:
                st.session_state["last_query"] = final_q
                ok, wait = limiter.check(st.session_state.sid)
                if not ok:
                    st.warning(f"Rate limit reached. Please wait {wait:.0f}s.")
                else:
                    active_profile = user_profile_dict if opt_personalization else None
                    persona_cache_key = f"{user.role}:{user.tenure}" if opt_personalization else "standard"
                    res = cache.get(final_q, strategy, persona=persona_cache_key) if len(st.session_state.log) == 0 else None
                    if res is None:
                        with st.spinner("Searching policies..."):
                            try:
                                res = agent.run(
                                    final_q, st.session_state.sid, strategy,
                                    user_profile=active_profile,
                                )
                            except TypeError:
                                res = agent.run(final_q, st.session_state.sid, strategy)
                        if len(st.session_state.log) == 0:
                            cache.put(final_q, strategy, res, persona=persona_cache_key)

                    st.session_state.log.append((final_q, res))
                    history_store.append(user.user_id, st.session_state.sid, final_q, res)
                    st.rerun()

    # ==================================================================
    # Tab 2: Traces
    # ==================================================================
    if tab_traces is not None:
        with tab_traces:
            tr_hdr, tr_btn = st.columns([5, 1], vertical_alignment="center")
            with tr_hdr:
                st.markdown("### Execution Traces")
            with tr_btn:
                if st.button("Refresh", icon=":material/refresh:", key="btn_refresh_traces", use_container_width=True):
                    st.rerun()

            all_traces = load_traces(S.trace_dir)
            if user.role in ("hr_ops", "sys_admin"):
                traces = all_traces
                st.caption(f"Viewing all system traces ({user.display_role} view).")
            else:
                user_sessions = {s["session_id"] for s in history_store.list_sessions(user.user_id, limit=20)}
                user_sessions.add(st.session_state.sid)
                traces = [
                    t for t in all_traces
                    if t.get("meta", {}).get("user_id") == user.user_id or t.get("meta", {}).get("session") in user_sessions
                ]
                st.caption(f"Viewing traces for your account ({user.name}).")

            if not traces:
                st.info("No traces yet for your account. Ask a question first.")
            else:
                m = summarize(traces)
                c = st.columns(5)
                c[0].metric("Queries", m["queries"])
                c[1].metric("p50 ms", m["p50_ms"])
                c[2].metric("p95 ms", m["p95_ms"])
                c[3].metric("Avg tool calls", m["avg_tool_calls"])
                c[4].metric("JSON repairs", m["json_repairs"])

                status_summary = " · ".join(f"{k}: {v}" for k, v in m.get("statuses", {}).items())
                st.caption(f"Status breakdown: {status_summary}")

                st.dataframe(
                    [
                        {
                            "trace": t["trace_id"],
                            "query": t["meta"].get("query", "")[:80],
                            "status": t["result"]["status"],
                            "ms": t["duration_ms"],
                            **{k: v for k, v in t["counters"].items()},
                        }
                        for t in reversed(traces[-200:])
                    ],
                    width="stretch",
                    hide_index=True,
                )

                pick = st.selectbox("Inspect trace", [t["trace_id"] for t in reversed(traces[-50:])], key="trace_pick")
                picked_trace = next(t for t in traces if t["trace_id"] == pick)
                span_rows = [
                    {
                        "Stage": f"{sp.get('kind', '')} · {sp.get('name', '')}",
                        "Status": sp.get("status", "ok"),
                        "Duration (ms)": sp.get("duration_ms", 0.0),
                        "Details": (sp.get("error") or json.dumps(sp.get("attrs", {}), ensure_ascii=False))[:160],
                    }
                    for sp in picked_trace.get("spans", [])
                ]
                if span_rows:
                    st.markdown("#### Span Execution Timeline")
                    _render_wrapped_table(
                        span_rows,
                        {
                            "Stage": "white-space:nowrap; font-family:JetBrains Mono, monospace; font-weight:600;",
                            "Status": "white-space:nowrap;",
                            "Duration (ms)": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                            "Details": "min-width:260px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                        },
                        semantic_cols={"Status"},
                    )
                with st.expander("Raw Trace JSON", expanded=False):
                    st.json(picked_trace)

    # ==================================================================
    # Tab 3: Feedback
    # ==================================================================
    if tab_fb is not None:
        with tab_fb:
            st.markdown("### RLHF Feedback Analytics")
            if user.role in ("hr_ops", "sys_admin"):
                fb = feedback_store.analytics()
                raw_feedback = feedback_store.all()
                st.caption(f"Viewing feedback across all users ({user.display_role} view).")
            else:
                fb = feedback_store.analytics(user_id=user.user_id)
                raw_feedback = feedback_store.all(user_id=user.user_id)
                st.caption(f"Viewing feedback for your account ({user.name}).")

            if fb["total"] == 0:
                st.info("No feedback yet. Rate answers in the Ask tab to populate this dashboard.")
                st.markdown(
                    """
                    **How the RLHF loop works:**
                    1. User rates an answer **Helpful** or **Improve** in the Ask tab.
                    2. This dashboard surfaces low-rated queries as prompt and retrieval improvement candidates.
                    """
                )
            else:
                mc = st.columns(4)
                mc[0].metric("Total Ratings", fb["total"])
                mc[1].metric("Positive", fb["positive"])
                mc[2].metric("Negative", fb["negative"])
                mc[3].metric(
                    "Satisfaction",
                    f"{fb['pct_positive']}%",
                    delta=f"{fb['pct_positive'] - 70:.1f}%" if fb["total"] > 5 else None,
                    delta_color="normal",
                )

                st.divider()
                left, right = st.columns(2)

                with left:
                    st.markdown("#### By Role")
                    if fb["by_role"]:
                        role_data = [
                            {"Role": role, "Positive": v["pos"], "Negative": v["neg"], "Total": v["total"]}
                            for role, v in fb["by_role"].items()
                        ]
                        st.dataframe(role_data, width="stretch", hide_index=True)

                    st.markdown("#### By Strategy")
                    if fb["by_strategy"]:
                        strat_data = [
                            {"Strategy": s, "Positive": v["pos"], "Negative": v["neg"], "Total": v["total"]}
                            for s, v in fb["by_strategy"].items()
                        ]
                        st.dataframe(strat_data, width="stretch", hide_index=True)

                with right:
                    st.markdown("#### Improvement Opportunities")
                    st.caption("Queries with the most negative ratings.")
                    if fb["low_rated"]:
                        for item in fb["low_rated"]:
                            with st.container(border=True):
                                st.markdown(f"<span class='status-err'>{item['count']}x Negative</span> — *\"{ _html.escape(item['query']) }\"*", unsafe_allow_html=True)
                                if item.get("answer_snippet"):
                                    st.caption(f"Answer: {item['answer_snippet'][:110]}...")
                    else:
                        st.success("No negatively-rated queries yet.")

                    st.markdown("#### Top Helpful Queries")
                    if fb["high_rated"]:
                        for item in fb["high_rated"]:
                            with st.container(border=True):
                                st.markdown(f"<span class='status-ok'>{item['count']}x Helpful</span> — *\"{ _html.escape(item['query']) }\"*", unsafe_allow_html=True)

                if fb["corrections"]:
                    st.divider()
                    st.markdown("#### Recent User Corrections")
                    for c_item in fb["corrections"]:
                        with st.container(border=True):
                            st.caption(c_item["ts"])
                            st.markdown(f"**Question:** *{c_item['query']}*")
                            st.markdown(f"**Suggested correction:** {c_item['correction']}")

                st.divider()
                with st.expander("Raw Feedback Log", expanded=False):
                    st.dataframe(raw_feedback, width="stretch", hide_index=True)

    # ==================================================================
    # Tab 4: Human Review Queue (hr_ops, manager, sys_admin)
    # ==================================================================
    if tab_review is not None:
        with tab_review:
            hq_hdr, hq_btn = st.columns([5, 1], vertical_alignment="center")
            with hq_hdr:
                st.markdown("### Human Review Queue")
            with hq_btn:
                if st.button("Refresh", icon=":material/refresh:", key="btn_refresh_hitl", use_container_width=True):
                    st.rerun()

            st.caption("Requests the assistant declined to decide automatically. A human reviewer must handle these.")
            handoff_store = HandoffStore(S.state_dir)
            rows = handoff_store.all(role=user.role, team_id=user.team_id)

            if rows:
                pending_n = sum(1 for r in rows if r.get("ui_status", "pending") == "pending")
                ack_n     = sum(1 for r in rows if r.get("ui_status") == "acknowledged")
                res_n     = sum(1 for r in rows if r.get("ui_status") in ("resolved", "escalated"))
                mc = st.columns(3)
                mc[0].metric("Pending", pending_n)
                mc[1].metric("Acknowledged", ack_n)
                mc[2].metric("Resolved / Escalated", res_n)
                st.divider()

            if not rows:
                st.success("Queue is empty — no pending requests.")
            else:
                status_text_map = {
                    "pending": ("Pending", "#B45309"),
                    "acknowledged": ("Acknowledged", "#1E3A8A"),
                    "resolved": ("Resolved", "#15803D"),
                    "escalated": ("Escalated", "#B91C1C"),
                }
                for row in rows:
                    ticket_id      = row.get("ticket_id", "unknown")
                    current_status = row.get("ui_status", "pending")
                    allowed_next   = VALID_TRANSITIONS.get(current_status, [])
                    st_lbl, st_clr = status_text_map.get(current_status, ("Pending", "#B45309"))

                    with st.container(border=True):
                        hdr_col, badge_col = st.columns([3.5, 1])
                        with hdr_col:
                            st.markdown(
                                f"**Ticket:** `{ticket_id}`  \n"
                                f"**Category:** `{row.get('reason', row.get('category', '—'))}`  \n"
                                f"**Query:** {row.get('topic', row.get('query', '—'))[:180]}"
                            )
                            role_lbl = row.get("user_role", "")
                            st.caption(
                                f"{row.get('timestamp', 'unknown')}"
                                + (f" · Role: {role_lbl}" if role_lbl else "")
                                + (f" · Session: {row.get('session_id', '')[:12]}" if row.get("session_id") else "")
                            )
                        with badge_col:
                            st.markdown(
                                f"<div style='text-align:right; color:{st_clr}; font-weight:700; font-size:0.9rem;'>"
                                f"{_html.escape(st_lbl)}</div>",
                                unsafe_allow_html=True,
                            )

                        if row.get("notes"):
                            for n_item in row["notes"]:
                                st.caption(f"Note ({n_item.get('actor', 'reviewer')} · {n_item.get('ts', '')[:19]}): {n_item.get('text', '')}")

                        if allowed_next:
                            btn_cols = st.columns(len(allowed_next) + 1)
                            btn_meta = {
                                "acknowledged": ("Acknowledge", ":material/check_circle:"),
                                "resolved": ("Mark resolved", ":material/task_alt:"),
                                "escalated": ("Escalate", ":material/priority_high:"),
                            }
                            for i, next_st in enumerate(allowed_next):
                                lbl, ico = btn_meta.get(next_st, (next_st.capitalize(), ":material/arrow_forward:"))
                                if btn_cols[i].button(lbl, icon=ico, key=f"{ticket_id}_{next_st}", use_container_width=True):
                                    handoff_store.update_status(ticket_id, next_st, actor=user.name)
                                    st.toast(f"Ticket {ticket_id[:10]} -> {next_st}")
                                    st.rerun()
                            with st.expander("Add resolution note", icon=":material/note_add:"):
                                note_text = st.text_area("Note", key=f"note_{ticket_id}", height=76, placeholder="Describe action taken...")
                                if st.button("Save note", key=f"savenote_{ticket_id}"):
                                    if note_text.strip():
                                        handoff_store.add_note(ticket_id, note_text.strip(), actor=user.name)
                                        st.toast("Note saved.")
                                        st.rerun()
                        else:
                            st.caption("Ticket closed — no further transitions available.")

    # ==================================================================
    # Tab 6: Evaluation Runner (sys_admin)
    # ==================================================================
    if tab_eval_run is not None:
        with tab_eval_run:
            from hr_policy_navigator.eval_runner import (
                WEEK1_SYSTEM_PROMPTS,
                generate_week_wise_report,
                load_eval_datasets,
                run_week1_eval,
                run_week2_eval,
                run_week3_eval,
                run_week4_eval,
            )

            def _format_w1_grid(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
                return [
                    {
                        "ID": r["id"],
                        "Question Type": "Domain" if r.get("dataset", "domain" if r.get("type") == "answerable" else "adversarial") == "domain" else "Adversarial",
                        "Question": r.get("question", ""),
                        "Gold Answer": r["gold_answer"] or f"[{r['type']} · expect {r.get('expected_status', 'abstained')}]",
                        "Model Answer": r["model_answer"],
                        "Decision Accuracy": "Pass" if r["correct"] else "Fail",
                        "ROUGE-LCS F1": f"{r['rouge_lcs_f1']:.4f}" if r.get("rouge_lcs_f1") is not None else "—",
                        "Valid JSON": "Valid" if r["json_valid"] else "Invalid",
                        "Latency (ms)": r["latency_ms"],
                        "Primary Failures": r["failure"] or "—",
                    }
                    for r in rows
                ]

            def _render_w1_grids(w1_res: dict) -> None:
                col_styles = {
                    "ID": "white-space:nowrap; font-weight:600; font-family:JetBrains Mono, monospace;",
                    "Question Type": "white-space:nowrap; font-weight:600;",
                    "Question": "min-width:180px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                    "Gold Answer": "min-width:240px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                    "Model Answer": "min-width:240px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                    "Decision Accuracy": "white-space:nowrap;",
                    "ROUGE-LCS F1": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                    "Valid JSON": "white-space:nowrap; text-align:center;",
                    "Latency (ms)": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                    "Primary Failures": "min-width:130px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                }
                for strat_name, strat_data in w1_res.get("strategies", {}).items():
                    st.markdown(f"#### `{strat_name}`")
                    sys_prompt = strat_data.get("system_prompt") or WEEK1_SYSTEM_PROMPTS.get(strat_name, "")
                    if sys_prompt:
                        st.caption("System Prompt:")
                        st.code(sys_prompt, language="text")
                    _render_wrapped_table(_format_w1_grid(strat_data.get("rows", [])), col_styles, semantic_cols={"Decision Accuracy", "Valid JSON"})

            def _format_w2_grid(run_data: dict) -> List[Dict[str, Any]]:
                ci_str = str(run_data.get("hit5_ci95", "—"))
                return [
                    {
                        "ID": r["id"],
                        "Question Type": "Domain",
                        "Question": r.get("question", ""),
                        "expected_sources": ", ".join(r.get("expected_sources", [])),
                        "retrieved chunks": "\n".join(r.get("retrieved_chunks", [])),
                        "Retrieval Mode": r.get("mode", run_data.get("mode", "")),
                        "Hit@1": f"{r['hit1']:.3f}",
                        "Hit@3": f"{r['hit3']:.3f}",
                        "Hit@5": f"{r['hit5']:.3f}",
                        "Doc Recall@5": f"{r['doc_recall_5']:.3f}",
                        "MRR": f"{r['mrr']:.3f}",
                        "Hit@5 95% CI": ci_str,
                        "Latency (ms)": r.get("latency_ms", 0.0),
                    }
                    for r in run_data.get("rows", [])
                ]

            def _render_w2_grids(w2_res: dict) -> None:
                col_styles = {
                    "ID": "white-space:nowrap; font-weight:600; font-family:JetBrains Mono, monospace;",
                    "Question Type": "white-space:nowrap; font-weight:600;",
                    "Question": "min-width:180px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                    "expected_sources": "min-width:180px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                    "retrieved chunks": "min-width:240px; white-space:pre-wrap; word-wrap:break-word; overflow-wrap:anywhere;",
                    "Retrieval Mode": "white-space:nowrap; font-weight:600;",
                    "Hit@1": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                    "Hit@3": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                    "Hit@5": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                    "Doc Recall@5": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                    "MRR": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                    "Hit@5 95% CI": "white-space:nowrap; text-align:center; font-family:JetBrains Mono, monospace;",
                    "Latency (ms)": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                }
                for run_key, run_data in w2_res.get("runs", {}).items():
                    st.markdown(f"#### `{run_key}`")
                    _render_wrapped_table(_format_w2_grid(run_data), col_styles)

            def _format_w3_grid(strat_name: str, strat_data: dict) -> List[Dict[str, Any]]:
                return [
                    {
                        "ID": r["id"],
                        "Question Type": "Domain" if r.get("type", "answerable") == "answerable" else "Adversarial",
                        "Question": r.get("question", ""),
                        "Gold Answer": r.get("gold_answer") or f"[{r.get('type', '')} · expect {r.get('expected_status', 'abstained')}]",
                        "Strategy": r.get("strategy", strat_name),
                        "Model Answer": r.get("model_answer", ""),
                        "Decision Accuracy": "Pass" if r.get("correct") else "Fail",
                        "ROUGE-LCS F1": f"{r['rouge_lcs_f1']:.4f}" if r.get("rouge_lcs_f1") is not None else "—",
                        "Retrieval Hit in Context": (
                            "Hit" if r["retrieval_hit"] else "Miss"
                        ) if r.get("retrieval_hit") is not None else "—",
                        "Tool Calls": r.get("tool_calls", 0),
                        "LLM Calls": r.get("llm_calls", 0),
                        "Tokens": r.get("tokens", 0),
                        "Cost (USD)": f"${r.get('cost_usd', 0.0):.6f}",
                        "JSON Repairs": r.get("json_repairs", 0),
                        "Latency": r.get("latency_ms", 0.0),
                    }
                    for r in strat_data.get("rows", [])
                ]

            def _render_w3_grids(w3_res: dict) -> None:
                col_styles = {
                    "ID": "white-space:nowrap; font-weight:600; font-family:JetBrains Mono, monospace;",
                    "Question Type": "white-space:nowrap; font-weight:600;",
                    "Question": "min-width:170px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                    "Gold Answer": "min-width:220px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                    "Strategy": "white-space:nowrap; font-weight:600;",
                    "Model Answer": "min-width:220px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                    "Decision Accuracy": "white-space:nowrap;",
                    "ROUGE-LCS F1": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                    "Retrieval Hit in Context": "white-space:nowrap; text-align:center;",
                    "Tool Calls": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                    "LLM Calls": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                    "Tokens": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                    "Cost (USD)": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                    "JSON Repairs": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                    "Latency": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                }
                for strat_name, strat_data in w3_res.get("strategies", {}).items():
                    st.markdown(f"#### `{strat_name}`")
                    _render_wrapped_table(_format_w3_grid(strat_name, strat_data), col_styles, semantic_cols={"Decision Accuracy", "Retrieval Hit in Context"})

            def _format_w4_grid(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
                return [
                    {
                        "ID": r.get("id", ""),
                        "Question Type": "Adversarial",
                        "Question": r.get("question", ""),
                        "Gold Answer": r.get("gold_answer", ""),
                        "Attack Vector": r.get("attack", ""),
                        "Modified query": r.get("modified_query", ""),
                        "Model Answer": r.get("model_answer", ""),
                        "Status": r.get("status", ""),
                        "Defence Result": "Breached" if r.get("attack_succeeded") else "Blocked",
                        "If breached, then how?": r.get("breach_reason", "—"),
                    }
                    for r in rows
                ]

            def _render_w4_grids(w4_res: dict) -> None:
                col_styles = {
                    "ID": "white-space:nowrap; font-weight:600; font-family:JetBrains Mono, monospace;",
                    "Question Type": "white-space:nowrap; font-weight:600;",
                    "Question": "min-width:150px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                    "Gold Answer": "min-width:160px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                    "Attack Vector": "white-space:nowrap; font-weight:600; font-family:JetBrains Mono, monospace;",
                    "Modified query": "min-width:200px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                    "Model Answer": "min-width:200px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                    "Status": "white-space:nowrap;",
                    "Defence Result": "white-space:nowrap;",
                    "If breached, then how?": "min-width:180px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                }
                _render_wrapped_table(_format_w4_grid(w4_res.get("rows", [])), col_styles, semantic_cols={"Defence Result"})

            st.markdown("### Evaluation Runner & Week-Wise Report Generator")
            st.caption("Run Week 1-4 benchmarks on configurable slices of domain queries and adversarial queries.")

            all_domain, all_adv = load_eval_datasets()
            max_domain = len(all_domain)
            max_adv = len(all_adv)

            with st.container(border=True):
                cfg_col1, cfg_col2, cfg_col3 = st.columns([1, 1, 1.2])
                with cfg_col1:
                    n_domain = st.number_input(
                        f"Domain queries (max {max_domain})",
                        min_value=0, max_value=max_domain, value=max_domain, step=1,
                        key="eval_n_domain",
                    )
                with cfg_col2:
                    n_adv = st.number_input(
                        f"Adversarial queries (max {max_adv})",
                        min_value=0, max_value=max_adv, value=max_adv, step=1,
                        key="eval_n_adv",
                    )
                with cfg_col3:
                    eval_scope = st.selectbox(
                        "Evaluation Scope",
                        [
                            "Run All (Week 1 -> Week 4)",
                            "Week 1: Prompt Baselines (No RAG)",
                            "Week 2: Retrieval Evaluation",
                            "Week 3: Multi-Agent E2E + ROUGE-LCS",
                            "Week 4: Injection Defense Suite",
                        ],
                        key="eval_scope_select",
                    )

                opt_col1, opt_col2, opt_col3 = st.columns(3)
                with opt_col1:
                    w1_strats = st.multiselect(
                        "Week 1 Strategies",
                        ["zero_shot", "structured_fewshot", "chain_of_thought", "decomposition"],
                        default=["zero_shot", "structured_fewshot", "chain_of_thought", "decomposition"],
                        key="eval_w1_strats",
                    )
                with opt_col2:
                    w2_chunks = st.multiselect(
                        "Week 2 Chunking",
                        ["heading_cap", "fixed_500", "fixed_200", "heading"],
                        default=["heading_cap", "fixed_500", "fixed_200", "heading"],
                        key="eval_w2_chunks",
                    )
                with opt_col3:
                    w3_strats = st.multiselect(
                        "Week 3 Strategies",
                        ["baseline", "structured", "selfconsistency", "decomposition"],
                        default=["baseline", "structured", "selfconsistency", "decomposition"],
                        key="eval_w3_strats",
                    )

                run_clicked = st.button("Run evaluation", icon=":material/play_arrow:", type="primary", key="btn_run_eval")

            status_placeholder = st.empty()
            results_placeholder = st.empty()

            if run_clicked:
                if n_domain == 0 and n_adv == 0 and "Week 4" not in eval_scope:
                    st.warning("Select at least 1 domain query or adversarial query.")
                else:
                    st.session_state.pop("last_eval_results", None)
                    st.session_state.pop("last_eval_report", None)
                    st.session_state.pop("last_eval_path", None)
                    results_placeholder.empty()

                    domain_slice, adv_slice = load_eval_datasets(int(n_domain), int(n_adv))
                    eval_results: dict = {}
                    run_all = eval_scope.startswith("Run All")

                    with status_placeholder.status("Running evaluation suite...", expanded=True) as status_box:
                        run_scroll = st.container(height=260)
                        with run_scroll:
                            log_area = st.empty()
                            log_lines: List[str] = []

                            def _cb(msg: str) -> None:
                                log_lines.append(msg)
                                log_area.text("\n".join(log_lines[-30:]))

                            if run_all or eval_scope.startswith("Week 1"):
                                _cb("Starting Week 1: Single-Prompt Baselines...")
                                eval_results["week1"] = run_week1_eval(
                                    domain_slice, adv_slice, S, use_mock=False,
                                    strategies=w1_strats or ["zero_shot"], progress_cb=_cb
                                )
                            if run_all or eval_scope.startswith("Week 2"):
                                _cb("Starting Week 2: Retrieval Evaluation...")
                                eval_results["week2"] = run_week2_eval(
                                    domain_slice, S, chunkings=w2_chunks or ["heading_cap"], progress_cb=_cb
                                )
                            if run_all or eval_scope.startswith("Week 3"):
                                _cb("Starting Week 3: Multi-Agent E2E Evaluation...")
                                eval_results["week3"] = run_week3_eval(
                                    domain_slice, adv_slice, S, use_mock=False,
                                    strategies=w3_strats or ["decomposition"], progress_cb=_cb
                                )
                            if run_all or eval_scope.startswith("Week 4"):
                                _cb("Starting Week 4: Responsible AI Injection Suite...")
                                eval_results["week4"] = run_week4_eval(S, use_mock=False, progress_cb=_cb)

                        model_lbl = S.model if S.has_model else "mock"
                        report_md = generate_week_wise_report(eval_results, int(n_domain), int(n_adv), model_lbl)
                        out_dir = Path("eval_runs")
                        out_dir.mkdir(exist_ok=True)
                        ts_tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
                        report_path = out_dir / f"week_wise_eval_report_{ts_tag}.md"
                        report_path.write_text(report_md, encoding="utf-8")
                        (out_dir / "latest_week_wise_report.md").write_text(report_md, encoding="utf-8")

                        st.session_state["last_eval_results"] = eval_results
                        st.session_state["last_eval_report"] = report_md
                        st.session_state["last_eval_path"] = str(report_path)
                        status_box.update(label="Evaluation complete", state="complete", expanded=False)

            if st.session_state.get("last_eval_results") or st.session_state.get("last_eval_report"):
                with results_placeholder.container():
                    if st.session_state.get("last_eval_results", {}).get("week1"):
                        with st.expander("Strategy Grids & System Prompts (Week 1)", expanded=True):
                            _render_w1_grids(st.session_state["last_eval_results"]["week1"])
                    if st.session_state.get("last_eval_results", {}).get("week2"):
                        with st.expander("Strategy Grids & Retrieval Evaluation (Week 2)", expanded=True):
                            _render_w2_grids(st.session_state["last_eval_results"]["week2"])
                    if st.session_state.get("last_eval_results", {}).get("week3"):
                        with st.expander("Strategy Grids & Multi-Agent E2E (Week 3)", expanded=True):
                            _render_w3_grids(st.session_state["last_eval_results"]["week3"])
                    if st.session_state.get("last_eval_results", {}).get("week4"):
                        with st.expander("Strategy Grids & Injection Defense (Week 4)", expanded=True):
                            _render_w4_grids(st.session_state["last_eval_results"]["week4"])

                    if st.session_state.get("last_eval_report"):
                        st.divider()
                        dl_col1, dl_col2 = st.columns([3, 1], vertical_alignment="center")
                        with dl_col1:
                            st.success(f"Report saved to `{st.session_state.get('last_eval_path', 'eval_runs/latest_week_wise_report.md')}`")
                        with dl_col2:
                            st.download_button(
                                "Download Report (.md)",
                                icon=":material/download:",
                                data=st.session_state["last_eval_report"],
                                file_name="week_wise_eval_report.md",
                                mime="text/markdown",
                                use_container_width=True,
                            )

                        report_text = st.session_state["last_eval_report"]
                        if "<details>" in report_text and "</details>" in report_text:
                            m_det = re.search(
                                r"<details>\s*<summary>.*?</summary>\s*(.*?)\s*</details>",
                                report_text,
                                re.S,
                            )
                            if m_det:
                                before_det = report_text[: m_det.start()].strip()
                                inner_det = m_det.group(1).strip()
                                after_det = report_text[m_det.end() :].strip()
                                if before_det:
                                    st.markdown(before_det)
                                with st.expander("5.2 Week 1 vs Week 3 Detailed Comparison", expanded=False):
                                    st.markdown(inner_det)
                                if after_det:
                                    st.markdown(after_det)
                            else:
                                st.markdown(report_text)
                        else:
                            st.markdown(report_text)

    # ==================================================================
    # Tab 7: Bird's Eye View (sys_admin)
    # ==================================================================
    if tab_birds_eye is not None:
        with tab_birds_eye:
            from hr_policy_navigator.eval_runner import (
                load_eval_datasets,
                run_week1_eval,
                run_week3_eval,
            )

            st.markdown("### Bird's Eye View — Single-Query Week 1 vs Week 3 Comparison")
            st.caption("Write a custom question or pick from Domain / Adversarial sets to compare all Week 1 and Week 3 strategies.")

            be_domain, be_adv = load_eval_datasets()
            with st.container(border=True):
                q_source = st.radio(
                    "Question Source",
                    ["Write Custom Question", "Pick from Domain Queries", "Pick from Adversarial Queries"],
                    horizontal=True,
                    key="be_q_source",
                )

                selected_item: dict = {}
                source_kind = "custom"
                if q_source == "Write Custom Question":
                    custom_q = st.text_input(
                        "Enter your HR policy question",
                        placeholder="e.g. How many PTO days do I get?",
                        key="be_custom_q",
                    )
                    if custom_q.strip():
                        selected_item = {
                            "id": "CUSTOM",
                            "type": "custom",
                            "question": custom_q.strip(),
                            "gold_answer": "",
                            "expected_sources": [],
                            "expected_status": "custom",
                        }
                elif q_source == "Pick from Domain Queries":
                    source_kind = "domain"
                    dom_map = {
                        f"[{it['id']}] ({it.get('theme', '')}) {it['question']}": it
                        for it in be_domain
                    }
                    picked_dom = st.selectbox("Select Domain Query", list(dom_map.keys()), key="be_dom_select")
                    selected_item = dom_map[picked_dom]
                else:
                    source_kind = "adversarial"
                    adv_map = {
                        f"[{it['id']}] ({it.get('type', '')}) {it['question']}": it
                        for it in be_adv
                    }
                    picked_adv = st.selectbox("Select Adversarial Query", list(adv_map.keys()), key="be_adv_select")
                    selected_item = adv_map[picked_adv]

                be_opt1, be_opt2 = st.columns(2)
                with be_opt1:
                    be_w1_strats = st.multiselect(
                        "Week 1 Strategies",
                        ["zero_shot", "structured_fewshot", "chain_of_thought", "decomposition"],
                        default=["zero_shot", "structured_fewshot", "chain_of_thought", "decomposition"],
                        key="be_w1_strats",
                    )
                with be_opt2:
                    be_w3_strats = st.multiselect(
                        "Week 3 Strategies",
                        ["baseline", "structured", "selfconsistency", "decomposition"],
                        default=["baseline", "structured", "selfconsistency", "decomposition"],
                        key="be_w3_strats",
                    )

                be_run_clicked = st.button("Run evaluation", icon=":material/play_arrow:", type="primary", key="btn_birds_eye_run")

            be_status_ph = st.empty()
            be_res_ph = st.empty()

            if be_run_clicked:
                if not selected_item.get("question"):
                    st.warning("Please enter or select a question first.")
                else:
                    st.session_state.pop("birds_eye_result", None)
                    be_res_ph.empty()
                    dom_in = [selected_item] if source_kind in ("domain", "custom") else []
                    adv_in = [selected_item] if source_kind == "adversarial" else []
                    sel_w1 = be_w1_strats or ["zero_shot"]
                    sel_w3 = be_w3_strats or ["decomposition"]

                    with be_status_ph.status("Running Week 1 & Week 3 across selected strategies...", expanded=True) as be_box:
                        be_box.write(f"Running Week 1 ({', '.join(sel_w1)})...")
                        w1_single = run_week1_eval(
                            dom_in, adv_in, S, use_mock=False,
                            strategies=sel_w1,
                            progress_cb=lambda m: be_box.write(m),
                        )
                        be_box.write(f"Running Week 3 ({', '.join(sel_w3)})...")
                        w3_single = run_week3_eval(
                            dom_in, adv_in, S, use_mock=False,
                            strategies=sel_w3,
                            progress_cb=lambda m: be_box.write(m),
                        )
                        st.session_state["birds_eye_result"] = {
                            "item": selected_item,
                            "source_kind": source_kind,
                            "week1": w1_single,
                            "week3": w3_single,
                        }
                        be_box.update(label="Bird's Eye evaluation complete", state="complete", expanded=False)

            if st.session_state.get("birds_eye_result"):
                be_data = st.session_state["birds_eye_result"]
                it = be_data["item"]
                sk = be_data["source_kind"]
                with be_res_ph.container():
                    with st.container(border=True):
                        st.markdown(f"**Question:** {it['question']}")
                        if sk == "domain":
                            st.markdown(f"**ID:** `{it.get('id', '')}` · **Theme:** `{it.get('theme', '—')}`")
                            st.markdown(f"**Gold Answer:** {it.get('gold_answer', '—')}")
                            st.markdown(f"**Expected Sources:** `{', '.join(it.get('expected_sources', [])) or '—'}`")
                        elif sk == "adversarial":
                            st.markdown(
                                f"**ID:** `{it.get('id', '')}` · **Type:** `{it.get('type', '—')}` "
                                f"· **Expected Status:** `{it.get('expected_status', '—')}`"
                            )
                            if it.get("gold_answer"):
                                st.markdown(f"**Gold Answer:** {it['gold_answer']}")
                            if it.get("expected_sources"):
                                st.markdown(f"**Expected Sources:** `{', '.join(it['expected_sources'])}`")

                    be_rows: List[Dict[str, Any]] = []
                    for s_name, s_payload in be_data["week1"].get("strategies", {}).items():
                        for r in s_payload.get("rows", []):
                            be_rows.append({
                                "Week": "Week 1",
                                "Strategy": s_name,
                                "Response": r.get("model_answer", ""),
                                "Accuracy": "—" if sk == "custom" else ("Pass" if r.get("correct") else "Fail"),
                                "ROUGE-LCS": f"{r['rouge_lcs_f1']:.4f}" if (sk != "custom" and r.get("rouge_lcs_f1") is not None) else "—",
                                "Latency (ms)": r.get("latency_ms", 0.0),
                            })
                    for s_name, s_payload in be_data["week3"].get("strategies", {}).items():
                        for r in s_payload.get("rows", []):
                            be_rows.append({
                                "Week": "Week 3",
                                "Strategy": s_name,
                                "Response": r.get("model_answer", ""),
                                "Accuracy": "—" if sk == "custom" else ("Pass" if r.get("correct") else "Fail"),
                                "ROUGE-LCS": f"{r['rouge_lcs_f1']:.4f}" if (sk != "custom" and r.get("rouge_lcs_f1") is not None) else "—",
                                "Latency (ms)": r.get("latency_ms", 0.0),
                            })

                    be_col_styles = {
                        "Week": "white-space:nowrap; font-weight:600;",
                        "Strategy": "white-space:nowrap; font-weight:600; font-family:JetBrains Mono, monospace;",
                        "Response": "min-width:340px; white-space:normal; word-wrap:break-word; overflow-wrap:anywhere;",
                        "Accuracy": "white-space:nowrap;",
                        "ROUGE-LCS": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                        "Latency (ms)": "white-space:nowrap; text-align:right; font-family:JetBrains Mono, monospace;",
                    }
                    _render_wrapped_table(be_rows, be_col_styles, semantic_cols={"Accuracy"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def app() -> None:
    if "user" not in st.session_state:
        render_login()
    else:
        main()


if __name__ == "__main__":
    app()
