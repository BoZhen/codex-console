"""Read Codex's context accounting without estimating tokens or opening a thread."""
import os
from pathlib import Path
import re
import sqlite3
import time


_MARKER = "post sampling token usage"
_FIELD = re.compile(r"\b([a-z_]+)=(Some\(\d+\)|None|[A-Za-z0-9_-]+)")


def parse_context_usage(message):
    if not isinstance(message, str) or _MARKER not in message:
        return None
    fields = dict(_FIELD.findall(message.split(_MARKER, 1)[1]))

    def number(key):
        value = fields.get(key, "")
        if value.startswith("Some("):
            value = value[5:-1]
        return int(value) if value.isascii() and value.isdigit() else None

    tokens = number("total_usage_tokens")
    window = number("full_context_window_limit")
    if tokens is None or not window or tokens >= 2**63 or window >= 2**63:
        return None
    return {
        "source": "codex", "totalTokens": tokens, "maxTokens": window,
        "reportedMaxTokens": window, "percentage": round(tokens * 100.0 / window, 1),
        "turnId": fields.get("turn_id"),
        "autoCompactTokens": number("auto_compact_scope_tokens"),
        "autoCompactLimit": number("auto_compact_scope_limit"),
        "autoCompactScope": fields.get("auto_compact_limit_scope"),
    }


def read_context_usage(thread_id, since=0.0, root=None):
    """Best-effort, indexed, bounded read of this thread's latest core diagnostic.

    The app-server tokenUsage event exposes API usage, not this accounting value.
    The log schema is internal: absent/changed/locked databases must fall back
    cleanly. Never enable extra logging, create a database, or return log text.
    """
    if not thread_id:
        return None
    root = Path(root or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        paths = [p for p in root.glob("logs_*.sqlite")
                 if re.fullmatch(r"logs_\d+\.sqlite", p.name)]
        if not paths:
            return None
        path = max(paths, key=lambda p: int(p.stem.split("_")[-1]))
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.05)
        try:
            deadline = time.monotonic() + .05
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            rows = db.execute("""
                SELECT ts, ts_nanos, feedback_log_body FROM logs
                WHERE thread_id = ? AND ts >= ?
                  AND target = 'codex_core::session::turn'
                  AND instr(feedback_log_body, ?) > 0
                ORDER BY ts DESC, ts_nanos DESC, id DESC LIMIT 1
            """, (thread_id, int(since), _MARKER)).fetchall()
        finally:
            db.close()
    except (OSError, sqlite3.Error, ValueError):
        return None
    if not rows:
        return None
    seconds, nanos, message = rows[0]
    if not isinstance(seconds, int) or not isinstance(nanos, int) or not 0 <= nanos < 1_000_000_000:
        return None
    at = seconds + nanos / 1e9
    if at <= since:
        return None
    sample = parse_context_usage(message)
    if sample:
        sample["sampledAt"] = at
    return sample
