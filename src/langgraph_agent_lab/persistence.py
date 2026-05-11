"""Checkpointer adapter for LangGraph persistence."""

from __future__ import annotations

import sqlite3


def build_checkpointer(
    kind: str = "memory", database_url: str | None = None,
) -> object | None:
    """Return a LangGraph checkpointer.

    Supported kinds:
    - 'none': No persistence (stateless runs).
    - 'memory': MemorySaver for development/testing.
    - 'sqlite': SqliteSaver with WAL mode for durable persistence.
    - 'postgres': PostgresSaver for production deployments.
    """
    if kind == "none":
        return None
    if kind == "memory":
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()
    if kind == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError as exc:
            raise RuntimeError(
                "SQLite checkpointer requires: "
                "pip install langgraph-checkpoint-sqlite"
            ) from exc
        # Use SqliteSaver(conn=...) — NOT from_conn_string() which returns a context manager in 3.x
        conn = sqlite3.connect(database_url or "checkpoints.db", check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        return SqliteSaver(conn=conn)
    if kind == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError as exc:
            raise RuntimeError(
                "Postgres checkpointer requires: "
                "pip install langgraph-checkpoint-postgres"
            ) from exc
        return PostgresSaver.from_conn_string(database_url or "")
    raise ValueError(f"Unknown checkpointer kind: {kind}")

