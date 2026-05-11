"""Bonus 2 — Real Human-in-the-Loop with LangGraph's ``interrupt()``.

The lab's ``approval_node`` already wires both modes:

  * ``LANGGRAPH_INTERRUPT`` unset → mock approval (used by tests/CI).
  * ``LANGGRAPH_INTERRUPT=true`` → calls ``interrupt({...})`` which pauses the
    graph until a caller supplies a value via ``Command(resume=...)``.

A real ``interrupt()`` requires a checkpointer because LangGraph needs to
persist state before it can wake up later on a separate invocation. This
script demonstrates the full cycle end to end:

  1. Build graph with ``SqliteSaver`` so the pause is durable.
  2. Invoke a **risky** scenario — graph stops at ``approval_node`` and
     returns control to us with an ``__interrupt__`` marker in the snapshot.
  3. Inspect ``graph.get_state(thread)`` to read the payload the human
     reviewer would see (proposed action + risk level).
  4. Resume twice with two different decisions to prove the gate works:
       - Thread A: approve → graph continues to ``tool → evaluate → answer``.
       - Thread B: reject → graph branches to ``clarify`` instead.

Outputs:
  - ``outputs/hitl_interrupt.db``   SQLite checkpoint file (kept as evidence).
  - ``outputs/hitl_interrupt.log``  Human-readable log of the pause/resume flow.

Run with::

    LANGGRAPH_INTERRUPT=true python scripts/bonus_hitl_interrupt.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from textwrap import indent

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from langgraph_agent_lab.graph import build_graph
from langgraph_agent_lab.state import Scenario, initial_state


def banner(title: str) -> str:
    return f"\n{'=' * 70}\n{title}\n{'=' * 70}"


def fmt(label: str, payload: object) -> str:
    return f"{label}:\n" + indent(json.dumps(payload, indent=2, default=str), "  ")


def run_one(
    *,
    thread_label: str,
    decision: dict,
    db_path: Path,
    log_lines: list[str],
) -> None:
    """Run one risky scenario, pause at approval, resume with ``decision``."""
    # Force the approval_node into real-interrupt mode for this run.
    os.environ["LANGGRAPH_INTERRUPT"] = "true"

    # Open a fresh sqlite connection per run so we get an isolated thread.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    checkpointer = SqliteSaver(conn=conn)
    graph = build_graph(checkpointer=checkpointer)

    scenario = Scenario(
        id=f"BONUS_HITL_{thread_label}",
        query="Refund customer 12345 and send confirmation email",
        expected_route="risky",
        requires_approval=True,
    )
    state = initial_state(scenario)
    # One thread_id per "ticket" so the checkpointer can resume it later.
    state["thread_id"] = f"hitl-{thread_label}"
    run_config = {"configurable": {"thread_id": state["thread_id"]}}

    log_lines.append(banner(f"Thread {thread_label} — first invoke (expect pause)"))
    log_lines.append(fmt("initial_state.query", state["query"]))

    # First invoke — graph runs until it hits interrupt() in approval_node,
    # then yields control back to us. The returned dict contains an
    # __interrupt__ key with the payload the reviewer would inspect.
    paused_state = graph.invoke(state, config=run_config)
    interrupt_marker = paused_state.get("__interrupt__")
    log_lines.append(fmt("paused_state.__interrupt__", interrupt_marker))
    log_lines.append(
        fmt(
            "paused_state (excerpt)",
            {
                "route": paused_state.get("route"),
                "risk_level": paused_state.get("risk_level"),
                "proposed_action": paused_state.get("proposed_action"),
                "final_answer": paused_state.get("final_answer"),
                "events_so_far": [e.get("node") for e in paused_state.get("events", [])],
            },
        )
    )

    # The snapshot in the checkpointer confirms we paused at `approval`.
    snapshot = graph.get_state(run_config)
    log_lines.append(
        fmt(
            "checkpoint snapshot",
            {
                "next": list(snapshot.next),
                "pending_writes_count": len(getattr(snapshot, "pending_writes", []) or []),
                "values.route": snapshot.values.get("route"),
                "values.proposed_action": snapshot.values.get("proposed_action"),
            },
        )
    )

    log_lines.append(banner(f"Thread {thread_label} — resume with decision"))
    log_lines.append(fmt("reviewer decision", decision))

    # Second invoke — Command(resume=...) feeds the value back into the
    # original interrupt() call site. The graph wakes up and finishes.
    final_state = graph.invoke(Command(resume=decision), config=run_config)
    log_lines.append(
        fmt(
            "final_state (excerpt)",
            {
                "route": final_state.get("route"),
                "approval": final_state.get("approval"),
                "final_answer": final_state.get("final_answer"),
                "pending_question": final_state.get("pending_question"),
                "tool_results": final_state.get("tool_results"),
                "events": [e.get("node") for e in final_state.get("events", [])],
            },
        )
    )

    conn.close()


def main() -> None:
    out_dir = Path(__file__).resolve().parents[1] / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    # SQLite can fail with "disk I/O error" on some mounted/networked
    # filesystems (e.g. Cowork sandbox mounts). Use a temp dir for the DB
    # itself; the log file — our actual evidence — still lives in outputs/.
    db_dir = Path(tempfile.mkdtemp(prefix="hitl_ckpt_"))
    db_path = db_dir / "hitl_interrupt.db"
    log_path = out_dir / "hitl_interrupt.log"

    log_lines: list[str] = [
        "Real HITL demo — graph pauses at approval_node, resumes via Command(resume=...).",
        f"Checkpointer: SqliteSaver(db={db_path.name}) with WAL mode.",
    ]

    # Thread A — reviewer approves. Expect: tool runs, answer is produced.
    run_one(
        thread_label="approve",
        decision={"approved": True, "reviewer": "alice", "comment": "verified ID"},
        db_path=db_path,
        log_lines=log_lines,
    )

    # Thread B — reviewer rejects. Expect: graph branches to clarify.
    run_one(
        thread_label="reject",
        decision={"approved": False, "reviewer": "bob", "comment": "need manager OK"},
        db_path=db_path,
        log_lines=log_lines,
    )

    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    print("\n".join(log_lines))
    print(f"\n[bonus_hitl] Wrote log to {log_path}")
    print(f"[bonus_hitl] SQLite checkpoint kept at {db_path}")


if __name__ == "__main__":
    main()
