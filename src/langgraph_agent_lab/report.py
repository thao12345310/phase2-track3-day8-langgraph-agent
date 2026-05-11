"""Report generation helper."""

from __future__ import annotations

from pathlib import Path

from .metrics import MetricsReport


def render_report(metrics: MetricsReport) -> str:
    """Render a comprehensive lab report from metrics data."""
    # Build scenario results table
    scenario_rows = []
    for m in metrics.scenario_metrics:
        status = "✅" if m.success else "❌"
        scenario_rows.append(
            f"| {m.scenario_id} | {m.expected_route} | {m.actual_route or 'N/A'} "
            f"| {status} | {m.retry_count} | {m.interrupt_count} | {m.latency_ms}ms |"
        )
    scenario_table = "\n".join(scenario_rows)

    # Build state schema table
    state_schema = """| Field | Reducer | Why |
|---|---|---|
| `thread_id` | overwrite | Unique per run, used by checkpointer |
| `scenario_id` | overwrite | Identifies which scenario is running |
| `query` | overwrite | Current user query text |
| `route` | overwrite | Current classified route (simple/tool/risky/error/missing_info) |
| `risk_level` | overwrite | Current risk assessment (low/high/unknown) |
| `attempt` | overwrite | Current retry attempt counter — compared against max_attempts |
| `max_attempts` | overwrite | Maximum retry attempts before dead-letter |
| `final_answer` | overwrite | The response to return to the user |
| `pending_question` | overwrite | Clarification question for missing_info route |
| `proposed_action` | overwrite | Risky action description awaiting approval |
| `approval` | overwrite | Approval decision from HITL node |
| `evaluation_result` | overwrite | Gate for retry loop: 'needs_retry' or 'success' |
| `messages` | **append** (`add`) | Audit trail of messages across all nodes |
| `tool_results` | **append** (`add`) | Accumulated tool execution results |
| `errors` | **append** (`add`) | Accumulated error messages across retries |
| `events` | **append** (`add`) | Primary audit log — used by metrics to count nodes visited |"""

    return f"""# Day 08 Lab Report

## 1. Team / student

- Name: Duong Thi Phuong Thao
- Date: 2026-05-11

## 2. Architecture

The graph implements a support-ticket agent with 11 nodes and conditional routing:

```
START → intake → classify → [conditional routing]
  simple       → answer → finalize → END
  tool         → tool → evaluate → answer → finalize → END
  missing_info → clarify → finalize → END
  risky        → risky_action → approval → tool → evaluate → answer → finalize → END
  error        → retry → tool → evaluate → [retry loop or answer] → finalize → END
  max retry    → retry → dead_letter → finalize → END
```

### Key design decisions:

1. **Keyword-based classifier** with priority: risky > tool > missing_info > error > simple.
   This prevents conflicts when a query contains keywords from multiple categories.

2. **Retry loop** implemented via `evaluate_node` (the "done?" check) and `route_after_evaluate`.
   The loop is bounded by `max_attempts`; when exhausted, routes to `dead_letter`.

3. **HITL approval** for risky actions via `approval_node`. Supports both mock (default) and real
   `interrupt()` mode via `LANGGRAPH_INTERRUPT=true` environment variable.

4. **Append-only audit trail** using LangGraph's `add` reducer for `events`, `messages`,
   `tool_results`, and `errors`. Ensures full observability across node executions.

## 3. State schema

{state_schema}

## 4. Scenario results

- **Total scenarios**: {metrics.total_scenarios}
- **Success rate**: {metrics.success_rate:.2%}
- **Average nodes visited**: {metrics.avg_nodes_visited:.2f}
- **Total retries**: {metrics.total_retries}
- **Total interrupts**: {metrics.total_interrupts}

| Scenario | Expected route | Actual route | Success | Retries | Interrupts | Latency |
|---|---|---|:---:|---:|---:|---:|
{scenario_table}

## 5. Failure analysis

### 1. Retry exhaustion → Dead letter (S07)

When `max_attempts=1`, the retry node immediately hits the bound. `route_after_retry` detects
`attempt >= max_attempts` and routes to `dead_letter_node`, which logs the failure and sets a
final answer indicating manual review is needed. This prevents infinite retry loops.

### 2. Risky action without approval

If `approval_node` returns `approved=False`, `route_after_approval` routes to `clarify` instead
of `tool`. This ensures destructive actions never execute without explicit approval.
In the current lab, mock approval always returns `True`, but the routing logic handles rejection.

### 3. Error route recovery (S05)

The error scenario (`S05_error`) simulates transient tool failures. `tool_node` returns errors for
the first 2 attempts when `route == "error"`. The evaluate → retry loop runs until attempt 2,
when the tool succeeds and `evaluate_node` sets `evaluation_result="success"`, breaking the loop.

## 6. Persistence / recovery evidence

- **MemorySaver**: Used by default for development. Thread ID per run ensures state isolation.
- **SQLite persistence**: Implemented with `SqliteSaver(conn=sqlite3.connect(...))` and WAL mode
  for better concurrent read performance. Fixed the known `from_conn_string()` context manager bug.
- **Thread ID strategy**: Each scenario gets `thread-{{scenario_id}}` for unique state tracking.

## 7. Extension work

- **SQLite persistence**: Full implementation with WAL mode, fixing the known API issue.
- **Graph diagram**: Architecture documented with Mermaid-compatible text representation.

## 8. Improvement plan

If given one more day, the following improvements would be prioritized:

1. **LLM-based classifier**: Replace keyword heuristics with a lightweight LLM call for more
   robust routing that handles edge cases and unseen query patterns.
2. **Real tool integration**: Replace mock tools with actual API calls (e.g., order lookup,
   refund processing) with proper error handling and idempotency keys.
3. **Structured logging**: Add OpenTelemetry tracing to each node for production observability.
4. **Parallel fan-out**: Use `Send()` for concurrent tool execution when multiple tools are needed.
5. **Rate limiting**: Add backoff strategy to retry loops with exponential delay.
"""


def write_report(metrics: MetricsReport, output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_report(metrics), encoding="utf-8")
