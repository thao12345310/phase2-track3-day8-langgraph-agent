"""Streamlit UI — Real Human-in-the-Loop with LangGraph ``interrupt()``.

Demonstrates ALL routes of the support-ticket agent. Risky queries
*actually pause* at the approval gate and wait for Approve/Reject.

Run with::

    streamlit run scripts/streamlit_hitl.py
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import uuid

import streamlit as st

os.environ["LANGGRAPH_INTERRUPT"] = "true"

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from langgraph_agent_lab.graph import build_graph  # noqa: E402
from langgraph_agent_lab.state import Scenario, initial_state  # noqa: E402

# ── Preset scenarios ──────────────────────────────────────────────────
PRESETS: dict[str, dict] = {
    "🛡️ Risky — Refund customer": {
        "query": "Refund customer #12345 and send confirmation email",
        "route": "risky", "approval": True,
    },
    "🗑️ Risky — Delete account": {
        "query": "Delete customer account after support verification",
        "route": "risky", "approval": True,
    },
    "📧 Risky — Send email": {
        "query": "Send a password reset link to my email",
        "route": "risky", "approval": True,
    },
    "🚫 Risky — Cancel subscription": {
        "query": "Cancel my subscription immediately",
        "route": "risky", "approval": True,
    },
    "🔍 Tool — Order lookup": {
        "query": "Please lookup order status for order 12345",
        "route": "tool", "approval": False,
    },
    "📦 Tool — Track shipment": {
        "query": "Track my shipment number 98765",
        "route": "tool", "approval": False,
    },
    "💬 Simple — Password reset": {
        "query": "How do I reset my password?",
        "route": "simple", "approval": False,
    },
    "🕐 Simple — Business hours": {
        "query": "What are your business hours?",
        "route": "simple", "approval": False,
    },
    "❓ Missing info — Vague": {
        "query": "Can you fix it?",
        "route": "missing_info", "approval": False,
    },
    "⚠️ Error — Timeout": {
        "query": "Timeout failure while processing request",
        "route": "error", "approval": False,
    },
    "💀 Error — Dead letter": {
        "query": "System failure cannot recover after multiple attempts",
        "route": "error", "approval": False, "max_attempts": 1,
    },
}

# ── Page config ───────────────────────────────────────────────────────
st.set_page_config(
    page_title="LangGraph Agent Demo",
    page_icon="🛡️",
    layout="wide",
)

# ── CSS ───────────────────────────────────────────────────────────────
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700;800&display=swap');
html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
.main .block-container { padding-top: 1.5rem; max-width: 1100px; }

.hero {
    background: linear-gradient(135deg, #1e1b4b 0%, #312e81 40%, #4338ca 100%);
    border-radius: 16px; padding: 1.8rem 2.2rem; margin-bottom: 1.2rem;
    color: white; box-shadow: 0 8px 32px rgba(67,56,202,.25);
}
.hero h1 { margin:0 0 .2rem; font-size:1.8rem; font-weight:800; }
.hero p { margin:0; opacity:.82; font-size:.95rem; }

.scard {
    border-radius: 14px; padding: 1.2rem 1.4rem; margin-bottom: .8rem;
    border: 1px solid rgba(0,0,0,.06);
}
.frozen { background: linear-gradient(135deg,#fef3c7,#fde68a); border-color:#f59e0b; }
.ok     { background: linear-gradient(135deg,#d1fae5,#a7f3d0); border-color:#10b981; }
.nok    { background: linear-gradient(135deg,#fee2e2,#fecaca); border-color:#ef4444; }
.idle   { background: linear-gradient(135deg,#f0f9ff,#e0f2fe); border-color:#38bdf8; }
.done   { background: linear-gradient(135deg,#ede9fe,#ddd6fe); border-color:#8b5cf6; }

.tl { display:flex; flex-wrap:wrap; gap:.4rem; margin:.6rem 0; }
.tl span {
    padding:.3rem .65rem; border-radius:999px;
    font-size:.78rem; font-weight:600;
}
.tl .d { background:#c7d2fe; color:#3730a3; }
.tl .p { background:#fbbf24; color:#78350f; animation: pg 1.8s infinite; }
.tl .u { background:#e5e7eb; color:#6b7280; }
@keyframes pg { 0%,100%{box-shadow:0 0 0 #fbbf24} 50%{box-shadow:0 0 12px #fbbf24} }

div.stButton>button { border-radius:10px; font-weight:600; }
</style>
""", unsafe_allow_html=True)


# ── Helpers ───────────────────────────────────────────────────────────
@st.cache_resource
def _db_path() -> str:
    return os.path.join(tempfile.mkdtemp(prefix="hitl_st_"), "hitl.db")


def _graph():
    conn = sqlite3.connect(_db_path(), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return build_graph(checkpointer=SqliteSaver(conn=conn))


def _chips(events, paused_at=None):
    nodes = [e.get("node", "?") for e in events]
    h = "".join(f'<span class="d">✅ {n}</span>' for n in nodes)
    if paused_at:
        h += f'<span class="p">⏸️ {paused_at}</span>'
    return f'<div class="tl">{h}</div>'


# ── Session state ─────────────────────────────────────────────────────
for k, v in {"phase": "idle", "paused_state": None, "final_state": None,
             "run_config": None, "decision": None, "elapsed": 0,
             "reviewer": "Alice", "is_risky": False}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ── Header ────────────────────────────────────────────────────────────
st.markdown("""
<div class="hero">
    <h1>🛡️ LangGraph Agent — Interactive Demo</h1>
    <p>Choose any scenario. Risky queries <b>freeze</b> at the approval gate; others run straight through.</p>
</div>
""", unsafe_allow_html=True)

# ── Sidebar ───────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 🎯 Choose Scenario")
    preset_name = st.selectbox("Preset", list(PRESETS.keys()), index=0)
    preset = PRESETS[preset_name]

    query = st.text_area("Query", value=preset["query"], height=70)
    expected = preset["route"]
    needs_approval = preset.get("approval", False)
    max_attempts = preset.get("max_attempts", 3)

    route_colors = {
        "risky": "🔴", "tool": "🔵", "simple": "🟢",
        "missing_info": "🟡", "error": "🟠",
    }
    st.markdown(f"**Expected route:** {route_colors.get(expected, '⚪')} `{expected}`")
    if needs_approval:
        st.markdown("**⏸️ Will pause for approval**")
    else:
        st.markdown("**▶️ Runs straight through**")

    st.markdown("---")
    st.session_state.reviewer = st.text_input("Reviewer", value="Alice")

    st.markdown("---")
    st.markdown("### 📋 Route Guide")
    st.markdown("""
| Icon | Route | Behavior |
|------|-------|----------|
| 🟢 | simple | Direct answer |
| 🔵 | tool | Tool → evaluate → answer |
| 🟡 | missing | Ask clarification |
| 🔴 | risky | **Pauses** for approval |
| 🟠 | error | Retry loop |
""")

# ── Main area ─────────────────────────────────────────────────────────
col_l, col_r = st.columns([3, 2])

with col_l:
    # ── IDLE ──────────────────────────────────────────────────────────
    if st.session_state.phase == "idle":
        st.markdown(f"""
<div class="scard idle">
    <b>🔵 Ready</b> — Select a scenario and click Submit.
</div>""", unsafe_allow_html=True)

        if st.button("▶️  Submit Request", type="primary", use_container_width=True):
            graph = _graph()
            tid = f"hitl-{uuid.uuid4().hex[:8]}"
            scenario = Scenario(
                id=f"ST_{tid}", query=query,
                expected_route=expected, requires_approval=needs_approval,
                max_attempts=max_attempts,
            )
            state = initial_state(scenario)
            state["thread_id"] = tid
            cfg = {"configurable": {"thread_id": tid}}

            t0 = time.perf_counter()
            result = graph.invoke(state, config=cfg)
            ms = round((time.perf_counter() - t0) * 1000)

            interrupt = result.get("__interrupt__")
            if interrupt:
                st.session_state.phase = "paused"
                st.session_state.paused_state = result
                st.session_state.is_risky = True
            else:
                st.session_state.phase = "done"
                st.session_state.final_state = result
                st.session_state.is_risky = False

            st.session_state.run_config = cfg
            st.session_state.elapsed = ms
            st.rerun()

    # ── PAUSED (risky) ────────────────────────────────────────────────
    elif st.session_state.phase == "paused":
        p = st.session_state.paused_state
        st.markdown(f"""
<div class="scard frozen">
    <b>⏸️ Graph FROZEN</b> — Waiting for human approval ({st.session_state.elapsed}ms to pause)
</div>""", unsafe_allow_html=True)

        st.markdown(_chips(p.get("events", []), "approval"), unsafe_allow_html=True)

        proposed = p.get("proposed_action", "N/A")
        risk = p.get("risk_level", "unknown")
        rc = "#ef4444" if risk == "high" else "#f59e0b"
        st.markdown(f"""
<div style="background:#fafafa;border-radius:10px;padding:1rem 1.2rem;
    border-left:4px solid {rc};margin:.5rem 0 1rem">
    <b>🎯 Proposed Action</b><br>
    <span style="font-size:.92rem">{proposed}</span><br>
    <span style="background:{rc};color:white;padding:.15rem .5rem;
        border-radius:999px;font-size:.75rem;font-weight:600;margin-top:.4rem;display:inline-block">
        ⚠️ {risk.upper()}
    </span>
</div>""", unsafe_allow_html=True)

        comment = st.text_input("💬 Comment", placeholder="Optional reviewer comment")
        c1, c2 = st.columns(2)
        approve = c1.button("✅ Approve", type="primary", use_container_width=True)
        reject = c2.button("❌ Reject", use_container_width=True)

        if approve or reject:
            decision = {
                "approved": approve,
                "reviewer": st.session_state.reviewer,
                "comment": comment or ("Approved" if approve else "Rejected"),
            }
            graph = _graph()
            final = graph.invoke(Command(resume=decision), config=st.session_state.run_config)
            st.session_state.phase = "done"
            st.session_state.final_state = final
            st.session_state.decision = decision
            st.rerun()

    # ── DONE ──────────────────────────────────────────────────────────
    elif st.session_state.phase == "done":
        f = st.session_state.final_state
        d = st.session_state.decision
        is_risky = st.session_state.is_risky

        if is_risky and d:
            cls = "ok" if d["approved"] else "nok"
            icon = "✅" if d["approved"] else "❌"
            word = "Approved" if d["approved"] else "Rejected"
            st.markdown(f"""
<div class="scard {cls}">
    <b>{icon} {word}</b> by {d.get('reviewer','?')}
    {f' — "{d.get("comment","")}"' if d.get("comment") else ""}
</div>""", unsafe_allow_html=True)
        else:
            route = f.get("route", "?")
            st.markdown(f"""
<div class="scard done">
    <b>✅ Completed</b> — Route: <code>{route}</code> ({st.session_state.elapsed}ms)
</div>""", unsafe_allow_html=True)

        st.markdown(_chips(f.get("events", [])), unsafe_allow_html=True)

        st.markdown("#### 💬 Agent Response")
        answer = f.get("final_answer") or f.get("pending_question") or "No answer"
        st.info(answer, icon="🤖")

        path = " → ".join(e.get("node", "?") for e in f.get("events", []))
        st.markdown(f"**Path:** `{path}`")

        if f.get("tool_results"):
            st.markdown("**Tool results:**")
            for r in f["tool_results"]:
                st.code(r)

        if f.get("errors"):
            st.markdown("**Errors (retries):**")
            for e in f["errors"]:
                st.warning(e)

        st.markdown("---")
        if st.button("🔄 New Request", type="primary", use_container_width=True):
            for k in ["phase", "paused_state", "final_state", "run_config", "decision"]:
                st.session_state[k] = "idle" if k == "phase" else None
            st.session_state.is_risky = False
            st.rerun()

# ── Right column: state inspector ─────────────────────────────────────
with col_r:
    st.markdown("### 🔍 State Inspector")

    if st.session_state.phase == "idle":
        st.caption("Submit a request to inspect state.")

    elif st.session_state.phase == "paused":
        p = st.session_state.paused_state
        try:
            graph = _graph()
            snap = graph.get_state(st.session_state.run_config)
            st.markdown("**Checkpoint:**")
            st.json({
                "next": list(snap.next),
                "route": snap.values.get("route"),
                "risk_level": snap.values.get("risk_level"),
                "proposed_action": (snap.values.get("proposed_action") or "")[:80] + "…",
            })
        except Exception:
            pass

        irq = p.get("__interrupt__")
        if irq:
            st.markdown("**Interrupt payload:**")
            st.json(irq)

        with st.expander("📦 Raw state"):
            st.json({k: v for k, v in p.items() if k != "__interrupt__"})

    elif st.session_state.phase == "done":
        f = st.session_state.final_state
        if st.session_state.decision:
            st.markdown("**Decision:**")
            st.json(st.session_state.decision)

        st.markdown("**Summary:**")
        st.json({
            "route": f.get("route"),
            "approval": f.get("approval"),
            "answer": (f.get("final_answer") or "")[:120],
            "tool_results": f.get("tool_results", []),
            "errors": f.get("errors", []),
            "nodes": [e.get("node") for e in f.get("events", [])],
        })

        with st.expander("📦 Raw state"):
            st.json(f)
