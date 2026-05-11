"""Node skeletons for the LangGraph workflow.

Each function should be small, testable, and return a partial state update. Avoid mutating the
input state in place.
"""

from __future__ import annotations

from .state import AgentState, ApprovalDecision, Route, make_event


def intake_node(state: AgentState) -> dict:
    """Normalize raw query into state fields.

    Strips whitespace and records the normalized query for downstream nodes.
    """
    query = state.get("query", "").strip()
    return {
        "query": query,
        "messages": [f"intake:{query[:40]}"],
        "events": [make_event("intake", "completed", "query normalized")],
    }


def classify_node(state: AgentState) -> dict:
    """Classify the query into a route using keyword-based heuristics.

    Priority order (highest to lowest):
    1. RISKY — destructive or external actions requiring approval
    2. TOOL — data lookup or search operations
    3. MISSING_INFO — vague/short queries lacking context
    4. ERROR — system failures or timeouts
    5. SIMPLE — default for general questions
    """
    import re

    query = state.get("query", "").lower()
    # Strip punctuation for clean word boundary matching
    clean_query = re.sub(r'[?!.,;:\'"()]', ' ', query)
    words = clean_query.split()

    route = Route.SIMPLE
    risk_level = "low"

    # Priority 1: RISKY — destructive/external actions
    risky_keywords = {"refund", "delete", "send", "cancel", "remove", "revoke"}
    # Priority 2: TOOL — data lookup operations
    tool_kw = {
        "status", "order", "lookup", "check", "track", "find", "search",
    }
    # Priority 4: ERROR — system failures
    error_kw = {
        "timeout", "fail", "failure", "error", "crash", "unavailable",
    }

    if any(kw in words for kw in risky_keywords):
        route = Route.RISKY
        risk_level = "high"
    elif any(kw in words for kw in tool_kw):
        route = Route.TOOL
    # Priority 3: MISSING_INFO — vague/short queries with pronouns
    elif len(words) < 5 and any(w in words for w in {"it", "this", "that"}):
        route = Route.MISSING_INFO
    elif any(kw in words for kw in error_kw):
        route = Route.ERROR

    return {
        "route": route.value,
        "risk_level": risk_level,
        "events": [make_event("classify", "completed", f"route={route.value}")],
    }


def ask_clarification_node(state: AgentState) -> dict:
    """Ask for missing information instead of hallucinating.

    Generates a clarification question when the query is too vague to route.
    """
    query = state.get("query", "")
    question = (
        f"Your request '{query}' is unclear. Could you provide more "
        "details such as an order ID, account number, or describe "
        "the issue more specifically?"
    )
    return {
        "pending_question": question,
        "final_answer": question,
        "events": [make_event("clarify", "completed", "missing information requested")],
    }


def tool_node(state: AgentState) -> dict:
    """Call a mock tool.

    Simulates transient failures for error-route scenarios to demonstrate retry loops.
    For non-error routes, returns a successful mock result.
    """
    attempt = int(state.get("attempt", 0))
    if state.get("route") == Route.ERROR.value and attempt < 2:
        sid = state.get('scenario_id', 'unknown')
        result = (
            f"ERROR: transient failure attempt={attempt} "
            f"scenario={sid}"
        )
    else:
        result = f"mock-tool-result for scenario={state.get('scenario_id', 'unknown')}"
    return {
        "tool_results": [result],
        "events": [make_event("tool", "completed", f"tool executed attempt={attempt}")],
    }


def risky_action_node(state: AgentState) -> dict:
    """Prepare a risky action for approval.

    Extracts the risky action from the query and prepares it for human review.
    """
    query = state.get("query", "")
    risk = state.get("risk_level", "high")
    proposed = (
        f"Proposed risky action based on request: '{query}'. "
        f"Risk level: {risk}. Approval required before execution."
    )
    return {
        "proposed_action": proposed,
        "events": [make_event(
            "risky_action", "pending_approval",
            "approval required", query=query,
        )],
    }


def approval_node(state: AgentState) -> dict:
    """Human approval step with optional LangGraph interrupt().

    Set LANGGRAPH_INTERRUPT=true to use real interrupt() for HITL demos.
    Default uses mock approval so tests and CI run offline.
    Rejected approvals route to clarification; approved actions proceed to tool execution.
    """
    import os

    if os.getenv("LANGGRAPH_INTERRUPT", "").lower() == "true":
        from langgraph.types import interrupt

        value = interrupt({
            "proposed_action": state.get("proposed_action"),
            "risk_level": state.get("risk_level"),
        })
        if isinstance(value, dict):
            decision = ApprovalDecision(**value)
        else:
            decision = ApprovalDecision(approved=bool(value))
    else:
        decision = ApprovalDecision(approved=True, comment="mock approval for lab")
    return {
        "approval": decision.model_dump(),
        "events": [make_event("approval", "completed", f"approved={decision.approved}")],
    }


def retry_or_fallback_node(state: AgentState) -> dict:
    """Record a retry attempt and increment the attempt counter.

    The routing logic in route_after_retry handles the bound check
    (attempt >= max_attempts → dead_letter). This node simply increments.
    """
    attempt = int(state.get("attempt", 0)) + 1
    errors = [f"transient failure attempt={attempt}"]
    return {
        "attempt": attempt,
        "errors": errors,
        "events": [make_event("retry", "completed", "retry attempt recorded", attempt=attempt)],
    }


def answer_node(state: AgentState) -> dict:
    """Produce a final response grounded in tool results and approval status."""

    tool_results = state.get("tool_results", [])
    approval = state.get("approval")
    query = state.get("query", "")

    if tool_results and approval:
        reviewer = approval.get("reviewer", "reviewer")
        answer = f"Action approved by {reviewer}. Result: {tool_results[-1]}"
    elif tool_results:
        answer = f"I found: {tool_results[-1]}"
    else:
        answer = f"Here is the answer to your question: '{query}' — resolved successfully."
    return {
        "final_answer": answer,
        "events": [make_event("answer", "completed", "answer generated")],
    }


def evaluate_node(state: AgentState) -> dict:
    """Evaluate tool results — the 'done?' check that enables retry loops.

    Checks the latest tool result for ERROR markers. Sets evaluation_result
    to 'needs_retry' (→ retry node) or 'success' (→ answer node).
    """
    tool_results = state.get("tool_results", [])
    latest = tool_results[-1] if tool_results else ""
    if "ERROR" in latest:
        return {
            "evaluation_result": "needs_retry",
            "events": [make_event(
                "evaluate", "completed",
                "tool result indicates failure, retry needed",
            )],
        }
    return {
        "evaluation_result": "success",
        "events": [make_event("evaluate", "completed", "tool result satisfactory")],
    }


def dead_letter_node(state: AgentState) -> dict:
    """Log unresolvable failures for manual review.

    Third layer of error strategy: retry → fallback → dead letter.
    In production, this would persist to a dead-letter queue and alert on-call.
    """
    dead_msg = (
        "Request could not be completed after maximum "
        "retry attempts. Logged for manual review."
    )
    attempt = state.get("attempt", 0)
    return {
        "final_answer": dead_msg,
        "events": [make_event(
            "dead_letter", "completed",
            f"max retries exceeded, attempt={attempt}",
        )],
    }


def finalize_node(state: AgentState) -> dict:
    """Finalize the run and emit a final audit event."""
    return {"events": [make_event("finalize", "completed", "workflow finished")]}
