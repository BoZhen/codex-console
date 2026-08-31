#!/usr/bin/env python3
"""Codex Console — an interactive browser GUI that drives OpenAI's Codex CLI
while keeping the *discussion* and the *code/file changes* visually separated.

It runs the real `codex` CLI through its `codex app-server` JSON-RPC protocol
(one persistent codex *thread* per chat), renders the typed event stream, shows
code/file changes as collapsed cards plus a live `git diff` drawer, and supports
per-action approval (exec / patch) and in-band tool user-input questions.

Env (legacy CLAUDE_CONSOLE_* names still accepted as a fallback):
  CODEX_CONSOLE_PORT   listen port (default 7704)
  CODEX_CONSOLE_BIND   bind address (default 127.0.0.1; set 0.0.0.0 for LAN)
  CODEX_CONSOLE_AUTH   optional HTTP Basic Auth "user:pass" (default disabled)
  CODEX_CONSOLE_WEBFM_URL  optional web-file-manager base URL for file links
"""

import asyncio
import base64
import glob
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

import tornado.ioloop
import tornado.iostream
import tornado.process
import tornado.web
import tornado.websocket

# This build drives the real `codex` CLI through its `codex app-server` JSON-RPC
# protocol (the same backend editor integrations use). No Python SDK needed —
# just the `codex` binary on PATH.

def _env(name, default=""):
    """Read CODEX_CONSOLE_<name>. (Deliberately does NOT fall back to
    CLAUDE_CONSOLE_*; this keeps service configuration isolated.)"""
    return os.environ.get("CODEX_CONSOLE_" + name, default)

PORT = int(_env("PORT", "7704"))
AUTH = _env("AUTH", "")
# Default to loopback: this serves ALL your agent transcripts + home-wide git
# diffs, so it must not land on the network by accident. Set CODEX_CONSOLE_BIND=
# 0.0.0.0 (ideally with CODEX_CONSOLE_AUTH) to reach it from another device.
BIND = _env("BIND", "127.0.0.1")
WEBFM_URL = _env("WEBFM_URL", "").rstrip("/")
HOME = os.path.expanduser("~")
CODEX_ROOT = os.path.join(HOME, ".codex", "sessions")
CLAUDE_ROOT = os.path.join(HOME, ".claude", "projects")   # only for cross-reads, unused in v1
# Interactive console drives the real `codex` CLI via `codex app-server`.
# Prefer the NATIVE binary over the npm node-wrapper (`~/.local/bin/codex` →
# node `codex.js`): the wrapper needs node + its platform package resolvable from
# the spawn context, which breaks when the server (with a minimal PATH) launches
# it. The native ELF binary has no such dependency and starts faster.
def _is_elf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except Exception:
        return False

def _resolve_codex_bin():
    ov = _env("CODEX")
    if ov and os.path.exists(ov):
        return ov
    # canonical npm-install vendor path for the native binary (matches the
    # version of the user's `codex` CLI), across fnm / npm-global / system roots
    roots = [os.path.join(HOME, ".fnm", "node-versions", "*", "installation", "lib",
                          "node_modules"),
             os.path.join(HOME, ".local", "lib", "node_modules"),
             "/usr/lib/node_modules", "/usr/local/lib/node_modules"]
    cands = []
    for r in roots:
        for sub in ("codex/node_modules/@openai/codex-linux-x64/vendor/*/bin/codex",
                    "codex/node_modules/@openai/codex-linux-x64/vendor/*/codex/codex"):
            cands += glob.glob(os.path.join(r, "@openai", sub))
    # newest first (most recently installed) — prefer a real ELF that's runnable
    cands.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    for c in cands:
        if _is_elf(c) and os.access(c, os.X_OK):
            return c
    # fall back to whatever `codex` resolves to (may be the node wrapper)
    return shutil.which("codex") or os.path.expanduser("~/.local/bin/codex")

CODEX_BIN = _resolve_codex_bin()
HAVE_CODEX = bool(CODEX_BIN and os.path.exists(CODEX_BIN))

CAP = 12000          # cap per long string field sent to the browser
RESULT_CAP = 6000    # cap per tool_result body
# asyncio StreamReader's default line limit is 64KB. The codex app-server frames
# each JSON-RPC message as one newline-delimited line, and a single notification
# (e.g. an item/completed carrying a big command output, file read, or diff) can
# far exceed that — which would make readline() raise and (previously) kill the
# session even though the app-server was still alive. Give it generous headroom.
APPSERVER_STREAM_LIMIT = 64 * 1024 * 1024   # 64 MB per app-server line
# upload ceiling for /api/import; large rollout files can exceed Tornado's default
IMPORT_MAX = int(_env("IMPORT_MAX_MB", "1024") or "1024") * 1024 * 1024
POLL_MS = 800        # transcript tail interval

# ── approval presets exposed in the single "mode" picker → (approvalPolicy, sandbox) ──
# approvalPolicy ∈ untrusted|on-failure|on-request|never ; sandbox ∈ read-only|workspace-write|danger-full-access
MODE_PRESETS = {
    "on-request":  ("on-request", "workspace-write"),   # 🔐 prompt before risky steps (default)
    "auto":        ("never",      "workspace-write"),    # ⚡ run in the workspace sandbox, no prompts
    "read-only":   ("on-request", "read-only"),          # 👁 read-only sandbox
    "full-access": ("never",      "danger-full-access"), # 🔓 no sandbox, no prompts (careful)
}
def _mode_policy(mode):
    return MODE_PRESETS.get(mode, MODE_PRESETS["on-request"])

def _sandbox_policy(sbx):
    """SandboxPolicy object for turn/start (camelCase tag form)."""
    if sbx == "danger-full-access":
        return {"type": "dangerFullAccess"}
    if sbx == "read-only":
        return {"type": "readOnly", "networkAccess": False}
    return {"type": "workspaceWrite", "networkAccess": False, "excludeSlashTmp": False,
            "excludeTmpdirEnvVar": False, "writableRoots": []}

def _summarize_changes(changes):
    """Compact text view of a fileChange item's `changes` for the tool card."""
    if not changes:
        return ""
    if isinstance(changes, str):
        return changes
    out = []
    if isinstance(changes, dict):
        changes = [changes]
    if isinstance(changes, list):
        for c in changes:
            if isinstance(c, dict):
                path = c.get("path") or c.get("file") or c.get("absolutePath") or ""
                kind = c.get("kind") or c.get("type") or c.get("change") or ""
                out.append(("%s %s" % (kind, path)).strip() or json.dumps(c)[:200])
            else:
                out.append(str(c))
    return "\n".join(out) if out else _txt(changes)

# rolling usage limits, fed by codex `account/rateLimits/*` (primary=5h, secondary=weekly)
_CODEX_USAGE = {}
def _fmt_usage(rl):
    """Normalize a codex rateLimits payload → {five_hour, seven_day}. The reset
    time is converted to epoch MILLIS (codex reports seconds; the browser's
    new Date() expects millis)."""
    rl = rl or {}
    if isinstance(rl.get("rateLimits"), dict):     # tolerate a wrapped read-response
        rl = rl["rateLimits"]
    def win(d):
        if not isinstance(d, dict) or d.get("usedPercent") is None:
            return None
        ra = d.get("resetsAt")
        return {"utilization": d.get("usedPercent"),
                "resets_at": int(ra) * 1000 if isinstance(ra, (int, float)) else None,
                "window_minutes": d.get("windowDurationMins")}
    out = {}
    p, s = win(rl.get("primary")), win(rl.get("secondary"))
    if p:
        out["five_hour"] = p
    if s:
        out["seven_day"] = s
    return out

def _set_usage(rl):
    global _CODEX_USAGE
    out = _fmt_usage(rl)
    if out:
        _CODEX_USAGE = out
    return out


# Live model catalog from the same Codex app-server protocol used for sessions.
# A short last-known-good cache avoids spawning a probe for every browser while
# still allowing newly released models to appear without a console deployment.
_models_cache = {"success_t": 0.0, "attempt_t": 0.0, "v": None}
_models_lock = threading.Lock()
MODEL_CACHE_SEC = 300
MODEL_RETRY_SEC = 30


def _rpc_probe_response(messages, request_id, deadline):
    """Wait for one response while ignoring interleaved server notifications."""
    while time.monotonic() < deadline:
        try:
            line = messages.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            break
        if line is None:
            break
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("id") != request_id:
            continue
        if msg.get("error"):
            raise RuntimeError("codex app-server request failed: %s" % msg["error"])
        return msg.get("result") or {}
    raise TimeoutError("timed out reading Codex model catalog")


def _probe_codex_models(timeout=12):
    """Ask the installed Codex CLI for its visible model catalog.

    This intentionally goes through app-server `model/list` rather than a
    separately maintained OpenAI model list: the result respects the installed
    CLI, account access, configuration, and staged model rollouts.
    """
    if not CODEX_BIN or not os.path.exists(CODEX_BIN):
        return None
    proc = subprocess.Popen(
        [CODEX_BIN, "app-server"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd=HOME, text=True, bufsize=1)
    messages = queue.Queue()

    def read_stdout():
        try:
            for line in proc.stdout:
                messages.put(line)
        finally:
            messages.put(None)

    reader = threading.Thread(
        target=read_stdout, name="codex-model-catalog", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    request_id = 1

    def send(method, params=None, notify=False):
        nonlocal request_id
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        rid = None
        if not notify:
            rid = request_id
            request_id += 1
            msg["id"] = rid
        proc.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
        proc.stdin.flush()
        return rid

    try:
        rid = send("initialize", {"clientInfo": {
            "name": "codex-console", "version": "0.1.0", "title": "Codex Console"}})
        _rpc_probe_response(messages, rid, deadline)
        send("initialized", notify=True)

        raw_models, seen = [], set()
        cursor = None
        for _ in range(20):  # defensive cap against a broken repeating cursor
            params = {"includeHidden": False, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            rid = send("model/list", params)
            page = _rpc_probe_response(messages, rid, deadline)
            for model in page.get("data") or []:
                if not isinstance(model, dict) or model.get("hidden"):
                    continue
                slug = str(model.get("model") or model.get("id") or "").strip()
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                efforts = []
                for effort in model.get("supportedReasoningEfforts") or []:
                    if isinstance(effort, dict) and effort.get("reasoningEffort"):
                        efforts.append(effort["reasoningEffort"])
                raw_models.append({
                    "id": slug,
                    "name": model.get("displayName") or slug,
                    "description": model.get("description") or "",
                    "isDefault": bool(model.get("isDefault")),
                    "reasoningEfforts": efforts,
                    "defaultReasoningEffort": model.get("defaultReasoningEffort") or "",
                })
            cursor = page.get("nextCursor")
            if not cursor:
                break
        return raw_models or None
    finally:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        reader.join(timeout=1)
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass


def fetch_models(force=False):
    """Return the current visible Codex catalog, or the last successful one."""
    now = time.monotonic()
    if (not force and _models_cache["v"] is not None
            and now - _models_cache["success_t"] < MODEL_CACHE_SEC):
        return _models_cache["v"]
    if (not force and _models_cache["attempt_t"]
            and now - _models_cache["attempt_t"] < MODEL_RETRY_SEC):
        return _models_cache["v"]
    # Do not tie up Tornado's shared executor behind a slow catalog probe. One
    # caller refreshes; concurrent callers immediately receive the LKG (or []).
    if not _models_lock.acquire(blocking=False):
        return _models_cache["v"]
    try:
        now = time.monotonic()
        if (not force and _models_cache["v"] is not None
                and now - _models_cache["success_t"] < MODEL_CACHE_SEC):
            return _models_cache["v"]
        if (not force and _models_cache["attempt_t"]
                and now - _models_cache["attempt_t"] < MODEL_RETRY_SEC):
            return _models_cache["v"]
        _models_cache["attempt_t"] = now
        try:
            models = _probe_codex_models()
            if models:
                _models_cache["success_t"] = time.monotonic()
                _models_cache["v"] = models
        except Exception:
            pass
        _models_cache["attempt_t"] = time.monotonic()
        return _models_cache["v"]
    finally:
        _models_lock.release()

# codex runs everything through the shell. Unwrap its `bash -lc '<inner>'` wrapper
# for display, and label a plain single-file read as a Read so the cards aren't all
# an indistinguishable "shell".
_WRAP_RE = re.compile(r"""^\S*(?:bash|sh|zsh|dash)\s+-l?c\s+(['"])(.*)\1\s*$""", re.S)
_READ_RE = re.compile(r"^\s*(?:cat|head|tail|bat|less|more|nl)\s")
_SED_READ_RE = re.compile(r"""^\s*sed\s+-n\s+(['"])?\d+(?:,\d+)?p\1\s+\S""")
_READ_SLICE_PIPE_RE = re.compile(
    r"""^\s*(?:cat|nl)\b[^|;&<>`$]*\|\s*sed\s+-n\s+(['"])?\d+(?:,\d+)?p\1\s*$""")
_CONFIG_MODEL_RE = re.compile(r"^\s*model\s*=\s*(['\"])(.*?)\1\s*(?:#.*)?$")
_CONFIG_CONTEXT_RE = re.compile(r"^\s*model_context_window\s*=\s*([0-9_]+)\s*(?:#.*)?$")
def _codex_is_read_cmd(command):
    c = _shell_command_text(command).strip()
    if not c:
        return False
    if "&&" in c:
        parts = [p.strip() for p in re.split(r"\s*&&\s*", c)]
        return all(parts) and all(_codex_is_read_cmd(p) for p in parts)
    if _READ_SLICE_PIPE_RE.match(c):
        return True
    return bool((_READ_RE.match(c) or _SED_READ_RE.match(c))
                and not re.search(r"[|;&]|\$\(|`|>|<", c))

def _shell_words(command):
    if isinstance(command, list):
        return [str(x) for x in command]
    return None


def _shell_command_text(command):
    words = _shell_words(command)
    if not words:
        return _txt(command)
    if (len(words) >= 3 and os.path.basename(words[0]) in ("bash", "sh", "zsh", "dash")
            and words[1] in ("-c", "-lc")):
        return " ".join(words[2:])
    return " ".join(shlex.quote(w) for w in words)


def _codex_cmd(command, actions=None):
    """→ (tool, shown_command). Prefer app-server commandActions when present.

    Modern app-server already parses shell commands into semantic actions. Use that
    instead of guessing from raw pipes whenever possible, then fall back to the
    older regex classifier for restored/legacy transcript events.
    """
    action_list = actions if isinstance(actions, list) else []
    action_types = [a.get("type") for a in action_list if isinstance(a, dict)]
    first = next((a for a in action_list if isinstance(a, dict)), None)
    c = _shell_command_text(command).strip()
    m = _WRAP_RE.match(c)
    inner = (m.group(2) if m else c).strip()
    if action_types and all(t == "read" for t in action_types):
        shown = first.get("path") or first.get("name") or first.get("command") or inner
        return "Read", _shell_command_text(shown)
    if action_types and all(t == "listFiles" for t in action_types):
        shown = first.get("path") or first.get("command") or inner
        return "List", _shell_command_text(shown)
    if action_types and all(t == "search" for t in action_types):
        shown = first.get("query") or first.get("path") or first.get("command") or inner
        return "Search", _shell_command_text(shown)
    if _codex_is_read_cmd(inner):
        return "Read", inner
    return "Bash", inner


def _codex_exec_event(base, args, call_id):
    args = args if isinstance(args, dict) else {}
    cmd = _txt(args.get("cmd") or args.get("command"))
    tool, shown = _codex_cmd(cmd)
    return {**base, "kind": "tool_use", "tool": tool,
            "input": {"command": _cap(shown), "cwd": args.get("workdir") or args.get("cwd")},
            "toolId": call_id or ""}


def _change_kind(kind):
    if isinstance(kind, dict):
        return kind.get("type") or kind.get("kind") or "update"
    return kind or "update"


def _file_change_rows(changes):
    if isinstance(changes, dict):
        changes = [changes]
    rows = []
    for ch in changes or []:
        if not isinstance(ch, dict):
            continue
        rows.append({
            "path": ch.get("path") or ch.get("file") or ch.get("absolutePath") or "",
            "diff": ch.get("diff") or ch.get("unified_diff") or ch.get("unifiedDiff") or "",
            "kind": _change_kind(ch.get("kind") or ch.get("type") or ch.get("change")),
        })
    return rows


def _file_change_input(changes):
    files = _file_change_rows(changes)
    if len(files) == 1:
        fp, body, kind = files[0]["path"], files[0]["diff"], files[0]["kind"]
    elif files:
        fp, kind = "%d files" % len(files), "edit"
        body = "\n".join("--- %s (%s) ---\n%s" % (f["path"], f["kind"], f["diff"])
                         for f in files)
    else:
        fp, body, kind = "apply_patch", "", "edit"
    return {"file_path": fp, "diff": _cap(body, RESULT_CAP), "kind": kind, "n": len(files)}


def _status_type(status):
    if isinstance(status, dict):
        return status.get("type") or ""
    return _txt(status)


def _active_flags(status):
    if isinstance(status, dict):
        return status.get("activeFlags") or []
    return []


def _decode_b64_text(value):
    if not value:
        return ""
    try:
        return base64.b64decode(value).decode("utf-8", "replace")
    except Exception:
        return ""


def _web_action_type(action):
    raw = _txt((action or {}).get("type")).strip()
    return {"open_page": "openPage", "find_in_page": "findInPage"}.get(raw, raw or "search")


def _web_search_input(item):
    item = item if isinstance(item, dict) else {}
    action = item.get("action") if isinstance(item.get("action"), dict) else {}
    kind = _web_action_type(action)
    out = {"action": kind}
    if kind == "search":
        queries = [_txt(q).strip() for q in (action.get("queries") or []) if _txt(q).strip()]
        query = _txt(action.get("query") or item.get("query") or (queries[0] if queries else "")).strip()
        if query:
            out["query"] = query
        if queries:
            out["queries"] = queries
        out["display"] = query or " / ".join(queries[:2]) or "web search"
    elif kind == "openPage":
        url = _txt(action.get("url") or item.get("url")).strip()
        if url:
            out["url"] = url
        out["display"] = url or "open page"
    elif kind == "findInPage":
        url = _txt(action.get("url") or item.get("url")).strip()
        pattern = _txt(action.get("pattern") or item.get("pattern")).strip()
        if url:
            out["url"] = url
        if pattern:
            out["pattern"] = pattern
        out["display"] = (pattern + (" · " + url if url else "")) or url or "find in page"
    else:
        query = _txt(item.get("query")).strip()
        if query:
            out["query"] = query
            out["display"] = query
        elif action:
            out["action_detail"] = action
            out["display"] = kind
        else:
            out["display"] = "web search"
    return out


def _web_result_text(results):
    if not results:
        return ""
    if not isinstance(results, list):
        return _txt(results)
    lines = []
    for i, res in enumerate(results[:8], 1):
        if isinstance(res, dict):
            title = _txt(res.get("title") or res.get("name") or res.get("url") or "result").strip()
            url = _txt(res.get("url") or res.get("link")).strip()
            snippet = _txt(res.get("snippet") or res.get("text") or res.get("content")).strip()
            line = "%d. %s" % (i, title)
            if url and url != title:
                line += "\n   " + url
            if snippet:
                line += "\n   " + _cap(snippet, 500)
            lines.append(line)
        else:
            text = _txt(res).strip()
            if text:
                lines.append("%d. %s" % (i, text))
    extra = len(results) - len(lines)
    if extra > 0:
        lines.append("… %d more result%s" % (extra, "" if extra == 1 else "s"))
    return "\n".join(lines)


def _thread_ref_ok(thread_id):
    return bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", _txt(thread_id)))


def _agent_label(rec):
    for key in ("agentRole", "agentNickname", "agentPath"):
        val = _txt((rec or {}).get(key)).strip()
        if val:
            return os.path.basename(val) if key == "agentPath" else val
    prompt = _txt((rec or {}).get("prompt")).strip().splitlines()
    if prompt:
        return _cap(prompt[0], 80)
    tid = _txt((rec or {}).get("threadId")).strip()
    return "subagent " + (tid[:8] if tid else "?")


def _subagent_public(rec):
    rec = dict(rec or {})
    state = _txt(rec.get("state") or "running")
    done = state in ("completed", "interrupted", "errored", "shutdown", "notFound", "notfound")
    out = {
        "threadId": rec.get("threadId"),
        "label": _agent_label(rec),
        "state": state,
        "active": not done,
        "updatedAt": rec.get("updatedAt"),
        "createdAt": rec.get("createdAt"),
    }
    for key in ("activity", "agentPath", "agentRole", "agentNickname", "prompt",
                "message", "model", "reasoningEffort", "tool", "cwd", "title"):
        if rec.get(key):
            out[key] = rec.get(key)
    return out


def _item_user_text(item):
    parts = []
    for block in item.get("content") or []:
        if isinstance(block, dict):
            bt = block.get("type")
            if bt == "text":
                parts.append(_txt(block.get("text")))
            elif bt == "localImage":
                parts.append("[image] " + _txt(block.get("path")))
            else:
                parts.append(_txt(block))
        else:
            parts.append(_txt(block))
    return "\n".join(x for x in parts if x)


def _subagent_item_message(entry):
    entry = entry if isinstance(entry, dict) else {}
    item = entry.get("item") if isinstance(entry.get("item"), dict) else {}
    it = item.get("type") or "item"
    role, text = "tool", ""
    if it == "userMessage":
        role, text = "user", _item_user_text(item)
    elif it == "agentMessage":
        role, text = "assistant", _txt(item.get("text"))
    elif it == "reasoning":
        role, text = "thinking", _txt(item.get("summary") or item.get("content"))
    elif it == "plan":
        role, text = "plan", _txt(item.get("text"))
    elif it == "commandExecution":
        tool, shown = _codex_cmd(item.get("command"), item.get("commandActions"))
        out = _txt(item.get("aggregatedOutput")).strip()
        text = tool + " " + shown + (("\n\n" + out) if out else "")
    elif it == "fileChange":
        inp = _file_change_input(item.get("changes") or [])
        text = "apply_patch " + _txt(inp.get("file_path")) + "\n" + _txt(inp.get("diff"))
    elif it == "mcpToolCall":
        text = "%s.%s" % (item.get("server") or "mcp", item.get("tool") or "")
        res = item.get("error") or item.get("result")
        if res:
            text += "\n" + _txt(res)
    elif it == "dynamicToolCall":
        name = ".".join(x for x in (item.get("namespace"), item.get("tool")) if x)
        text = name or "dynamicTool"
        if item.get("contentItems"):
            text += "\n" + _txt(item.get("contentItems"))
    elif it == "webSearch":
        inp = _web_search_input(item)
        text = "web_search " + _txt(inp.get("display"))
        res = _web_result_text(item.get("results"))
        if res:
            text += "\n" + res
    elif it == "subAgentActivity":
        role = "agent"
        text = "%s %s" % (item.get("kind") or "agent", item.get("agentPath") or "")
    elif it in ("enteredReviewMode", "exitedReviewMode"):
        role, text = "agent", it
    elif it == "contextCompaction":
        role, text = "system", "context compacted"
    else:
        text = _txt(item)
    if not _txt(text).strip():
        return None
    return {"turnId": entry.get("turnId"), "itemId": item.get("id"),
            "role": role, "kind": it, "status": item.get("status"),
            "txt": _cap(_txt(text).strip(), THREAD_TXT_CAP)}


def _configured_default_model():
    """Best-effort fallback for displaying what Codex's `default` resolves to."""
    codex_home = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
    try:
        with open(os.path.join(codex_home, "config.toml"), "r", encoding="utf-8") as f:
            for line in f:
                if line.lstrip().startswith("["):
                    break
                m = _CONFIG_MODEL_RE.match(line)
                if m and m.group(2).strip():
                    return m.group(2).strip()
    except Exception:
        pass
    return ""


def _configured_context_window():
    """Best-effort context-window override from Codex config.toml.

    Codex CLI `/status` reports this configured value. The app-server token usage
    event may still expose the model/catalog window, so the Console keeps both
    values and displays the larger configured window when present.
    """
    codex_home = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
    try:
        with open(os.path.join(codex_home, "config.toml"), "r", encoding="utf-8") as f:
            for line in f:
                if line.lstrip().startswith("["):
                    break
                m = _CONFIG_CONTEXT_RE.match(line)
                if m:
                    return int(m.group(1).replace("_", ""))
    except Exception:
        pass
    return None


def _display_model(selected="", resolved=""):
    resolved = _txt(resolved).strip()
    if resolved:
        return resolved
    selected = _txt(selected).strip()
    if selected and selected != "default":
        return selected
    return _configured_default_model() or "default"

# session recap ("away summary")
RECAP_ENABLED  = (_env("RECAP", "0") or "0").lower() not in ("0", "false", "no", "off")
RECAP_IDLE_SEC = int(_env("RECAP_IDLE_SEC", "300") or "300")
RECAP_MODEL = _env("RECAP_MODEL", "gpt-5.3-codex-spark")
RECAP_TIMEOUT_SEC = int(_env("RECAP_TIMEOUT_SEC", "45") or "45")
# turn-complete verbs — a curated past-tense set; one is stamped onto each finished turn.
DONE_PAST = ["Baked", "Brewed", "Churned", "Cogitated", "Cooked", "Crunched", "Sautéed", "Worked"]


# ───────────────────────── normalization ─────────────────────────
# Both adapters emit the same event shape so the frontend is source-agnostic:
#   {kind, ts, id, ...}
#   kind="user_text"|"assistant_text"   -> Discussion   (+ role, text)
#   kind="thinking"                     -> Discussion (muted, collapsible) (+ text)
#   kind="tool_use"                     -> Activity     (+ tool, input, toolId)
#   kind="tool_result"                  -> Activity (attached by toolId) (+ content, isError)

def _txt(x):
    """Flatten arbitrary content (str | list of blocks | dict) to text."""
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        out = []
        for b in x:
            if isinstance(b, dict):
                if "text" in b:
                    out.append(str(b.get("text", "")))
                else:
                    out.append(json.dumps(b, ensure_ascii=False))
            else:
                out.append(str(b))
        return "\n".join(out)
    if isinstance(x, dict):
        if "text" in x:
            return str(x.get("text", ""))
        return json.dumps(x, ensure_ascii=False)
    return str(x)


def _pick(d, *keys):
    if not isinstance(d, dict):
        return None
    for key in keys:
        val = d.get(key)
        if val is not None:
            return val
    return None


def _cap(s, n=CAP):
    s = s or ""
    if len(s) > n:
        return s[:n] + "\n…[truncated %d chars]" % (len(s) - n)
    return s


def _cap_input(inp):
    if isinstance(inp, dict):
        return {k: (_cap(v) if isinstance(v, str) else v) for k, v in inp.items()}
    if isinstance(inp, str):
        return _cap(inp)
    return inp


def _jsonish(x):
    if isinstance(x, str):
        try:
            return json.loads(x)
        except Exception:
            return x
    return x


def _patch_target(patch):
    files = []
    for line in (patch or "").splitlines():
        for prefix in ("*** Add File: ", "*** Update File: ", "*** Delete File: "):
            if line.startswith(prefix):
                files.append(line[len(prefix):].strip())
                break
    if len(files) == 1:
        return files[0]
    return ("%d files" % len(files)) if files else "apply_patch"


_PLUMBING_TAGS = ("<command-name>", "<command-message>", "<command-args>",
                  "<local-command-stdout>", "<local-command-caveat>", "<system-reminder>")
def _is_plumbing(s):
    """CLI-injected user content (slash-command markup, local-command stdout/caveats,
    system reminders) — recorded in transcripts but never real chat to display."""
    return isinstance(s, str) and s.lstrip().startswith(_PLUMBING_TAGS)


_INJECTED_RE = re.compile(r"\s*<task-notification>.*?</task-notification>\s*", re.S)
def _strip_injected(s):
    """Remove harness-appended blocks (e.g. a background-task completion notice) that get
    tacked onto an otherwise-real user message in the transcript. On resume-from-disk we
    then render the user's actual text only, not the plumbing block."""
    return _INJECTED_RE.sub("", s) if isinstance(s, str) else s


def parse_claude(rec, idx):
    t = rec.get("type")
    base = {"ts": rec.get("timestamp"), "id": rec.get("uuid") or ("L%d" % idx)}
    msg = rec.get("message") or {}
    evs = []
    if t == "assistant":
        content = msg.get("content")
        if isinstance(content, str):
            if content.strip():
                evs.append({**base, "kind": "assistant_text", "role": "assistant", "text": _cap(content)})
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text" and b.get("text", "").strip():
                    evs.append({**base, "kind": "assistant_text", "role": "assistant", "text": _cap(b["text"])})
                elif bt == "thinking":
                    th = b.get("thinking", "")
                    if th.strip():
                        evs.append({**base, "kind": "thinking", "text": _cap(th)})
                elif bt == "tool_use":
                    evs.append({**base, "kind": "tool_use", "tool": b.get("name", ""),
                                "input": _cap_input(b.get("input")), "toolId": b.get("id", "")})
    elif t == "user":
        if rec.get("isMeta") or rec.get("isCompactSummary"):
            return []          # harness/CLI-injected, never real chat: "Continue from where
                               # you left off." + tool-error retries + caveats (isMeta), and
                               # the post-compaction summary continuation (isCompactSummary)
        content = msg.get("content")
        if isinstance(content, str):
            content = _strip_injected(content)
            if content.strip() and not _is_plumbing(content):
                evs.append({**base, "kind": "user_text", "role": "user", "text": _cap(content)})
        elif isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    txt = _strip_injected(b.get("text", ""))
                    if txt.strip() and not _is_plumbing(txt):
                        evs.append({**base, "kind": "user_text", "role": "user", "text": _cap(txt)})
                elif bt == "tool_result":
                    evs.append({**base, "kind": "tool_result", "toolId": b.get("tool_use_id", ""),
                                "content": _cap(_txt(b.get("content")), RESULT_CAP),
                                "isError": bool(b.get("is_error"))})
    return evs


# Codex injects instruction blocks at the start of a thread that LOOK like a user
# message (role "user" or "developer") but are harness plumbing — AGENTS.md, the
# permissions/sandbox spec, the environment context, the OMX bootstrap. They must
# not render as chat, nor be picked as a session title. The real user prompts are
# the ones codex marks with an `event_msg`/`user_message`; these injected ones are
# response_items only. Match by their stable opening markers.
_CODEX_INJECT_MARKERS = (
    "<permissions", "<environment_context",
    "<user_instructions", "<INSTRUCTIONS", "<system", "OMX native SessionStart",
)
_CODEX_AGENTS_RE = re.compile(r"^#\s*AGENTS\.md instructions\b", re.I)
def _codex_injected(text):
    t = (text or "").lstrip()
    return bool(_CODEX_AGENTS_RE.match(t)) or any(
        t.startswith(m) for m in _CODEX_INJECT_MARKERS)


def parse_codex(rec, idx):
    base = {"ts": rec.get("timestamp"), "id": "L%d" % idx}
    if rec.get("type") == "event_msg":
        p = rec.get("payload") or {}
        if p.get("type") == "sub_agent_activity":
            tid = p.get("agent_thread_id")
            if not tid:
                return []
            kind = p.get("kind") or "interacted"
            state = {"started": "running", "interacted": "running",
                     "completed": "completed", "interrupted": "interrupted"}.get(kind, kind)
            ts = p.get("occurred_at_ms")
            updated = (ts / 1000.0) if isinstance(ts, (int, float)) else None
            agent = {"threadId": tid, "agentPath": p.get("agent_path"),
                     "activity": kind, "state": state,
                     "updatedAt": updated, "createdAt": updated}
            return [{**base, "id": p.get("event_id") or base["id"],
                     "kind": "subagent_activity",
                     "subagent": _subagent_public(agent), "activity": kind}]
        return []
    if rec.get("type") != "response_item":
        return []
    p = rec.get("payload") or {}
    pt = p.get("type")
    evs = []
    if pt == "message":
        role = p.get("role", "assistant")
        if role not in ("user", "assistant"):
            return []                       # developer/system/tool = harness plumbing
        text = _txt(p.get("content"))
        if text.strip() and not (role == "user" and _codex_injected(text)):
            kind = "user_text" if role == "user" else "assistant_text"
            evs.append({**base, "kind": kind, "role": role, "text": _cap(text)})
    elif pt == "reasoning":
        text = _txt(p.get("summary") or p.get("content"))
        if text.strip():
            evs.append({**base, "kind": "thinking", "text": _cap(text)})
    elif pt == "function_call":
        name = p.get("name", "")
        args = _jsonish(p.get("arguments"))
        if name == "exec_command":
            evs.append(_codex_exec_event(base, args, p.get("call_id", "")))
        elif name == "apply_patch":
            patch = args.get("patch") if isinstance(args, dict) else _txt(args)
            evs.append({**base, "kind": "tool_use", "tool": "apply_patch",
                        "input": {"file_path": _patch_target(patch), "diff": _cap(patch, RESULT_CAP),
                                  "kind": "edit", "n": 1},
                        "toolId": p.get("call_id", "")})
        else:
            evs.append({**base, "kind": "tool_use", "tool": name,
                        "input": _cap_input(args), "toolId": p.get("call_id", "")})
    elif pt == "custom_tool_call":
        name = p.get("name", "")
        raw = p.get("input")
        if name == "apply_patch":
            patch = _txt(raw)
            evs.append({**base, "kind": "tool_use", "tool": "apply_patch",
                        "input": {"file_path": _patch_target(patch), "diff": _cap(patch, RESULT_CAP),
                                  "kind": "edit", "n": 1},
                        "toolId": p.get("call_id", "")})
        else:
            evs.append({**base, "kind": "tool_use", "tool": name,
                        "input": _cap_input(_jsonish(raw)), "toolId": p.get("call_id", "")})
    elif pt in ("function_call_output", "custom_tool_call_output"):
        out = _jsonish(p.get("output"))
        if isinstance(out, dict) and "output" in out:
            out = out["output"]
        evs.append({**base, "kind": "tool_result", "toolId": p.get("call_id", ""),
                    "content": _cap(_txt(out), RESULT_CAP), "isError": False})
    elif pt == "web_search_call":
        tid = p.get("call_id") or p.get("id") or base["id"]
        evs.append({**base, "kind": "tool_use", "tool": "web_search",
                    "input": _web_search_input(p), "toolId": tid})
        if p.get("status"):
            evs.append({**base, "kind": "tool_result", "toolId": tid,
                        "content": _cap(_web_result_text(p.get("results")), RESULT_CAP),
                        "isError": p.get("status") == "failed", "status": p.get("status")})
    return evs


def parse_line(line, idx, source):
    try:
        rec = json.loads(line)
    except Exception:
        return []
    try:
        return parse_claude(rec, idx) if source == "claude" else parse_codex(rec, idx)
    except Exception:
        return []


def normalize_cc(rec):
    """Normalize one `claude --output-format stream-json` event for the console UI."""
    t = rec.get("type")
    evs = []
    if t == "system":
        if rec.get("subtype") == "init":
            evs.append({"kind": "ready", "session_id": rec.get("session_id"),
                        "model": rec.get("model"), "cwd": rec.get("cwd"),
                        "tools": rec.get("tools"), "permissionMode": rec.get("permissionMode")})
    elif t == "assistant":
        for b in (rec.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and b.get("text", "").strip():
                evs.append({"kind": "assistant_text", "text": _cap(b["text"])})
            elif bt == "thinking" and b.get("thinking", "").strip():
                evs.append({"kind": "thinking", "text": _cap(b["thinking"])})
            elif bt == "tool_use":
                evs.append({"kind": "tool_use", "tool": b.get("name", ""),
                            "input": _cap_input(b.get("input")), "toolId": b.get("id", "")})
    elif t == "user":
        for b in (rec.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                evs.append({"kind": "tool_result", "toolId": b.get("tool_use_id", ""),
                            "content": _cap(_txt(b.get("content")), RESULT_CAP),
                            "isError": bool(b.get("is_error"))})
    elif t == "result":
        evs.append({"kind": "turn_done", "subtype": rec.get("subtype"),
                    "isError": bool(rec.get("is_error")), "numTurns": rec.get("num_turns"),
                    "cost": rec.get("total_cost_usd")})
    elif t == "rate_limit_event":
        evs.append({"kind": "notice", "text": "rate limit update"})
    return evs


# ───────────────────────── session discovery ─────────────────────────
def _peek_claude(path):
    cwd, branch, title = "", "", ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 150:
                    break
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if not cwd:
                    cwd = rec.get("cwd", "") or cwd
                    branch = rec.get("gitBranch", "") or branch
                if not title and rec.get("type") == "user" and not rec.get("isCompactSummary"):
                    msg = (rec.get("message") or {}).get("content")
                    s = msg if isinstance(msg, str) else _txt(msg)
                    s = (s or "").strip()
                    if s and not s.startswith("<") and "tool_result" not in s[:40]:
                        title = s.replace("\n", " ")[:100]
                if cwd and title:
                    break
    except Exception:
        pass
    return cwd, branch, title


def _peek_codex(path):
    cwd, branch, title, is_subagent = "", "", "", False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 150:
                    break
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                p = rec.get("payload") or {}
                rt = rec.get("type")
                if rt == "session_meta":
                    cwd = p.get("cwd", "") or cwd
                    g = p.get("git") or {}
                    branch = g.get("branch", "") or branch
                    source = p.get("source")
                    if isinstance(source, dict) and source.get("subagent"):
                        is_subagent = True
                # the REAL first user prompt: codex marks actual user input with an
                # `event_msg`/`user_message` — the AGENTS.md / permissions / env-context
                # blocks injected at thread start are response_items only, so this skips
                # them (they used to become the title).
                if not title and rt == "event_msg" and p.get("type") == "user_message":
                    s = _txt(p.get("message") or p.get("text") or p.get("content")).strip()
                    if s and not _codex_injected(s):
                        title = s.replace("\n", " ")[:100]
                # fallback for older rollouts without user_message events
                elif not title and rt == "response_item" and p.get("type") == "message" \
                        and p.get("role") == "user":
                    s = _txt(p.get("content")).strip()
                    if s and not s.startswith("<") and not _codex_injected(s):
                        title = s.replace("\n", " ")[:100]
                if cwd and title:
                    break
    except Exception:
        pass
    return cwd, branch, title, is_subagent


def list_sessions(limit=50):
    items = []
    codex_files = glob.glob(os.path.join(CODEX_ROOT, "**", "*.jsonl"), recursive=True)
    paths = [(p, "codex") for p in codex_files]
    try:
        paths.sort(key=lambda pc: os.path.getmtime(pc[0]), reverse=True)
    except Exception:
        pass
    for path, source in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        cwd, branch, title, is_subagent = _peek_codex(path)
        if is_subagent:
            continue
        items.append({
            "id": path, "source": source, "cwd": cwd, "branch": branch,
            "title": title or os.path.basename(path),
            "mtime": st.st_mtime, "size": st.st_size,
        })
        if len(items) >= limit:
            break
    return items


def list_projects():
    """Recent session cwds for the console picker, filtered to skip runtime/cache dirs."""
    junk = [os.path.realpath(p) for p in (
        "/tmp", os.path.join(HOME, ".cache"), os.path.join(HOME, ".claude-mem"),
        os.path.join(HOME, ".claude"), os.path.join(HOME, ".codex"),
        os.path.join(HOME, ".config"))]

    def is_junk(p):
        rp = os.path.realpath(p)
        return any(rp == j or rp.startswith(j + os.sep) for j in junk)

    out, seen = [], set()
    for s in list_sessions(60):
        cwd = s.get("cwd")
        if cwd and cwd not in seen and os.path.isdir(cwd) and not is_junk(cwd):
            seen.add(cwd)
            out.append({"path": cwd, "recent": True,
                        "git": os.path.isdir(os.path.join(cwd, ".git"))})
    return out


def _valid_cc(cc):
    return bool(cc) and 6 <= len(cc) <= 64 and all(c.isalnum() or c in "-_" for c in cc)


_JUNK_ROOTS = None
def _is_junk(p):
    global _JUNK_ROOTS
    if _JUNK_ROOTS is None:
        _JUNK_ROOTS = [os.path.realpath(x) for x in (
            "/tmp", os.path.join(HOME, ".cache"), os.path.join(HOME, ".claude-mem"),
            os.path.join(HOME, ".claude"), os.path.join(HOME, ".codex"),
            os.path.join(HOME, ".config"))]
    rp = os.path.realpath(p)
    return any(rp == j or rp.startswith(j + os.sep) for j in _JUNK_ROOTS)


def find_transcript(cc):
    """Locate codex's on-disk rollout for a thread id. Rollout files are named
    `rollout-<ts>-<threadId>.jsonl` under ~/.codex/sessions/YYYY/MM/DD/."""
    if not _valid_cc(cc):
        return None
    hits = glob.glob(os.path.join(CODEX_ROOT, "**", "rollout-*-" + cc + ".jsonl"),
                     recursive=True)
    if not hits:   # fall back to a looser match (id embedded anywhere in the name)
        hits = glob.glob(os.path.join(CODEX_ROOT, "**", "*" + cc + "*.jsonl"),
                         recursive=True)
    return hits[0] if hits else None


def trash_transcript(cc):
    """Move a codex session's on-disk rollout to the trash (reversible) — the
    sidebar 🗑 uses this to clean resumable sessions out of a folder. Prefers
    `gio trash`; falls back to an in-place rename if gio is unavailable."""
    if not _valid_cc(cc):
        return {"ok": False, "error": "invalid session id"}
    path = find_transcript(cc)
    if not path:
        return {"ok": False, "error": "transcript not found"}
    try:
        subprocess.run(["gio", "trash", path], check=True,
                       capture_output=True, timeout=10)
        return {"ok": True}
    except FileNotFoundError:
        pass  # gio not installed — fall through to the rename fallback
    except subprocess.CalledProcessError as ex:
        err = (ex.stderr or b"").decode("utf-8", "replace").strip()
        return {"ok": False, "error": err or "gio trash failed"}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}
    try:
        os.rename(path, "%s.trashed-%d" % (path, int(time.time())))
        return {"ok": True}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}


NAMES_FILE = os.path.join(HOME, ".codex", "console-names.json")
_names_cache = {"mtime": -1.0, "v": {}}

def load_names():
    """Custom session labels {codex_thread_id: name} that override the auto
    title. Cached; invalidated by the file's mtime."""
    try:
        m = os.path.getmtime(NAMES_FILE)
    except OSError:
        _names_cache["mtime"], _names_cache["v"] = -1.0, {}
        return _names_cache["v"]
    if m != _names_cache["mtime"]:
        try:
            with open(NAMES_FILE, encoding="utf-8") as f:
                d = json.load(f)
            _names_cache["v"] = d if isinstance(d, dict) else {}
        except Exception:
            _names_cache["v"] = {}
        _names_cache["mtime"] = m
    return _names_cache["v"]


def set_name(cc, name):
    """Set (or, when name is blank, clear) the custom label for a session."""
    if not _valid_cc(cc):
        return False
    names = dict(load_names())
    name = (name or "").strip()[:120]
    if name:
        names[cc] = name
    else:
        names.pop(cc, None)
    try:
        os.makedirs(os.path.dirname(NAMES_FILE), exist_ok=True)
        tmp = NAMES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(names, f, ensure_ascii=False)
        os.replace(tmp, NAMES_FILE)
        _names_cache["mtime"] = -1.0
        return True
    except Exception:
        return False


PREFS_FILE = os.path.join(HOME, ".codex", "console-prefs.json")
_prefs_cache = {"mtime": -1.0, "v": {}}

def load_prefs():
    """Per-session UI prefs {claude_session_id: {mode, model}} so a resumed
    session restores its own permission mode / model instead of reverting to the
    picker defaults. Cached; invalidated by mtime."""
    try:
        m = os.path.getmtime(PREFS_FILE)
    except OSError:
        _prefs_cache["mtime"], _prefs_cache["v"] = -1.0, {}
        return _prefs_cache["v"]
    if m != _prefs_cache["mtime"]:
        try:
            with open(PREFS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            _prefs_cache["v"] = d if isinstance(d, dict) else {}
        except Exception:
            _prefs_cache["v"] = {}
        _prefs_cache["mtime"] = m
    return _prefs_cache["v"]


def save_pref(cc, mode=None, model=None, effort=None, fav=None):
    """Persist a session's mode/model/effort/favorite. No-op write when unchanged.
    fav: a dict {cwd,name,title,ts} stars the session; a falsy value unstars it."""
    if not _valid_cc(cc):
        return
    prefs = load_prefs()
    cur = dict(prefs.get(cc) or {})
    if mode is not None:
        cur["mode"] = mode
    if model is not None:
        cur["model"] = model
    if effort is not None:
        cur["effort"] = effort
    if fav is not None:
        if fav:
            cur["fav"] = fav
        else:
            cur.pop("fav", None)
    if cur == prefs.get(cc):
        return
    prefs = dict(prefs)
    if cur:
        prefs[cc] = cur
    else:
        prefs.pop(cc, None)   # don't leave an empty entry behind
    try:
        os.makedirs(os.path.dirname(PREFS_FILE), exist_ok=True)
        tmp = PREFS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(prefs, f, ensure_ascii=False)
        os.replace(tmp, PREFS_FILE)
        _prefs_cache["mtime"] = -1.0
    except Exception:
        pass


def list_favorites():
    """All starred sessions across every device, newest-starred first.
    Title tracks the live custom label (load_names) so a rename shows up here too;
    falls back to the snapshot captured at star time when no custom name is set."""
    out = []
    names = load_names()
    for cc, p in load_prefs().items():
        f = p.get("fav") if isinstance(p, dict) else None
        if isinstance(f, dict):
            out.append({"cc": cc, "cwd": f.get("cwd", ""), "name": f.get("name", ""),
                        "title": names.get(cc) or f.get("title", ""), "ts": f.get("ts", 0)})
    out.sort(key=lambda x: x.get("ts", 0), reverse=True)
    for x in out:
        x.pop("ts", None)
    return out


# ─────────────────── projects (the sidebar groups by folder) ───────────────────
#
# A "project" is one folder. Its sessions are the rollouts whose cwd is EXACTLY
# that folder — a subfolder is a separate project, so opening several sessions in
# one directory collects them under one heading instead of scattering them
# through a flat recency list. Folders reach the sidebar two ways: they already
# hold a session, or they were pinned through Import folder.

PROJECTS_FILE = os.path.join(HOME, ".codex", "console-projects.json")
_projmeta_cache = {"mtime": -1.0, "v": {}}


def load_project_meta():
    """Per-folder metadata {abs_path: {name, fav, pinned, ts}}. Cached by mtime."""
    try:
        m = os.path.getmtime(PROJECTS_FILE)
    except OSError:
        _projmeta_cache["mtime"], _projmeta_cache["v"] = -1.0, {}
        return _projmeta_cache["v"]
    if m != _projmeta_cache["mtime"]:
        try:
            with open(PROJECTS_FILE, encoding="utf-8") as f:
                d = json.load(f)
            _projmeta_cache["v"] = d if isinstance(d, dict) else {}
        except Exception:
            _projmeta_cache["v"] = {}
        _projmeta_cache["mtime"] = m
    return _projmeta_cache["v"]


def save_project_meta(path, name=None, fav=None, pinned=None):
    """Persist one folder's label / star / pin. Dropping every flag removes the
    entry, so an unpinned, unstarred, unrenamed folder leaves no residue."""
    path = _norm_dir(path)
    if not path:
        return False
    meta = load_project_meta()
    cur = meta.get(path)
    cur = dict(cur) if isinstance(cur, dict) else {}
    if name is not None:
        name = (name or "").strip()[:120]
        if name:
            cur["name"] = name
        else:
            cur.pop("name", None)
    if fav is not None:
        if fav:
            cur["fav"] = True
            cur.setdefault("ts", time.time())
        else:
            cur.pop("fav", None)
    if pinned is not None:
        if pinned:
            cur["pinned"] = True
            cur.setdefault("ts", time.time())
        else:
            cur.pop("pinned", None)
    if not cur.get("fav") and not cur.get("pinned"):
        cur.pop("ts", None)
    if cur == meta.get(path):
        return True
    meta = dict(meta)
    if cur:
        meta[path] = cur
    else:
        meta.pop(path, None)
    return _write_project_meta(meta)


def _write_project_meta(meta):
    try:
        os.makedirs(os.path.dirname(PROJECTS_FILE), exist_ok=True)
        tmp = PROJECTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(tmp, PROJECTS_FILE)
        _projmeta_cache["mtime"] = -1.0
        return True
    except Exception:
        return False


FAV_MIGRATION_KEY = "#migrated_session_favs"


def migrate_session_favs_to_projects():
    """One-off: favorites used to be per session, they are per folder now. Lift
    every existing session star onto the folder that session ran in, so the
    Favorites list survives the change instead of starting empty. A marker in the
    same file makes this idempotent, so a folder the user later unstars is never
    resurrected on the next start."""
    meta = load_project_meta()
    if meta.get(FAV_MIGRATION_KEY):
        return 0
    moved = set()
    for f in list_favorites():
        path = _norm_dir(f.get("cwd"))
        if path and not _is_junk(path):
            moved.add(path)
    for path in moved:
        save_project_meta(path, fav=True)
    meta = dict(load_project_meta())
    meta[FAV_MIGRATION_KEY] = True
    _write_project_meta(meta)
    return len(moved)


def _norm_dir(p):
    """Absolute, symlink-resolved directory path, or "" when it isn't one.
    Blank input returns "" rather than the process cwd, which is what
    os.path.realpath("") would hand back."""
    p = (p or "").strip()
    if not p:
        return ""
    try:
        p = os.path.realpath(os.path.expanduser(p))
    except Exception:
        return ""
    return p if p and os.path.isdir(p) else ""


def short_path(p):
    """The subtitle shown under a project name: parent/current, never the full
    path. $HOME collapses to ~ so a session opened in the home dir reads as ~."""
    if p == HOME:
        return "~"
    rel = p[len(HOME) + 1:] if p.startswith(HOME + os.sep) else p.lstrip(os.sep)
    parts = [x for x in rel.split(os.sep) if x]
    return os.sep.join(parts[-2:]) if parts else p


_scan_cache = {"t": 0.0, "v": {}}


def _sessions_by_folder(force=False):
    """{folder: [session, ...]} over every non-subagent rollout, newest first.
    Grouping is by exact folder. Cheap (~0.05s for 240 rollouts) but it runs on
    every sidebar refresh, so it is cached for a few seconds."""
    now = time.monotonic()
    if not force and _scan_cache["v"] and now - _scan_cache["t"] < 6:
        return _scan_cache["v"]
    names = load_names()
    out = {}
    for s in list_sessions(2000):
        if s.get("source") != "codex":
            continue
        folder = _norm_dir(s.get("cwd"))
        if not folder or _is_junk(folder):
            continue
        cc = _codex_cc_from_path(s["id"])
        if not cc:
            continue
        out.setdefault(folder, []).append({
            "cc": cc, "cwd": s.get("cwd") or folder,
            "title": names.get(cc) or s.get("title", ""),
            "mtime": s.get("mtime", 0),
        })
    _scan_cache["v"], _scan_cache["t"] = out, now
    return out


def project_tree(force=False):
    """The sidebar payload. One entry per folder, most recently touched first.
    Pinned and starred folders appear even with no sessions on disk yet."""
    by_folder = _sessions_by_folder(force)
    meta = load_project_meta()
    folders = set(by_folder)
    for path, m in meta.items():
        if not isinstance(m, dict):
            continue          # the migration marker, not a folder
        if (m.get("fav") or m.get("pinned")) and os.path.isdir(path):
            folders.add(path)
    out = []
    for path in folders:
        sessions = by_folder.get(path, [])
        m = meta.get(path)
        m = m if isinstance(m, dict) else {}
        out.append({
            "path": path,
            "name": m.get("name") or os.path.basename(path) or path,
            "sub": short_path(path),
            "fav": bool(m.get("fav")),
            "pinned": bool(m.get("pinned")),
            "renamed": bool(m.get("name")),
            "mtime": max([x["mtime"] for x in sessions], default=m.get("ts", 0)),
            "sessions": sessions[:60],
            "n": len(sessions),
        })
    out.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return out


def fetch_usage():
    """The rolling usage limits codex reports via `account/rateLimits/updated`
    (the same numbers the codex CLI shows). Populated live by any active session;
    `{five_hour:{utilization,resets_at}, seven_day:{...}}`, or {} until a session
    has received its first rate-limit notification."""
    return dict(_CODEX_USAGE)


# ─────────────────── history search index ───────────────────
#
# Codex rollouts contain huge tool outputs, encrypted reasoning blobs, environment
# bootstrap, and AGENTS.md context. Searching raw JSONL would be noisy and slow, so
# the index stores only conversation text plus the paths/commands tools acted on.

INDEX_DB = os.path.join(HOME, ".cache", "codex-console", "history.db")
CJK_RE = re.compile(r"[㐀-鿿぀-ヿ가-힯]")
INDEX_REFRESH_SEC = 30
INDEX_SCHEMA = 1
THREAD_TXT_CAP = 20000
_index_lock = threading.Lock()
_index_state = {"t": 0.0, "err": ""}


def _index_conn():
    os.makedirs(os.path.dirname(INDEX_DB), exist_ok=True)
    db = sqlite3.connect(INDEX_DB, timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    if db.execute("PRAGMA user_version").fetchone()[0] != INDEX_SCHEMA:
        db.executescript("DROP TABLE IF EXISTS msgs; DROP TABLE IF EXISTS files;")
        db.execute("PRAGMA user_version=%d" % INDEX_SCHEMA)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS files(
            fid INTEGER PRIMARY KEY, path TEXT UNIQUE,
            off INTEGER DEFAULT 0, cc TEXT, cwd TEXT, title TEXT);
        CREATE TABLE IF NOT EXISTS msgs(
            fid INTEGER, uid TEXT, ts TEXT, role TEXT, txt TEXT);
        CREATE INDEX IF NOT EXISTS msgs_fid ON msgs(fid);
        CREATE INDEX IF NOT EXISTS files_cc ON files(cc);
        CREATE INDEX IF NOT EXISTS files_cwd ON files(cwd);
        CREATE UNIQUE INDEX IF NOT EXISTS msgs_uid ON msgs(fid, uid);
    """)
    return db


def _tool_search_text(ev):
    i = ev.get("input")
    if isinstance(i, dict):
        for k in ("file_path", "command", "pattern", "url"):
            v = i.get(k)
            if isinstance(v, str) and v.strip():
                return v[:400]
        return json.dumps(i, ensure_ascii=False)[:400]
    if isinstance(i, str):
        return i[:400]
    return ""


def _searchable_codex_record(rec, idx):
    """Yield (role, text) pairs for one Codex rollout record."""
    if rec.get("type") == "event_msg":
        p = rec.get("payload") or {}
        if p.get("type") == "user_message":
            text = _strip_injected(_txt(p.get("message") or p.get("text") or p.get("content")))
            if text.strip() and not _codex_injected(text):
                yield "user", text
        return
    for ev in parse_codex(rec, idx):
        k = ev.get("kind")
        if k == "assistant_text":
            text = ev.get("text") or ""
            if text.strip():
                yield "assistant", text
        elif k == "tool_use":
            text = _tool_search_text(ev)
            if text.strip():
                yield "tool", text


def _rollout_meta_from_record(rec):
    if rec.get("type") != "session_meta":
        return "", "", ""
    p = rec.get("payload") or {}
    return p.get("id") or "", p.get("cwd") or "", p.get("timestamp") or rec.get("timestamp") or ""


def reindex():
    """Incrementally fold Codex rollout tails into the search index."""
    if not _index_lock.acquire(blocking=False):
        return {"skipped": "already running"}
    try:
        db = _index_conn()
        known = {p: (fid, off) for fid, p, off
                 in db.execute("SELECT fid, path, off FROM files")}
        added = 0
        alive = set()
        paths = glob.glob(os.path.join(CODEX_ROOT, "**", "*.jsonl"), recursive=True)
        for path in paths:
            cwd, _branch, title, is_subagent = _peek_codex(path)
            if is_subagent:
                continue
            alive.add(path)
            try:
                sz = os.path.getsize(path)
            except OSError:
                continue
            fid, off = known.get(path, (None, 0))
            if fid is not None and sz < off:
                db.execute("DELETE FROM msgs WHERE fid=?", (fid,))
                off = 0
            if fid is not None and sz == off:
                continue
            try:
                with open(path, "rb") as f:
                    f.seek(off)
                    data = f.read()
            except OSError:
                continue
            cut = data.rfind(b"\n")
            if cut < 0:
                continue
            chunk, consumed = data[:cut + 1], cut + 1
            cc = _codex_cc_from_path(path)
            rows = []
            pos = 0
            for raw in chunk.splitlines(True):
                line_start = off + pos
                pos += len(raw)
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line.decode("utf-8", "replace"))
                except Exception:
                    continue
                m_cc, m_cwd, _m_ts = _rollout_meta_from_record(rec)
                cc = m_cc or cc
                cwd = m_cwd or cwd
                ts = rec.get("timestamp") or ""
                for seq, (role, txt) in enumerate(_searchable_codex_record(rec, line_start)):
                    rows.append(("%d:%d" % (line_start, seq), ts, role, txt))
            if fid is None:
                cur = db.execute(
                    "INSERT INTO files(path, off, cc, cwd, title) VALUES(?,?,?,?,?)",
                    (path, 0, cc, cwd, title))
                fid = cur.lastrowid
            else:
                db.execute("UPDATE files SET cc=COALESCE(NULLIF(cc,''),?),"
                           " cwd=COALESCE(NULLIF(cwd,''),?),"
                           " title=COALESCE(NULLIF(title,''),?) WHERE fid=?",
                           (cc, cwd, title, fid))
            if rows:
                before = db.total_changes
                db.executemany(
                    "INSERT OR IGNORE INTO msgs(fid, uid, ts, role, txt) VALUES(?,?,?,?,?)",
                    [(fid, uid, ts, role, txt) for uid, ts, role, txt in rows])
                added += db.total_changes - before
            db.execute("UPDATE files SET off=? WHERE fid=?", (off + consumed, fid))
        for path, (fid, _off) in known.items():
            if path not in alive:
                db.execute("DELETE FROM msgs WHERE fid=?", (fid,))
                db.execute("DELETE FROM files WHERE fid=?", (fid,))
        db.commit()
        n = db.execute("SELECT COUNT(*) FROM msgs").fetchone()[0]
        db.close()
        _index_state["t"] = time.time()
        _index_state["err"] = ""
        return {"added": added, "messages": n}
    except Exception as e:
        _index_state["err"] = str(e)
        return {"error": str(e)}
    finally:
        _index_lock.release()


def min_query_len(q):
    return 1 if CJK_RE.search(q or "") else 2


def _snippet(txt, q, span=160):
    i = txt.lower().find(q.lower())
    if i < 0:
        return {"pre": txt[:span], "hit": "", "post": ""}
    a = max(0, i - span // 2)
    b = min(len(txt), i + len(q) + span)
    return {"pre": ("..." if a else "") + txt[a:i],
            "hit": txt[i:i + len(q)],
            "post": txt[i + len(q):b] + ("..." if b < len(txt) else "")}


def search_history(q, scope="all", cc="", cwd="", limit=200):
    q = (q or "").strip()
    need = min_query_len(q)
    if len(q) < need:
        return {"results": [], "note": "type at least %d character%s"
                                       % (need, "" if need == 1 else "s")}
    if time.time() - _index_state["t"] > INDEX_REFRESH_SEC:
        reindex()
    try:
        db = _index_conn()
    except Exception as e:
        return {"results": [], "error": str(e)}
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    where, args = ["m.txt LIKE ? ESCAPE '\\'"], ["%" + esc + "%"]
    if scope == "session" and cc:
        where.append("f.cc=?")
        args.append(cc)
    elif scope == "project" and cwd:
        base = os.path.abspath(os.path.expanduser(cwd)).rstrip(os.sep)
        pre = base.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(f.cwd=? OR f.cwd LIKE ? ESCAPE '\\')")
        args.extend([base, pre + os.sep + "%"])
    args.append(int(limit) + 1)
    rows = db.execute(
        "SELECT f.cc, f.cwd, f.title, m.ts, m.role, m.txt, m.rowid"
        " FROM msgs m JOIN files f ON f.fid=m.fid WHERE " + " AND ".join(where) +
        " ORDER BY m.ts DESC, m.rowid DESC LIMIT ?", args).fetchall()
    db.close()
    more = len(rows) > limit
    names = load_names()
    out = []
    for r_cc, r_cwd, r_title, ts, role, txt, mid in rows[:limit]:
        item = {"cc": r_cc, "cwd": r_cwd, "ts": ts, "role": role, "mid": mid,
                "title": names.get(r_cc) or r_title or os.path.basename(r_cwd or "") or "session",
                "name": os.path.basename(r_cwd or "")}
        item.update(_snippet(txt, q))
        out.append(item)
    return {"results": out, "more": more}


def load_thread(mid, before=40, after=40):
    try:
        db = _index_conn()
        mid = int(mid)
    except Exception as e:
        return {"error": str(e), "messages": []}
    r = db.execute("SELECT fid FROM msgs WHERE rowid=?", (mid,)).fetchone()
    if not r:
        db.close()
        return {"error": "that message is no longer indexed", "messages": []}
    fid = r[0]
    cc, cwd, title = db.execute(
        "SELECT cc, cwd, title FROM files WHERE fid=?", (fid,)).fetchone() or ("", "", "")
    pre = db.execute("SELECT rowid, ts, role, txt FROM msgs WHERE fid=? AND rowid<=?"
                     " ORDER BY rowid DESC LIMIT ?", (fid, mid, int(before) + 1)).fetchall()
    post = db.execute("SELECT rowid, ts, role, txt FROM msgs WHERE fid=? AND rowid>?"
                      " ORDER BY rowid LIMIT ?", (fid, mid, int(after))).fetchall()
    lo, hi = db.execute("SELECT MIN(rowid), MAX(rowid) FROM msgs WHERE fid=?", (fid,)).fetchone()
    total = db.execute("SELECT COUNT(*) FROM msgs WHERE fid=?", (fid,)).fetchone()[0]
    db.close()
    rows = list(reversed(pre)) + list(post)
    msgs = []
    for rid, ts, role, txt in rows:
        cut = len(txt) > THREAD_TXT_CAP
        msgs.append({"mid": rid, "ts": ts, "role": role,
                     "txt": txt[:THREAD_TXT_CAP] + ("\n...(truncated)" if cut else "")})
    return {"cc": cc, "cwd": cwd, "total": total, "messages": msgs,
            "title": load_names().get(cc) or title or os.path.basename(cwd or "") or "session",
            "atStart": bool(rows) and rows[0][0] == lo,
            "atEnd": bool(rows) and rows[-1][0] == hi}


_UUID_RE = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})")

def _codex_cc_from_path(path):
    """Extract a codex thread id (the trailing UUID) from a rollout filename."""
    m = _UUID_RE.search(os.path.basename(path or ""))
    return m.group(1) if m else os.path.splitext(os.path.basename(path or ""))[0]


def transcript_thread_id(body):
    """Read the Codex thread id claimed by an uploaded rollout."""
    for line in body[: 2 << 20].decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        cc, _cwd, _ts = _rollout_meta_from_record(rec)
        if cc:
            return str(cc)
    return ""


def _rollout_timestamp(body):
    for line in body[: 2 << 20].decode("utf-8", "replace").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        _cc, _cwd, ts = _rollout_meta_from_record(rec)
        ts = ts or rec.get("timestamp") or ""
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", ts)
        if m:
            y, mo, d, h, mi, s = m.groups()
            return os.path.join(y, mo, d), "%s-%s-%sT%s-%s-%s" % (y, mo, d, h, mi, s)
    g = time.gmtime()
    return time.strftime("%Y/%m/%d", g), time.strftime("%Y-%m-%dT%H-%M-%S", g)


def import_rollout_dest(cc, body):
    date_dir, stamp = _rollout_timestamp(body)
    dest_dir = os.path.join(CODEX_ROOT, date_dir)
    return os.path.join(dest_dir, "rollout-%s-%s.jsonl" % (stamp, cc))


def load_transcript_events(cc, cap=1000):
    """Parse a saved codex rollout into console events, for preload on resume."""
    path = find_transcript(cc)
    if not path:
        return []
    evs = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if line:
                    evs.extend(parse_line(line, i, "codex"))
    except Exception:
        pass
    return evs[-cap:] if len(evs) > cap else evs


def load_transcript_meta(cc):
    """Latest non-content model/context metadata from a saved Codex rollout."""
    path = find_transcript(cc)
    if not path:
        return {}
    meta = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                p = rec.get("payload") or {}
                if rec.get("type") == "turn_context":
                    model = _txt(p.get("model")).strip()
                    effort = _txt(p.get("effort")).strip()
                    if model:
                        meta["model"] = model
                    if effort:
                        meta["effort"] = effort
                elif rec.get("type") == "event_msg" and p.get("type") == "token_count":
                    info = p.get("info") or {}
                    last = _pick(info, "last_token_usage", "lastTokenUsage") or {}
                    mx = _pick(info, "model_context_window", "modelContextWindow")
                    cur = _pick(last, "input_tokens", "inputTokens", "total_tokens", "totalTokens")
                    if cur is not None and mx:
                        meta["ctx"] = {"totalTokens": cur, "maxTokens": mx,
                                       "reportedMaxTokens": mx,
                                       "configuredMaxTokens": _configured_context_window(),
                                       "percentage": round(cur * 100.0 / mx, 1)}
                elif rec.get("type") == "event_msg" and p.get("type") == "task_started":
                    mx = _pick(p, "model_context_window", "modelContextWindow")
                    if mx:
                        meta["reportedMaxTokens"] = mx
    except Exception:
        return meta
    if meta.get("ctx") and meta.get("model"):
        meta["ctx"]["model"] = meta["model"]
    return meta


def list_resumable(cwd=None, limit=20):
    """Recent codex sessions that can be continued (thread/resume). If `cwd` is
    given, restrict to sessions whose working dir is exactly that folder
    (scan deeper, since one folder's sessions may be far down the global list)."""
    target = os.path.realpath(cwd) if cwd else None
    out = []
    for s in list_sessions(500 if target else 60):
        if s.get("source") != "codex":
            continue
        scwd = s.get("cwd")
        if not scwd or not os.path.isdir(scwd) or _is_junk(scwd):
            continue
        # match the folder AND everything under it (sessions usually live in a
        # project subdir, not the container folder itself)
        rscwd = os.path.realpath(scwd)
        if target and not (rscwd == target or rscwd.startswith(target + os.sep)):
            continue
        cc = _codex_cc_from_path(s["id"])
        out.append({"cc": cc, "cwd": scwd, "name": os.path.basename(scwd) or scwd,
                    "title": load_names().get(cc) or s.get("title", ""),
                    "mtime": s.get("mtime", 0)})
        if len(out) >= limit:
            break
    return out


_proj_cache = {"t": 0.0, "v": []}
def _projects_cached():
    now = time.monotonic()
    if now - _proj_cache["t"] > 8 or not _proj_cache["v"]:
        _proj_cache["v"] = list_projects()
        _proj_cache["t"] = now
    return _proj_cache["v"]


def dir_complete(q, limit=500):
    """Directory autocomplete for the console path box. If the typed path IS an
    existing directory, list its children (so you don't need a trailing '/');
    otherwise complete the last segment within its parent. A bare name fragment
    (no '/') also fuzzy-matches known projects. Restricted to $HOME. Returns
    (dirs, more) where `more` flags that results were capped."""
    q = (q or "").strip()
    home = os.path.realpath(HOME)
    out, seen = [], set()

    def add(p):
        rp = os.path.realpath(os.path.expanduser(p))
        if rp in seen or not os.path.isdir(rp):
            return
        if not (rp == home or rp.startswith(home + os.sep)):
            return
        if _is_junk(rp):
            return
        seen.add(rp)
        out.append(rp)

    def fs_complete():
        cand = os.path.expanduser(q) if q else HOME
        if cand and not cand.endswith(os.sep) and os.path.isdir(cand):
            base, frag = cand, ""           # typed path is a dir → list its children
        elif cand.endswith(os.sep):
            base, frag = cand, ""
        else:
            base, frag = os.path.split(cand)
        base = base or HOME
        try:
            names = sorted(os.listdir(base), key=str.lower)
        except Exception:
            return
        fl = frag.lower()
        for name in names:
            if name.startswith(".") and not frag.startswith("."):
                continue
            if not fl or name.lower().startswith(fl):
                add(os.path.join(base, name))

    def fuzzy():
        ql = q.lower()
        for proj in _projects_cached():
            p = proj["path"]
            if not ql or ql in p.lower() or ql in os.path.basename(p).lower():
                add(p)

    if ("/" in q) or q.startswith("~"):
        fs_complete()                       # explicit path → filesystem browse only
    else:
        fuzzy(); fs_complete()              # bare name → fuzzy projects + home entries
    return out[:limit], len(out) > limit


# ───────────────────────── git diff (ground truth) ─────────────────────────
def _git(cwd, args, timeout=6):
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout
    except Exception:
        return ""


def git_snapshot(cwd):
    rp = os.path.realpath(cwd)
    if not (rp == os.path.realpath(HOME) or rp.startswith(os.path.realpath(HOME) + os.sep)):
        return {"ok": False, "error": "path outside home"}
    if not os.path.isdir(rp):
        return {"ok": False, "error": "not a directory"}
    inside = _git(rp, ["rev-parse", "--is-inside-work-tree"]).strip()
    if inside != "true":
        return {"ok": False, "error": "not a git repo", "cwd": rp}
    branch = _git(rp, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    porcelain = _git(rp, ["status", "--porcelain"])
    files = []
    untracked = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        code, name = line[:2], line[3:]
        files.append({"status": code.strip() or "?", "path": name})
        if code == "??":
            untracked.append(name)
    diff = _git(rp, ["-c", "core.pager=cat", "diff"])
    staged = _git(rp, ["-c", "core.pager=cat", "diff", "--cached"])
    chunks = []
    if staged.strip():
        chunks.append("# ── staged ──\n" + staged)
    if diff.strip():
        chunks.append(diff)
    # synthesize a +diff for untracked (small text) files so new files are visible
    for name in untracked[:40]:
        fp = os.path.join(rp, name)
        try:
            if os.path.isfile(fp) and os.path.getsize(fp) < 80000:
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
                if "\x00" in body:
                    continue
                lines = body.splitlines()
                head = ["diff --git a/%s b/%s" % (name, name), "new file (untracked)",
                        "--- /dev/null", "+++ b/%s" % name]
                chunks.append("\n".join(head + ["+" + ln for ln in lines]))
        except Exception:
            continue
    full = "\n".join(chunks)
    if len(full) > 260000:
        full = full[:260000] + "\n…[diff truncated]"
    return {"ok": True, "cwd": rp, "branch": branch, "files": files, "diff": full}


def git_status_brief(cwd):
    rp = os.path.realpath(cwd)
    if not (rp == os.path.realpath(HOME) or rp.startswith(os.path.realpath(HOME) + os.sep)):
        return {"ok": False, "error": "path outside home"}
    if not os.path.isdir(rp):
        return {"ok": False, "error": "not a directory"}
    inside = _git(rp, ["rev-parse", "--is-inside-work-tree"]).strip()
    if inside != "true":
        return {"ok": False, "error": "not a git repo", "cwd": rp}
    branch = _git(rp, ["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    head = _git(rp, ["rev-parse", "--short", "HEAD"]).strip()
    root = _git(rp, ["rev-parse", "--show-toplevel"]).strip()
    staged = unstaged = untracked = 0
    files = []
    for line in _git(rp, ["status", "--porcelain=v1"]).splitlines():
        if len(line) < 4:
            continue
        x, y, name = line[0], line[1], line[3:]
        if x == "?" and y == "?":
            untracked += 1
        else:
            if x != " ":
                staged += 1
            if y != " ":
                unstaged += 1
        files.append({"status": line[:2].strip() or "?", "path": name})
    return {
        "ok": True, "cwd": rp, "root": root, "branch": branch, "head": head,
        "dirty": bool(files), "staged": staged, "unstaged": unstaged,
        "untracked": untracked, "files": files[:8], "file_count": len(files),
    }


# ───────────────────────── auth ─────────────────────────
class AuthMixin:
    def _ok_auth(self):
        if not AUTH:
            return True
        header = self.request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                if secrets.compare_digest(base64.b64decode(header[6:]).decode(), AUTH):
                    return True
            except Exception:
                pass
        self.set_status(401)
        self.set_header("WWW-Authenticate", 'Basic realm="codex-console"')
        self.finish()
        return False


# ───────────────────────── handlers ─────────────────────────


class ProjectsHandler(AuthMixin, tornado.web.RequestHandler):
    def get(self):
        if not self._ok_auth():
            return
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps({"projects": list_projects(), "home": HOME}))


class ResumableHandler(AuthMixin, tornado.web.RequestHandler):
    def get(self):
        if not self._ok_auth():
            return
        self.set_header("Content-Type", "application/json")
        cwd = self.get_argument("cwd", "") or None
        self.write(json.dumps({"resumable": list_resumable(cwd)}))


class TreeHandler(AuthMixin, tornado.web.RequestHandler):
    """The project-grouped sidebar: folders, each with the sessions it owns."""
    def get(self):
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps({"projects": project_tree()}))


class PinFolderHandler(AuthMixin, tornado.web.RequestHandler):
    """Import folder: register a directory as a project so it shows in the
    sidebar before it owns any session."""
    def post(self):
        self.set_header("Content-Type", "application/json")
        try:
            body = json.loads(self.request.body or b"{}")
        except Exception:
            body = {}
        raw = (body.get("path") or "").strip()
        path = _norm_dir(raw)
        if not path:
            self.write(json.dumps({"ok": False, "error": "not a directory: " + (raw or "(empty)")}))
            return
        home = os.path.realpath(HOME)
        if not (path == home or path.startswith(home + os.sep)):
            self.write(json.dumps({"ok": False, "error": "outside $HOME"}))
            return
        if _is_junk(path):
            self.write(json.dumps({"ok": False, "error": "runtime/cache directory"}))
            return
        ok = save_project_meta(path, pinned=True)
        self.write(json.dumps({"ok": bool(ok), "path": path,
                               "name": os.path.basename(path) or path,
                               "sub": short_path(path)}))


class DirCompleteHandler(AuthMixin, tornado.web.RequestHandler):
    def get(self):
        if not self._ok_auth():
            return
        self.set_header("Content-Type", "application/json")
        dirs, more = dir_complete(self.get_argument("q", ""))
        self.write(json.dumps({"dirs": dirs, "more": more}))


class DiffHandler(AuthMixin, tornado.web.RequestHandler):
    def get(self):
        if not self._ok_auth():
            return
        cwd = self.get_argument("cwd", "")
        self.set_header("Content-Type", "application/json")
        if not cwd:
            self.write(json.dumps({"ok": False, "error": "no cwd"}))
            return
        self.write(json.dumps(git_snapshot(cwd)))


class UsageHandler(AuthMixin, tornado.web.RequestHandler):
    async def get(self):
        if not self._ok_auth():
            return
        self.set_header("Content-Type", "application/json")
        loop = tornado.ioloop.IOLoop.current()
        u = await loop.run_in_executor(None, fetch_usage)   # blocking HTTP off-loop
        self.write(json.dumps({"usage": u}))


class ModelsHandler(AuthMixin, tornado.web.RequestHandler):
    async def get(self):
        if not self._ok_auth():
            return
        self.set_header("Content-Type", "application/json")
        self.set_header("Cache-Control", "no-store")
        loop = tornado.ioloop.IOLoop.current()
        fresh = self.get_argument("fresh", "") == "1"
        models = await loop.run_in_executor(None, fetch_models, fresh)
        default_model = _configured_default_model()
        if not default_model:
            default_model = next(
                (m.get("id") for m in (models or []) if m.get("isDefault")), "")
        self.write(json.dumps({"models": models or [], "defaultModel": default_model}))


class ExportHandler(AuthMixin, tornado.web.RequestHandler):
    async def get(self):
        if not self._ok_auth():
            return
        cc = self.get_argument("cc", "")
        path = find_transcript(cc)
        if not path:
            self.set_status(404)
            self.write("no transcript for that session")
            return
        self.set_header("Content-Type", "application/x-ndjson")
        self.set_header("Content-Disposition", 'attachment; filename="%s.jsonl"' % cc)
        self.set_header("Content-Length", str(os.path.getsize(path)))
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                self.write(chunk)
                await self.flush()


class ImportHandler(AuthMixin, tornado.web.RequestHandler):
    def post(self):
        if not self._ok_auth():
            return
        self.set_header("Content-Type", "application/json")

        def fail(msg, code=400):
            self.set_status(code)
            self.write(json.dumps({"ok": False, "error": msg}))

        cwd = (self.get_body_argument("cwd", "") or "").strip()
        if not cwd:
            return fail("pick a project folder first")
        cwd = os.path.realpath(os.path.abspath(os.path.expanduser(cwd)))
        home = os.path.realpath(HOME)
        if not (cwd == home or cwd.startswith(home + os.sep)) or not os.path.isdir(cwd):
            return fail("not a folder under home: %s" % cwd)
        files = self.request.files.get("file") or []
        if not files:
            return fail("no file uploaded")
        up = files[0]
        body = up.get("body") or b""
        if not body:
            return fail("empty file")
        cc = transcript_thread_id(body)
        if not cc:
            cc = os.path.splitext(os.path.basename(up.get("filename") or ""))[0]
        if not _valid_cc(cc):
            return fail("not a codex rollout transcript (no thread id found)")
        dest = import_rollout_dest(cc, body)
        if os.path.exists(dest) or find_transcript(cc):
            return fail("this session already exists", 409)
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".part"
            with open(tmp, "wb") as f:
                f.write(body)
            os.chmod(tmp, 0o600)
            os.replace(tmp, dest)
        except Exception as e:
            return fail("write failed: %s" % e, 500)
        _index_state["t"] = 0.0
        self.write(json.dumps({"ok": True, "cc": cc, "cwd": cwd,
                               "bytes": len(body), "path": dest}))


class SearchHandler(AuthMixin, tornado.web.RequestHandler):
    async def get(self):
        if not self._ok_auth():
            return
        self.set_header("Content-Type", "application/json")
        loop = tornado.ioloop.IOLoop.current()
        res = await loop.run_in_executor(
            None, search_history, self.get_argument("q", ""),
            self.get_argument("scope", "all"), self.get_argument("cc", ""),
            self.get_argument("cwd", ""), 200)
        self.write(json.dumps(res))


class ThreadHandler(AuthMixin, tornado.web.RequestHandler):
    async def get(self):
        if not self._ok_auth():
            return
        self.set_header("Content-Type", "application/json")
        loop = tornado.ioloop.IOLoop.current()
        try:
            before = max(0, min(400, int(self.get_argument("before", "40") or 40)))
            after = max(0, min(400, int(self.get_argument("after", "40") or 40)))
        except Exception:
            before, after = 40, 40
        res = await loop.run_in_executor(
            None, load_thread, self.get_argument("mid", "0"), before, after)
        self.write(json.dumps(res))


CHAT_SESSIONS = {}  # id -> ChatSession (live, independent of any browser connection)


def safe_cwd(cwd):
    rp = os.path.realpath(cwd or "")
    if not (rp == os.path.realpath(HOME) or rp.startswith(os.path.realpath(HOME) + os.sep)):
        return None
    return rp if os.path.isdir(rp) else None


def _sanitize_mode(m):
    return m if m in MODE_PRESETS else "full-access"   # default: no approval, full access

_EFFORT_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _model_catalog_entry(model=""):
    """Return the cached catalog entry for a model value, resolving `default`."""
    models = _models_cache.get("v") or []
    selected = _txt(model).strip()
    if selected and selected != "default":
        return next((m for m in models if m.get("id") == selected), None)
    default_id = _configured_default_model()
    return (next((m for m in models if m.get("id") == default_id), None)
            or next((m for m in models if m.get("isDefault")), None))


def _sanitize_effort(e, model=""):
    """Validate an effort against the selected model's live catalog metadata.

    Unsupported or missing values fall back to that model's advertised default.
    When the catalog is unavailable, accept a safe protocol-shaped identifier so
    newly introduced effort ids remain forward-compatible.
    """
    effort = _txt(e).strip().lower()
    spec = _model_catalog_entry(model)
    if spec:
        allowed = [x for x in (spec.get("reasoningEfforts") or [])
                   if isinstance(x, str) and _EFFORT_ID_RE.fullmatch(x)]
        if effort in allowed:
            return effort
        default = _txt(spec.get("defaultReasoningEffort")).strip().lower()
        if default in allowed:
            return default
        if "xhigh" in allowed:
            return "xhigh"
        if allowed:
            return allowed[0]
    if effort and _EFFORT_ID_RE.fullmatch(effort):
        return effort
    return "xhigh"


class ChatSession:
    """A persistent `codex app-server` JSON-RPC client (one codex *thread* per
    chat), independent of any browser connection. Survives viewer detach; ends
    only on explicit end(). Provides per-action approval and interrupt().

    The PUBLIC surface (start/attach/detach/send_user/interrupt/resolve_approval/
    resolve_answer/set_model/set_mode/terminate/preload/title/turn_age + the
    attributes the controller reads) mirrors the claude build exactly, so the
    WebSocket controller and the entire frontend are unchanged — codex events are
    normalized into the SAME shape (assistant_text / thinking / tool_use /
    tool_result / turn_done / ready / compacted / approval / question / tokens /
    context)."""

    def __init__(self, sid, cwd, model, mode, resume_cc=None, effort="medium"):
        self.id = sid
        self.cwd = cwd
        self.model = model or ""        # "" / "default" → codex config default
        self.display_model = _display_model(self.model)
        self.mode = _sanitize_mode(mode)  # approval preset (see _mode_policy)
        effort_model = self.model if self.model and self.model != "default" else self.display_model
        self.effort = _sanitize_effort(effort, effort_model)   # reasoning effort (per-turn)
        self.proc = None                # the `codex app-server` subprocess
        self.log = []                   # normalized-event history, for replay
        self.event_seq = 0              # monotonic sequence for incremental tab attach
        self.viewers = set()            # attached ChatSockets
        self.busy = False
        self.ended = False
        self.cc_id = resume_cc          # codex threadId; preset when resuming
        self.resume_cc = resume_cc
        self.thread_id = resume_cc
        self.turn_id = None             # current turn id (for steer / interrupt)
        self._pending = {}              # aid -> asyncio.Future (unused; kept for parity)
        self._req = {}                  # aid -> {"rpc": rpc_id, "kind": ..., "raw": ...}
        self._aid = 0
        self.ctx = None                 # latest context-window usage
        self.usage = None               # latest rolling rate-limit usage (5h + weekly)
        self.queue = []                 # messages typed while busy (steering)
        self._qid = 0
        self.turn_started = None
        self.turn_word = 0
        self.compacting = False
        # JSON-RPC plumbing
        self._rpc_id = 0
        self._waiters = {}              # rpc_id -> Future (responses to our requests)
        self._stderr_tail = []          # recent app-server stderr lines (for error msgs)
        self._items = {}               # itemId -> bookkeeping for delta/result pairing
        self.thread_status = {"type": "notLoaded"}
        self.turn_diff = ""
        self.plan = None
        self.goal = None
        self.safety_buffering = None
        self.subagents = {}             # agentThreadId -> status shown in Subagents panel
        self.transcript_meta = {}
        # live streaming token counters (ephemeral; pushed to the pill, never logged)
        self._tok_up = 0
        self._tok_out = 0
        self._tok_chars = 0
        self._tok_exact = False
        self._last_tok_emit = 0.0
        self._step = 0
        # session-recap bookkeeping
        self.last_activity = time.time()
        self.recap_for = None
        self.recap_busy = False
        self._compact_turn = False

    # ───────── lifecycle ─────────
    def preload(self):
        """Populate history from the on-disk rollout before resuming."""
        if self.resume_cc:
            self.transcript_meta = load_transcript_meta(self.resume_cc)
            if (not self.model or self.model == "default") and self.transcript_meta.get("model"):
                self.display_model = self.transcript_meta["model"]
            if self.transcript_meta.get("ctx"):
                self.ctx = dict(self.transcript_meta["ctx"])
            self.log = load_transcript_events(self.resume_cc)
            for ev in self.log:
                self.event_seq += 1
                ev["_seq"] = self.event_seq
                if ev.get("kind") == "subagent_activity":
                    agent = dict(ev.get("subagent") or {})
                    tid = agent.get("threadId")
                    if tid:
                        self.subagents[tid] = {**self.subagents.get(tid, {}), **agent}

    def title(self):
        if self.cc_id:
            nm = load_names().get(self.cc_id)
            if nm:
                return nm
        for e in self.log:
            if e.get("kind") == "user_text" and (e.get("text") or "").strip():
                return e["text"].strip().replace("\n", " ")[:60]
        return ""

    async def start(self):
        if not CODEX_BIN or not os.path.exists(CODEX_BIN):
            raise RuntimeError("codex CLI not found")
        self.proc = await asyncio.create_subprocess_exec(
            CODEX_BIN, "app-server",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=self.cwd,
            limit=APPSERVER_STREAM_LIMIT)
        tornado.ioloop.IOLoop.current().spawn_callback(self._reader)
        tornado.ioloop.IOLoop.current().spawn_callback(self._stderr_reader)
        # 1. handshake
        await self._request("initialize", {"clientInfo": {
            "name": "codex-console", "version": "0.1.0", "title": "Codex Console"},
            "capabilities": {"experimentalApi": True, "requestAttestation": False}})
        self._notify("initialized")
        # 2. start (or resume) a thread
        ap, sbx = _mode_policy(self.mode)
        base = {"cwd": self.cwd, "approvalPolicy": ap, "sandbox": sbx}
        if self.model and self.model != "default":
            base["model"] = self.model
        res = None
        if self.resume_cc:
            try:
                res = await self._request("thread/resume", {**base, "threadId": self.resume_cc})
            except Exception:
                res = None
        if res is None:
            res = await self._request("thread/start", base)
        th = (res or {}).get("thread") or {}
        self.thread_id = th.get("id") or self.thread_id
        self.cc_id = self.thread_id
        meta_model = self.transcript_meta.get("model") if (not self.model or self.model == "default") else ""
        self.display_model = _display_model(self.model, meta_model or (res or {}).get("model"))
        self.effort = (res or {}).get("reasoningEffort") or self.effort
        self.thread_status = th.get("status") or self.thread_status
        if self.cc_id:
            save_pref(self.cc_id, mode=self.mode, model=self.model, effort=self.effort)
        # Defer the "ready" push one loop tick: start() returns to the controller,
        # which then attaches the viewer and sends "started"; only after that does
        # this fire, so the freshly-attached viewer actually receives "ready"
        # (mirrors how the claude build's init event arrives async, post-attach).
        def _ready():
            self._push([{"kind": "ready", "session_id": self.cc_id,
                         "model": self.model or "default", "display_model": self.display_model,
                         "cwd": self.cwd,
                         "effort": self.effort}])
        tornado.ioloop.IOLoop.current().spawn_callback(_ready)
        # pull the rolling usage limits up front so the header's 5h + weekly
        # meters show immediately, without waiting for the first turn's push.
        async def _rl():
            try:
                r = await self._request("account/rateLimits/read", {})
                self._on_rate_limits(r or {})
            except Exception:
                pass
        tornado.ioloop.IOLoop.current().spawn_callback(_rl)

    # ───────── JSON-RPC framing ─────────
    def _send(self, obj):
        if not self.proc or not self.proc.stdin:
            return
        try:
            self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        except Exception:
            pass

    def _notify(self, method, params=None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _request(self, method, params=None, timeout=120):
        self._rpc_id += 1
        rid = self._rpc_id
        fut = asyncio.get_running_loop().create_future()
        self._waiters[rid] = fut
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._waiters.pop(rid, None)

    async def _reader(self):
        try:
            while self.proc and self.proc.stdout:
                try:
                    line = await self.proc.stdout.readline()
                except (asyncio.LimitOverrunError, ValueError) as ex:
                    # One JSON-RPC line exceeded APPSERVER_STREAM_LIMIT (a pathologically
                    # large command output / file read / diff). readline() has already
                    # drained the offending bytes, so DROP just this one message and keep
                    # going — the app-server is still alive; do NOT end the session.
                    self._emit({"type": "stderr",
                                "text": "dropped oversized app-server message: %r" % ex})
                    continue
                if not line:
                    break
                s = line.decode("utf-8", "replace").strip()
                if not s:
                    continue
                try:
                    m = json.loads(s)
                except Exception:
                    continue
                try:
                    self._dispatch_rpc(m)
                except Exception as ex:
                    self._emit({"type": "stderr", "text": "dispatch error: %r" % ex})
        except Exception as ex:
            self._emit({"type": "stderr", "text": "stream ended: %r" % ex})
        # the app-server's stdout closed → it exited. Fail every in-flight request
        # immediately (don't hang the initialize handshake for the full timeout).
        rc = None
        try:
            rc = self.proc.returncode if self.proc else None
        except Exception:
            pass
        tail = "\n".join(self._stderr_tail[-12:]).strip()
        err = RuntimeError("codex app-server exited (code=%s)%s"
                           % (rc, (": " + tail[-600:]) if tail else ""))
        for fut in list(self._waiters.values()):
            if not fut.done():
                fut.set_exception(err)
        self._waiters.clear()
        self.busy = False
        self.ended = True
        self._emit({"type": "exit", "code": rc if rc is not None else 0})

    async def _stderr_reader(self):
        try:
            while self.proc and self.proc.stderr:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                t = line.decode("utf-8", "replace").rstrip()
                if t:
                    self._stderr_tail.append(t)
                    if len(self._stderr_tail) > 40:
                        self._stderr_tail = self._stderr_tail[-40:]
                # app-server logs tracing to stderr — surface only hard errors live
                if " ERROR " in t or "Error:" in t:
                    self._emit({"type": "stderr", "text": _cap(t, 400)})
        except Exception:
            pass

    def _dispatch_rpc(self, m):
        # response to one of our requests
        if "id" in m and ("result" in m or "error" in m) and "method" not in m:
            fut = self._waiters.get(m.get("id"))
            if fut and not fut.done():
                if "error" in m:
                    fut.set_exception(RuntimeError(json.dumps(m.get("error"))))
                else:
                    fut.set_result(m.get("result"))
            return
        method = m.get("method")
        if not method:
            return
        if "id" in m:                       # server → client REQUEST (needs a response)
            self._on_request(m["id"], method, m.get("params") or {})
        else:                               # notification
            self._on_note(method, m.get("params") or {})

    # ───────── subagents ─────────
    def subagents_snapshot(self):
        rows = [_subagent_public(x) for x in self.subagents.values()]
        rows.sort(key=lambda r: (not r.get("active"), -(r.get("updatedAt") or 0)))
        return rows

    def _emit_subagents(self):
        self._emit({"type": "events", "events": [{"kind": "subagents",
                    "subagents": self.subagents_snapshot()}]})

    def _upsert_subagent(self, thread_id, state=None, activity=None, **fields):
        thread_id = _txt(thread_id).strip()
        if not thread_id:
            return None
        now = time.time()
        rec = self.subagents.setdefault(
            thread_id, {"threadId": thread_id, "createdAt": now})
        rec["updatedAt"] = now
        if activity:
            rec["activity"] = activity
            if activity in ("started", "interacted"):
                state = state or "running"
            elif activity in ("completed", "interrupted"):
                state = state or activity
        if state:
            state = _status_type(state)
            rec["state"] = {
                "active": "running",
                "idle": rec.get("state") or "running",
                "notLoaded": rec.get("state") or "notLoaded",
            }.get(state, state)
        elif not rec.get("state"):
            rec["state"] = "running"
        for key, val in fields.items():
            if val is not None and val != "":
                rec[key] = _cap(_txt(val), 1200) if isinstance(val, str) else val
        return _subagent_public(rec)

    def _update_subagents_from_collab(self, item):
        ids = list(item.get("receiverThreadIds") or [])
        states = item.get("agentsStates") if isinstance(item.get("agentsStates"), dict) else {}
        for tid in states:
            if tid not in ids:
                ids.append(tid)
        changed = False
        for tid in ids:
            st = states.get(tid) if isinstance(states.get(tid), dict) else {}
            self._upsert_subagent(
                tid,
                state=st.get("status") or ("pendingInit" if item.get("status") not in ("completed", "success") else None),
                prompt=item.get("prompt"),
                message=st.get("message"),
                model=item.get("model"),
                reasoningEffort=item.get("reasoningEffort"),
                tool=item.get("tool"))
            changed = True
        if changed:
            self._emit_subagents()

    def _on_subagent_activity(self, item, book=None):
        if book is not None and book.get("subagent_activity_seen"):
            return
        if book is not None:
            book["subagent_activity_seen"] = True
        tid = item.get("agentThreadId")
        rec = self._upsert_subagent(
            tid, activity=item.get("kind") or "interacted",
            agentPath=item.get("agentPath"))
        if not rec:
            return
        self._push([{"kind": "subagent_activity", "subagent": rec,
                     "activity": item.get("kind") or rec.get("activity")}])
        self._emit_subagents()

    def _on_foreign_thread_note(self, method, p):
        tid = _txt((p or {}).get("threadId")).strip()
        if not tid or not self.thread_id or tid == self.thread_id:
            return False
        if method == "thread/status/changed":
            self._upsert_subagent(tid, state=p.get("status"))
            self._emit_subagents()
            return True
        item = (p or {}).get("item") if isinstance((p or {}).get("item"), dict) else {}
        msg = ""
        if item.get("type") == "agentMessage" and item.get("text"):
            msg = item.get("text")
        elif method.startswith("turn/"):
            msg = method.replace("/", " ")
        elif method.startswith("item/"):
            msg = item.get("type") or method.replace("/", " ")
        self._upsert_subagent(tid, state="running", message=msg)
        self._emit_subagents()
        return True

    async def read_subagent_thread(self, thread_id, limit=160):
        thread_id = _txt(thread_id).strip()
        if not _thread_ref_ok(thread_id):
            return {"ok": False, "error": "invalid subagent thread id", "threadId": thread_id}
        if thread_id not in self.subagents:
            return {"ok": False, "error": "unknown subagent for this session", "threadId": thread_id}
        if not self.proc or self.ended:
            return {"ok": False, "error": "session is not running", "threadId": thread_id,
                    "subagent": _subagent_public(self.subagents.get(thread_id))}
        try:
            limit = int(limit or 160)
        except Exception:
            limit = 160
        limit = max(20, min(limit, 400))
        thread, entries, error = {}, [], ""
        try:
            res = await self._request("thread/read", {"threadId": thread_id, "includeTurns": False})
            thread = (res or {}).get("thread") or {}
            self._upsert_subagent(
                thread_id, state=thread.get("status"), title=thread.get("name") or thread.get("preview"),
                cwd=thread.get("cwd"), agentRole=thread.get("agentRole"),
                agentNickname=thread.get("agentNickname"))
        except Exception as ex:
            error = "thread/read failed: %r" % ex
        try:
            res = await self._request("thread/items/list", {
                "threadId": thread_id, "limit": limit, "sortDirection": "asc"})
            entries = (res or {}).get("data") or []
        except Exception as ex:
            if error:
                error += "; "
            error += "thread/items/list failed: %r" % ex
            try:
                res = await self._request("thread/read", {"threadId": thread_id, "includeTurns": True})
                thread = (res or {}).get("thread") or thread or {}
                for turn in thread.get("turns") or []:
                    for item in turn.get("items") or []:
                        entries.append({"turnId": turn.get("id"), "item": item})
            except Exception:
                pass
        messages = []
        for entry in entries:
            msg = _subagent_item_message(entry)
            if msg:
                messages.append(msg)
        return {"ok": not bool(error) or bool(messages), "error": error,
                "threadId": thread_id, "subagent": _subagent_public(self.subagents.get(thread_id)),
                "thread": {
                    "id": thread.get("id") or thread_id,
                    "name": thread.get("name") or thread.get("preview"),
                    "preview": thread.get("preview"),
                    "cwd": thread.get("cwd"),
                    "status": _status_type(thread.get("status")) or None,
                    "createdAt": thread.get("createdAt"),
                    "updatedAt": thread.get("updatedAt"),
                },
                "messages": messages}

    # ───────── inbound: notifications → normalized events ─────────
    def _on_note(self, method, p):
        if method == "thread/started":
            th = p.get("thread") or {}
            parent_id = th.get("parentThreadId")
            if th.get("id") and parent_id and self.thread_id and parent_id == self.thread_id:
                self._upsert_subagent(
                    th["id"], state=th.get("status"), title=th.get("name") or th.get("preview"),
                    cwd=th.get("cwd"), agentRole=th.get("agentRole"),
                    agentNickname=th.get("agentNickname"))
                self._emit_subagents()
                return
            if th.get("id"):
                self.cc_id = self.thread_id = th["id"]
            self.thread_status = th.get("status") or self.thread_status
            self._emit({"type": "events", "events": [{"kind": "thread_status",
                        "status": _status_type(self.thread_status),
                        "flags": _active_flags(self.thread_status)}]})
        elif self._on_foreign_thread_note(method, p):
            return
        elif method == "turn/started":
            turn = p.get("turn") or {}
            self.turn_id = turn.get("id")
            if turn.get("items"):
                for item in turn.get("items") or []:
                    self._on_item(item, started=True)
        elif method == "turn/completed":
            turn = p.get("turn") or {}
            for item in turn.get("items") or []:
                self._on_item(item, started=False)
            self._finish_turn(error=_status_type(turn.get("status")) == "failed")
        elif method == "turn/failed":
            err = (p.get("error") or {})
            self._push([{"kind": "notice", "text": "turn failed: " + _txt(err.get("message") or err)}])
            self._finish_turn(error=True)
        elif method == "item/started":
            self._on_item(p.get("item") or {}, started=True)
        elif method == "item/completed":
            self._on_item(p.get("item") or {}, started=False)
        elif method == "item/agentMessage/delta":
            d = p.get("delta") or ""
            if d:
                self._on_item_delta(p.get("itemId"), "assistant", d)
            if d and not self._tok_exact:
                self._tok_chars += len(d)
                self._tok_out = max(self._tok_out, (self._tok_chars + 3) // 4)
                self._emit_tokens()
        elif method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
            d = p.get("delta") or ""
            if d:
                self._on_item_delta(p.get("itemId"), "thinking", d)
            if d and not self._tok_exact:
                self._tok_chars += len(d)
                self._tok_out = max(self._tok_out, (self._tok_chars + 3) // 4)
                self._emit_tokens()
        elif method == "item/reasoning/summaryPartAdded":
            iid = p.get("itemId")
            book = self._book_item(iid, "reasoning")
            book["text"] = (book.get("text") or "").rstrip() + "\n"
        elif method == "item/plan/delta":
            d = p.get("delta") or ""
            if d:
                self._on_item_delta(p.get("itemId"), "plan", d, turn_id=p.get("turnId"))
        elif method == "turn/plan/updated":
            self.plan = {"explanation": p.get("explanation"), "plan": p.get("plan") or []}
            self._push([{"kind": "plan", "turnId": p.get("turnId"),
                         "explanation": _cap(_txt(p.get("explanation")), 4000),
                         "plan": p.get("plan") or []}])
        elif method == "turn/diff/updated":
            self.turn_diff = _cap(p.get("diff") or "", RESULT_CAP * 4)
            self._emit({"type": "events", "events": [{"kind": "turn_diff",
                        "turnId": p.get("turnId"), "diff": self.turn_diff}]})
        elif method == "item/fileChange/patchUpdated":
            self._on_file_change_updated(p.get("itemId"), p.get("changes") or [])
        elif method == "item/fileChange/outputDelta":
            self._on_tool_delta(p.get("itemId"), p.get("delta") or "")
        elif method == "item/commandExecution/outputDelta":
            self._on_tool_delta(p.get("itemId"), p.get("delta") or "")
        elif method == "item/commandExecution/terminalInteraction":
            self._push([{"kind": "tool_progress", "toolId": p.get("itemId"),
                         "text": _cap(_txt(p.get("stdin")), 1200)}])
        elif method in ("command/exec/outputDelta", "process/outputDelta"):
            delta = _decode_b64_text(p.get("deltaBase64"))
            pid = p.get("processId") or p.get("processHandle")
            if delta and pid:
                self._on_tool_delta(pid, delta, stream=p.get("stream"),
                                    cap_reached=bool(p.get("capReached")))
        elif method == "item/mcpToolCall/progress":
            self._push([{"kind": "tool_progress", "toolId": p.get("itemId"),
                         "text": _cap(_txt(p.get("message")), 1200)}])
        elif method == "thread/status/changed":
            self.thread_status = p.get("status") or {}
            st = _status_type(self.thread_status)
            if st == "active":
                self.busy = True
            elif st in ("idle", "notLoaded") and not self.turn_id:
                self.busy = False
            self._emit({"type": "events", "events": [{"kind": "thread_status",
                        "status": st, "flags": _active_flags(self.thread_status)}]})
        elif method == "thread/settings/updated":
            self._on_settings_updated(p.get("threadSettings") or {})
        elif method == "thread/name/updated":
            name = p.get("threadName") or ""
            if name:
                self._emit({"type": "events", "events": [{"kind": "notice",
                            "text": "thread renamed: " + _cap(name, 120)}]})
        elif method == "thread/goal/updated":
            self.goal = p.get("goal") or {}
            self._push([{"kind": "goal", "goal": self.goal}])
        elif method == "thread/goal/cleared":
            self.goal = None
            self._push([{"kind": "goal", "goal": None}])
        elif method == "thread/tokenUsage/updated":
            self._on_token_usage(p.get("tokenUsage") or {})
        elif method == "account/rateLimits/updated":
            self._on_rate_limits(p.get("rateLimits") or {})
        elif method == "thread/compacted":
            self.compacting = False
            self._push([{"kind": "compacted", "trigger": "manual",
                         "pre": None, "post": None, "ms": None}])
            if self.busy and self._compact_turn:
                self._finish_turn(error=False, compact=True)
        elif method == "error":
            self._push([{"kind": "notice", "text": _txt(p.get("message") or p)}])
        elif method == "model/rerouted":
            frm, to = p.get("fromModel"), p.get("toModel")
            if to:
                self.display_model = to
            self._push([{"kind": "model_rerouted", "from": frm, "to": to,
                         "reason": _txt(p.get("reason"))}])
        elif method == "model/safetyBuffering/updated":
            self.safety_buffering = p
            if p.get("showBufferingUi"):
                self._emit({"type": "events", "events": [{"kind": "safety_buffering",
                            "model": p.get("model"), "fasterModel": p.get("fasterModel"),
                            "reasons": p.get("reasons") or []}]})
        elif method in ("warning", "guardianWarning", "deprecationNotice", "configWarning"):
            msg = p.get("message") or p.get("warning") or p.get("text") or p
            self._push([{"kind": "notice", "text": _cap(_txt(msg), 1200)}])
        elif method in ("thread/archived", "thread/deleted", "thread/unarchived",
                        "thread/closed", "thread/reverted"):
            self._emit({"type": "events", "events": [{"kind": "thread_lifecycle",
                        "method": method}]})
        # ignored: hook/* details, mcpServer status spam, account/updated,
        # fuzzyFileSearch/*, realtime audio, and internal rawResponse/* payloads.

    def _book_item(self, item_id, item_type=None):
        if not item_id:
            return {}
        book = self._items.setdefault(item_id, {})
        if item_type and not book.get("type"):
            book["type"] = item_type
        return book

    def _on_item_delta(self, item_id, kind, delta, turn_id=None):
        if not item_id or not delta:
            return
        book = self._book_item(item_id, kind)
        book["streamed"] = True
        book["text"] = (book.get("text") or "") + delta
        ev_kind = {"assistant": "assistant_delta", "thinking": "thinking_delta",
                   "plan": "plan_delta"}.get(kind)
        if ev_kind:
            self._push([{"kind": ev_kind, "itemId": item_id, "turnId": turn_id,
                         "delta": _cap(delta, 4000)}])

    def _on_tool_delta(self, item_id, delta, stream=None, cap_reached=False):
        if not item_id or not delta:
            return
        book = self._book_item(item_id)
        book["output"] = _cap((book.get("output") or "") + delta, RESULT_CAP)
        self._emit({"type": "events", "events": [{"kind": "tool_delta",
                    "toolId": item_id, "delta": _cap(delta, 4000),
                    "stream": stream, "capReached": bool(cap_reached)}]})

    def _on_file_change_updated(self, item_id, changes):
        if not item_id:
            return
        inp = _file_change_input(changes)
        book = self._book_item(item_id, "fileChange")
        book["input"] = inp
        if not book.get("started"):
            book["started"] = True
            self._push([{"kind": "tool_use", "tool": "apply_patch",
                         "input": inp, "toolId": item_id}])
            self._maybe_steer()
            return
        self._push([{"kind": "tool_update", "toolId": item_id,
                     "tool": "apply_patch", "input": inp}])

    def _on_settings_updated(self, settings):
        if not isinstance(settings, dict):
            return
        model = settings.get("model")
        if model:
            self.display_model = model
        effort = settings.get("effort")
        if effort:
            self.effort = effort
        cwd = settings.get("cwd")
        if cwd:
            self.cwd = cwd
        self._emit({"type": "events", "events": [{"kind": "settings",
                    "model": self.model or "default",
                    "display_model": self.display_model,
                    "effort": self.effort,
                    "cwd": self.cwd,
                    "approvalPolicy": settings.get("approvalPolicy"),
                    "sandboxPolicy": settings.get("sandboxPolicy"),
                    "personality": settings.get("personality")}]})

    def _on_item(self, item, started):
        it = item.get("type")
        iid = item.get("id")
        book = self._book_item(iid, it)
        if iid and started:
            if book.get("started"):
                return
            book["started"] = True
        elif iid and not started:
            if book.get("completed"):
                return
            book["completed"] = True
        if it == "userMessage":
            return                          # echoed locally by _echo_user
        if it == "agentMessage":
            if not started:
                txt = item.get("text") or ""
                if txt.strip():
                    if book.get("streamed"):
                        if txt != book.get("text"):
                            book["text"] = txt
                            self._push([{"kind": "assistant_update", "itemId": iid,
                                         "text": _cap(txt)}])
                    else:
                        self._push([{"kind": "assistant_text", "itemId": iid,
                                     "text": _cap(txt)}])
            return
        if it == "reasoning":
            if not started:
                txt = _txt(item.get("summary") or item.get("content"))
                if txt.strip():
                    if book.get("streamed"):
                        if txt != book.get("text"):
                            book["text"] = txt
                            self._push([{"kind": "thinking_update", "itemId": iid,
                                         "text": _cap(txt)}])
                    else:
                        self._push([{"kind": "thinking", "itemId": iid,
                                     "text": _cap(txt)}])
            return
        if it == "plan":
            if not started:
                txt = _txt(item.get("text"))
                if txt.strip():
                    if book.get("streamed"):
                        if txt != book.get("text"):
                            book["text"] = txt
                            self._push([{"kind": "plan_text", "itemId": iid,
                                         "text": _cap(txt)}])
                    else:
                        self._push([{"kind": "plan_text", "itemId": iid,
                                     "text": _cap(txt)}])
            return
        if it == "commandExecution":
            raw_cmd = item.get("command")
            cmd = _shell_command_text(raw_cmd)
            if started:
                tool, shown = _codex_cmd(raw_cmd, item.get("commandActions"))
                self._push([{"kind": "tool_use", "tool": tool,
                             "input": {"command": _cap(cmd), "cwd": item.get("cwd"),
                                       "display": _cap(shown),
                                       "actions": item.get("commandActions") or []},
                             "toolId": iid}])
                self._maybe_steer()
            else:
                out = _txt(item.get("aggregatedOutput"))
                code = item.get("exitCode")
                err = (item.get("status") not in ("completed", "success")
                       or (code not in (0, None)))
                self._push([{"kind": "tool_result", "toolId": iid,
                             "content": _cap(out, RESULT_CAP), "isError": bool(err),
                             "exitCode": code, "durationMs": item.get("durationMs"),
                             "status": item.get("status")}])
            return
        if it == "fileChange":
            # codex apply_patch: each change carries {path, kind(add/update/delete),
            # diff(unified)}. Surface it as an EDIT (file path + real diff) so it
            # shows as a "see Changes" marker + a diff card in the drawer — not a
            # generic exec card.
            if started:
                inp = _file_change_input(item.get("changes") or [])
                book["input"] = inp
                self._push([{"kind": "tool_use", "tool": "apply_patch",
                             "input": inp, "toolId": iid}])
                self._maybe_steer()
            else:
                st = item.get("status")
                if st not in ("completed", "success", "applied", None):
                    self._push([{"kind": "tool_result", "toolId": iid,
                                 "content": "patch %s" % st, "isError": True,
                                 "status": st}])
            return
        if it == "mcpToolCall":
            name = "%s.%s" % (item.get("server") or "mcp", item.get("tool") or "")
            if started:
                self._push([{"kind": "tool_use", "tool": name,
                             "input": _cap_input(item.get("arguments")), "toolId": iid}])
                self._maybe_steer()
            else:
                res = item.get("error") or item.get("result")
                self._push([{"kind": "tool_result", "toolId": iid,
                             "content": _cap(_txt(res), RESULT_CAP),
                             "isError": bool(item.get("error")),
                             "durationMs": item.get("durationMs"),
                             "status": item.get("status")}])
            return
        if it == "dynamicToolCall":
            name = ".".join(x for x in (item.get("namespace"), item.get("tool")) if x)
            if started:
                self._push([{"kind": "tool_use", "tool": name or "dynamicTool",
                             "input": _cap_input(item.get("arguments")), "toolId": iid}])
                self._maybe_steer()
            else:
                content = item.get("contentItems")
                self._push([{"kind": "tool_result", "toolId": iid,
                             "content": _cap(_txt(content), RESULT_CAP),
                             "isError": item.get("success") is False,
                             "durationMs": item.get("durationMs"),
                             "status": item.get("status")}])
            return
        if it == "collabAgentToolCall":
            self._update_subagents_from_collab(item)
            if started:
                self._push([{"kind": "tool_use", "tool": "Agent",
                             "input": {"tool": item.get("tool"), "prompt": item.get("prompt"),
                                       "model": item.get("model"),
                                       "reasoningEffort": item.get("reasoningEffort"),
                                       "receiverThreadIds": item.get("receiverThreadIds") or []},
                             "toolId": iid}])
                self._maybe_steer()
            else:
                self._push([{"kind": "tool_result", "toolId": iid,
                                 "content": _cap(_txt(item.get("agentsStates")), RESULT_CAP),
                                 "isError": item.get("status") == "failed",
                                 "status": item.get("status")}])
            return
        if it == "subAgentActivity":
            self._on_subagent_activity(item, book=book)
            return
        if it == "webSearch":
            if started:
                self._push([{"kind": "tool_use", "tool": "web_search",
                             "input": _web_search_input(item), "toolId": iid}])
            else:
                self._push([{"kind": "tool_result", "toolId": iid,
                             "content": _cap(_web_result_text(item.get("results")), RESULT_CAP),
                             "isError": item.get("status") == "failed",
                             "status": item.get("status") or "completed"}])
            return
        if it == "imageView":
            if started:
                self._push([{"kind": "tool_use", "tool": "ViewImage",
                             "input": {"path": item.get("path")}, "toolId": iid}])
            else:
                self._push([{"kind": "tool_result", "toolId": iid,
                             "content": item.get("path") or "", "isError": False}])
            return
        if it == "imageGeneration":
            if started:
                self._push([{"kind": "tool_use", "tool": "ImageGeneration",
                             "input": _cap_input(item), "toolId": iid}])
            else:
                self._push([{"kind": "tool_result", "toolId": iid,
                             "content": _cap(_txt(item), RESULT_CAP),
                             "isError": False}])
            return
        if it == "contextCompaction":
            if started:
                self.compacting = True
                self._push([{"kind": "compacting", "word": self.turn_word}])
            else:
                self.compacting = False
                self._push([{"kind": "compacted", "trigger": "auto",
                             "pre": None, "post": None, "ms": None}])
            return
        if it in ("enteredReviewMode", "exitedReviewMode"):
            self._push([{"kind": "notice", "text": it}])

    def _finish_turn(self, error=False, compact=False):
        if not self.busy:
            return
        ev = {"kind": "turn_done", "subtype": ("compact" if compact else "ok"),
              "isError": bool(error)}
        if not self._compact_turn:
            ev["done_word"] = secrets.choice(DONE_PAST)
            ev["done_at"] = time.time()
            if self.turn_started:
                ev["dur_ms"] = int((time.time() - self.turn_started) * 1000)
        self._compact_turn = False
        self.busy = False
        self.turn_started = None
        self.turn_id = None
        self.compacting = False
        self.last_activity = time.time()
        self._push([ev])
        self._drain_queue()

    def _on_token_usage(self, tu):
        total = _pick(tu, "total", "total_token_usage", "totalTokenUsage") or {}
        last = _pick(tu, "last", "last_token_usage", "lastTokenUsage") or {}
        mx = _pick(tu, "modelContextWindow", "model_context_window")
        # Context-window occupancy = the LAST request's input tokens (the full
        # prompt the model saw, incl. cached history). NOT total.totalTokens —
        # that's the cumulative sum over the whole thread and runs to thousands of
        # percent on a long or resumed session (the "2508%" bug).
        cur = _pick(last, "inputTokens", "input_tokens")
        if cur is None:
            cur = _pick(last, "totalTokens", "total_tokens")
        if cur is not None and mx:
            if (not self.model or self.model == "default") and self.cc_id:
                meta_model = (self.transcript_meta or {}).get("model")
                if meta_model:
                    self.display_model = meta_model
            cfg_mx = _configured_context_window()
            # For compaction decisions, show occupancy against the effective
            # window reported by app-server, not a larger local config override.
            self.ctx = {"totalTokens": cur, "maxTokens": mx,
                        "reportedMaxTokens": mx, "configuredMaxTokens": cfg_mx,
                        "percentage": round(cur * 100.0 / mx, 1),
                        "model": self.display_model or self.model or None}
            self._emit({"type": "context", "ctx": self.ctx})
        li = _pick(last, "inputTokens", "input_tokens")
        lc = _pick(last, "cachedInputTokens", "cached_input_tokens") or 0
        lo = _pick(last, "outputTokens", "output_tokens")
        if li is not None:
            self._tok_up = max(0, li - lc)   # fresh (uncached) upload this request
        if lo is not None:
            self._tok_out = lo
            self._tok_exact = True
        self._emit_tokens(force=True)

    def _on_rate_limits(self, rl):
        """Rolling usage limits (codex `account/rateLimits/*`): update the global
        (for the /api/usage poll + fresh page loads) AND push live to viewers so
        the header's 5h + weekly meters refresh immediately, not just every 60s."""
        u = _set_usage(rl)
        if u:
            self.usage = u
            self._emit({"type": "usage", "usage": u})

    # ───────── inbound: server requests (approvals / questions) ─────────
    def _on_request(self, rpc_id, method, p):
        self._aid += 1
        aid = "ap%d" % self._aid
        if method in ("item/commandExecution/requestApproval", "execCommandApproval"):
            self._req[aid] = {"rpc": rpc_id, "kind": "exec"}
            raw_cmd = p.get("command") or ""
            cmd = _shell_command_text(raw_cmd)
            tool, shown = _codex_cmd(raw_cmd, p.get("commandActions"))
            self._push([{"kind": "approval", "aid": aid, "tool": tool,
                         "input": {"command": _cap(cmd) if cmd else None,
                                   "display": _cap(shown),
                                   "actions": p.get("commandActions") or [],
                                   "reason": p.get("reason"), "cwd": p.get("cwd")},
                         "toolId": p.get("itemId"), "always": True}])
        elif method in ("item/fileChange/requestApproval", "applyPatchApproval"):
            self._req[aid] = {"rpc": rpc_id, "kind": "patch"}
            self._push([{"kind": "approval", "aid": aid, "tool": "apply_patch",
                         "input": {"reason": p.get("reason"),
                                   "changes": _cap(_summarize_changes(
                                       p.get("changes") or p.get("fileChange")))},
                         "toolId": p.get("itemId"), "always": True}])
        elif method == "item/permissions/requestApproval":
            self._req[aid] = {"rpc": rpc_id, "kind": "perm"}
            self._push([{"kind": "approval", "aid": aid, "tool": "permissions",
                         "input": {"reason": p.get("reason")},
                         "toolId": p.get("itemId"), "always": False}])
        elif method == "item/tool/requestUserInput":
            qs_raw = p.get("questions") or []
            qs = [{"question": q.get("question"), "header": q.get("header"),
                   "options": [{"label": o.get("label"), "description": o.get("description")}
                               for o in (q.get("options") or [])]}
                  for q in qs_raw]
            self._req[aid] = {"rpc": rpc_id, "kind": "input", "raw": qs_raw}
            self._push([{"kind": "question", "aid": aid, "questions": qs}])
        else:
            # unknown server request → respond empty so codex isn't left blocked
            self._send({"jsonrpc": "2.0", "id": rpc_id, "result": {}})

    def resolve_approval(self, aid, allow, always=False):
        meta = self._req.pop(aid, None)
        self._pending.pop(aid, None)
        if not meta:
            return
        kind, rpc = meta["kind"], meta["rpc"]
        if kind in ("exec", "patch"):
            dec = ("acceptForSession" if (allow and always) else "accept") if allow else "decline"
        else:                                # perm / other
            dec = "accept" if allow else "decline"
        self._send({"jsonrpc": "2.0", "id": rpc, "result": {"decision": dec}})
        self._push([{"kind": "approval_resolved", "aid": aid,
                     "allow": bool(allow), "always": bool(allow and always)}])

    def resolve_answer(self, aid, answers):
        meta = self._req.pop(aid, None)
        self._pending.pop(aid, None)
        if not meta:
            return
        rpc = meta["rpc"]
        raw = meta.get("raw") or []
        picks = answers if isinstance(answers, list) else []
        out = []
        for i, q in enumerate(raw):
            val = picks[i] if i < len(picks) else None
            if val:
                out.append({"id": q.get("id"), "answers": [val]})
        if not out:
            # dismissed → answer with empty selections (codex continues the turn)
            self._send({"jsonrpc": "2.0", "id": rpc, "result": {"answers": []}})
            self._push([{"kind": "question_resolved", "aid": aid, "answers": None}])
            return
        self._send({"jsonrpc": "2.0", "id": rpc, "result": {"answers": out}})
        named = {}
        for i, q in enumerate(raw):
            val = picks[i] if i < len(picks) else None
            if val:
                named[q.get("question") or q.get("header") or ("Q%d" % (i + 1))] = val
        self._push([{"kind": "question_resolved", "aid": aid, "answers": named or None}])

    # ───────── outbound: user messages / turns ─────────
    def turn_age(self):
        return (time.time() - self.turn_started) if (self.busy and self.turn_started) else 0

    def _status_snapshot(self):
        ap, sbx = _mode_policy(self.mode)
        return {
            "generated_at": time.time(),
            "session": {
                "id": self.id,
                "thread_id": self.thread_id or self.cc_id or "",
                "cwd": self.cwd,
                "model": self.model or "default",
                "display_model": self.display_model,
                "effort": self.effort,
                "mode": self.mode,
                "approval_policy": ap,
                "sandbox": sbx,
                "busy": bool(self.busy),
                "ended": bool(self.ended),
                "compacting": bool(self.compacting),
                "queued": len(self.queue),
                "viewers": len(self.viewers),
                "turn_age": round(self.turn_age(), 1),
            },
            "service": {
                "bind": BIND,
                "port": PORT,
                "auth": bool(AUTH),
                "recap": bool(RECAP_ENABLED),
                "recap_idle_sec": RECAP_IDLE_SEC,
                "configured_context_window": _configured_context_window(),
                "codex": bool(HAVE_CODEX),
            },
            "context": self.ctx,
            "usage": self.usage or _CODEX_USAGE or None,
            "git": git_status_brief(self.cwd),
        }

    def _handle_local_command(self, text, images):
        cmd = text.strip().split(None, 1)[0] if text.strip() else ""
        if cmd != "/status":
            return False
        evs = [{"kind": "user_text", "text": _cap(text)}]
        if images:
            evs[0]["images"] = len(images)
        evs.append({"kind": "status", "status": self._status_snapshot()})
        self.last_activity = time.time()
        self._push(evs)
        return True

    def send_user(self, text, images=None):
        images = [im for im in (images or []) if im.get("data")]
        if (not text.strip() and not images) or not self.proc or self.ended:
            return
        if self._handle_local_command(text, images):
            return
        if self.busy:
            self._qid += 1
            qid = "q%d" % self._qid
            self.queue.append({"qid": qid, "text": text, "images": images})
            ev = {"kind": "queued", "qid": qid, "text": _cap(text)}
            if images:
                ev["images"] = len(images)
            self._push([ev])
            return
        self._dispatch(text, images)

    def _make_input(self, text, images):
        """Codex UserInput[] for turn/start. Images are written to temp files and
        sent as localImage (robust; the data-URL image variant is finicky)."""
        inp = []
        if text and text.strip():
            inp.append({"type": "text", "text": text})
        for im in (images or []):
            data = im.get("data")
            if not data:
                continue
            try:
                raw = base64.b64decode(data)
                ext = ".png"
                mt = im.get("media_type") or "image/png"
                if "jpeg" in mt or "jpg" in mt:
                    ext = ".jpg"
                elif "webp" in mt:
                    ext = ".webp"
                path = os.path.join(tempfile.gettempdir(),
                                    "codexcon-%s%s" % (secrets.token_hex(4), ext))
                with open(path, "wb") as f:
                    f.write(raw)
                inp.append({"type": "localImage", "path": path})
            except Exception:
                pass
        if not inp:
            inp.append({"type": "text", "text": text or ""})
        return inp

    def _echo_user(self, text, images, qid=None, start=False):
        evs = []
        if qid:
            evs.append({"kind": "dequeued", "qid": qid})
        if start:
            evs.append({"kind": "turn_start", "word": self.turn_word})
        ue = {"kind": "user_text", "text": _cap(text)}
        if images:
            ue["images"] = len(images)
        evs.append(ue)
        self._push(evs)

    def _dispatch(self, text, images, qid=None, start=True):
        if start:
            self.busy = True
            self.turn_started = time.time()
            self.turn_word = secrets.randbelow(100000)
            self._tok_up = self._tok_out = self._tok_chars = 0
            self._tok_exact = False
            self._step = 0
            cmd0 = text.strip().split(None, 1)[0] if text.strip() else ""
            self.compacting = (cmd0 == "/compact")
            self._compact_turn = self.compacting
        self._echo_user(text, images, qid=qid, start=start)
        if start and self.compacting:
            self._push([{"kind": "compacting", "word": self.turn_word}])
            tid = self.thread_id
            async def _c():
                try:
                    await self._request("thread/compact/start", {"threadId": tid})
                except Exception as ex:
                    self._push([{"kind": "notice", "text": "compact failed: %r" % ex}])
                    self._finish_turn(error=True)
            tornado.ioloop.IOLoop.current().spawn_callback(_c)
            return
        inp = self._make_input(text, images)
        ap, sbx = _mode_policy(self.mode)
        params = {"threadId": self.thread_id, "input": inp,
                  "approvalPolicy": ap, "sandboxPolicy": _sandbox_policy(sbx)}
        if self.effort:
            params["effort"] = self.effort
        if self.model and self.model != "default":
            params["model"] = self.model
        async def _q():
            try:
                res = await self._request("turn/start", params)
                self.display_model = _display_model(self.model, (res or {}).get("model"))
                self.turn_id = ((res or {}).get("turn") or {}).get("id") or self.turn_id
                if self.queue:           # messages queued before the id landed
                    self._maybe_steer()
            except Exception as ex:
                if start:
                    self.busy = False
                self._push([{"kind": "notice", "text": "send failed: %r" % ex}])
                self._drain_queue()
        tornado.ioloop.IOLoop.current().spawn_callback(_q)

    def _maybe_steer(self):
        """Steering: inject queued messages into the running turn at a tool
        boundary (turn/steer), so codex sees them at its next step."""
        if not self.queue or not self.proc or self.ended or not self.turn_id:
            return
        items, self.queue = self.queue, []
        for it in items:
            self._echo_user(it["text"], it["images"], qid=it["qid"], start=False)
            inp = self._make_input(it["text"], it["images"])
            params = {"threadId": self.thread_id, "expectedTurnId": self.turn_id, "input": inp}
            async def _s(pr=params):
                try:
                    await self._request("turn/steer", pr)
                except Exception as ex:
                    self._push([{"kind": "notice", "text": "steer failed: %r" % ex}])
            tornado.ioloop.IOLoop.current().spawn_callback(_s)

    def _drain_queue(self):
        if self.busy or self.ended or not self.proc or not self.queue:
            return
        item = self.queue.pop(0)
        self._dispatch(item["text"], item["images"], qid=item["qid"], start=True)

    def unqueue(self, qid):
        for i, it in enumerate(self.queue):
            if it["qid"] == qid:
                self.queue.pop(i)
                self._push([{"kind": "unqueued", "qid": qid}])
                return

    def interrupt(self):
        if self.proc and not self.ended:
            if self.queue:
                self._push([{"kind": "unqueued", "qid": it["qid"]} for it in self.queue])
                self.queue = []
            tid, turn = self.thread_id, self.turn_id
            if not turn:
                return
            async def _i():
                try:
                    await self._request("turn/interrupt", {"threadId": tid, "turnId": turn})
                except Exception:
                    pass
            tornado.ioloop.IOLoop.current().spawn_callback(_i)

    def _emit_tokens(self, force=False):
        now = time.time()
        if not force and (now - self._last_tok_emit) < 0.12:
            return
        self._last_tok_emit = now
        self._emit({"type": "tokens", "up": self._tok_up, "out": self._tok_out,
                    "exact": self._tok_exact, "word": self.turn_word})

    def _notice(self, text):
        self._emit({"type": "events", "events": [{"kind": "notice", "text": text}]})

    # ───────── live model / approval changes (per-turn in codex) ─────────
    def set_model(self, model):
        self.model = model or "default"
        self.display_model = _display_model(self.model)
        previous_effort = self.effort
        effort_model = self.model if self.model != "default" else self.display_model
        self.effort = _sanitize_effort(self.effort, effort_model)
        if self.cc_id:
            save_pref(self.cc_id, model=self.model, effort=self.effort)
        suffix = (" · effort → %s (model default)" % self.effort
                  if self.effort != previous_effort else "")
        self._notice("⚙ model → %s%s (applies next turn)" % (self.model, suffix))

    def set_effort(self, effort):
        effort_model = self.model if self.model != "default" else self.display_model
        effort = _sanitize_effort(effort, effort_model)
        if effort == self.effort:
            return
        self.effort = effort
        if _valid_cc(self.cc_id):
            save_pref(self.cc_id, effort=effort)
        self._notice("⚙ effort → %s (applies next turn)" % effort)

    def set_mode(self, mode):
        self.mode = _sanitize_mode(mode)
        if self.cc_id:
            save_pref(self.cc_id, mode=self.mode)
        self._notice("⚙ approval → %s (applies next turn)" % self.mode)

    # ───────── viewers / log ─────────
    def attach(self, ws):
        self.viewers.add(ws)

    def detach(self, ws):
        self.viewers.discard(ws)

    def _emit(self, obj):
        data = json.dumps(obj)
        for v in list(self.viewers):
            try:
                v.write_message(data)
            except Exception:
                self.viewers.discard(v)

    def _push(self, evs):
        for ev in evs:
            self.event_seq += 1
            ev["_seq"] = self.event_seq
        self.log.extend(evs)
        if len(self.log) > 1500:
            self.log = self.log[-1500:]
        self._emit({"type": "events", "events": evs})

    def events_since(self, after_seq=None):
        """Return an incremental replay when the requested sequence is retained."""
        if after_seq is None:
            return self.log, False
        try:
            after = int(after_seq)
        except (TypeError, ValueError):
            return self.log, False
        if after < 0 or after > self.event_seq:
            return self.log, False
        if not self.log:
            return [], True
        first = int(self.log[0].get("_seq") or 1)
        if after < first - 1:
            return self.log, False
        return [ev for ev in self.log if int(ev.get("_seq") or 0) > after], True

    def terminate(self):
        self.ended = True
        self._req.clear()
        self._pending.clear()
        for fut in list(self._waiters.values()):
            if not fut.done():
                fut.cancel()
        self._waiters.clear()
        proc = self.proc
        self.proc = None
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass

    # ───────── recap (disabled in v1) ─────────
    def _recap_transcript(self):
        lines, has_asst = [], False
        assistant, order = {}, []
        for e in self.log:
            k = e.get("kind")
            if k == "user_text":
                t = (e.get("text") or "").strip()
                if t:
                    lines.append("User: " + t)
            elif k == "assistant_text":
                t = (e.get("text") or "").strip()
                if t:
                    lines.append("Assistant: " + t)
                    has_asst = True
            elif k == "assistant_delta":
                iid = e.get("itemId") or "assistant"
                if iid not in assistant:
                    order.append(iid)
                assistant[iid] = assistant.get(iid, "") + (e.get("delta") or "")
            elif k == "assistant_update":
                iid = e.get("itemId") or "assistant"
                if iid not in assistant:
                    order.append(iid)
                assistant[iid] = e.get("text") or assistant.get(iid, "")
            elif k == "tool_use":
                lines.append("[tool: %s]" % (e.get("tool") or "?"))
        for iid in order:
            t = assistant.get(iid, "").strip()
            if t:
                lines.append("Assistant: " + t)
                has_asst = True
        if not has_asst:
            return ""
        return "\n".join(lines)[-6000:]

    async def _make_recap(self):
        if self.recap_busy or self.busy or self.ended or self.compacting:
            return
        transcript = self._recap_transcript()
        marker = self.last_activity
        if not transcript:
            self.recap_for = marker
            return

        self.recap_busy = True
        out_path = os.path.join(tempfile.gettempdir(),
                                "codex-console-recap-%s.txt" % secrets.token_hex(6))
        prompt = (
            "你是 Codex Console 的闲置摘要器。只根据下面这段聊天历史生成一条中文小结，"
            "用于用户回来时快速接上上下文。\n"
            "要求：只输出小结本身；不要 Markdown；不要列表；20 到 80 个中文字符；"
            "强调当前完成了什么、还剩什么或下一步是什么。不要读取文件，不要运行命令。\n\n"
            "<transcript>\n%s\n</transcript>\n" % transcript
        )
        cmd = [
            CODEX_BIN, "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "--model", RECAP_MODEL,
            "-c", 'model_reasoning_effort="low"',
            "--cd", self.cwd,
            "-o", out_path,
            "-",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.cwd)
            try:
                await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")),
                                       timeout=RECAP_TIMEOUT_SEC)
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                return

            text = ""
            try:
                with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except Exception:
                text = ""
            text = re.sub(r"\s+", " ", text).strip()
            if text:
                self._push([{"kind": "recap", "text": _cap(text, 500)}])
        finally:
            self.recap_for = marker
            self.recap_busy = False
            try:
                os.unlink(out_path)
            except Exception:
                pass


class ChatSocket(AuthMixin, tornado.websocket.WebSocketHandler):
    """Thin attach/detach controller over persistent ChatSessions.
    Closing the socket only DETACHES — it never kills the claude process."""

    def check_origin(self, origin):
        return True

    clients = set()          # every open socket — for cross-device favorite sync

    def open(self):
        if AUTH and not self._ok_auth():
            self.close(4401, "Unauthorized")
            return
        self.session = None
        ChatSocket.clients.add(self)
        self._say({"type": "favorites", "favorites": list_favorites()})
        self._say({"type": "projects", "projects": project_tree()})

    def _say(self, obj):
        try:
            self.write_message(json.dumps(obj))
        except Exception:
            pass

    def _broadcast_favorites(self):
        favs = list_favorites()
        for ws in list(ChatSocket.clients):
            ws._say({"type": "favorites", "favorites": favs})

    def _broadcast_tree(self, rescan=False):
        """Push the project-grouped sidebar to every device. `rescan` forces a
        disk re-scan; metadata-only edits (star, rename, pin) reuse the cache."""
        tree = project_tree(force=rescan)
        for ws in list(ChatSocket.clients):
            ws._say({"type": "projects", "projects": tree})

    async def on_message(self, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        mt = msg.get("type")
        if mt == "start":
            if not CODEX_BIN or not os.path.exists(CODEX_BIN):
                self._say({"type": "error", "error": "codex CLI not found"})
                return
            cwd = safe_cwd(msg.get("cwd") or HOME)
            if not cwd:
                self._say({"type": "error", "error": "invalid working directory"})
                return
            sid = secrets.token_hex(6)
            start_model = msg.get("model") or ""
            sess = ChatSession(sid, cwd, start_model, _sanitize_mode(msg.get("mode")),
                               effort=_sanitize_effort(msg.get("effort"), start_model))
            try:
                await sess.start()
            except Exception as e:
                self._say({"type": "error", "error": "spawn failed: %s" % e})
                return
            CHAT_SESSIONS[sid] = sess
            if self.session and self.session is not sess:
                self.session.detach(self)   # keep it alive, just stop viewing it
            self.session = sess
            sess.attach(self)
            self._say({"type": "started", "id": sid, "cwd": cwd,
                       "name": os.path.basename(cwd) or cwd,
                       "model": sess.model or "default", "display_model": sess.display_model,
                       "mode": sess.mode, "effort": sess.effort,
                       "activity": sess.last_activity,
                       "event_seq": sess.event_seq,
                       "subagents": sess.subagents_snapshot()})
        elif mt == "detach":
            old = self.session
            if old:
                old.detach(self)
                self.session = None
            self._say({"type": "detached", "id": old.id if old else None})
        elif mt == "attach":
            sess = CHAT_SESSIONS.get(msg.get("id"))
            if not sess:
                self._say({"type": "no_session", "id": msg.get("id")})
                return
            if self.session and self.session is not sess:
                self.session.detach(self)
            self.session = sess
            sess.attach(self)
            events, events_delta = sess.events_since(msg.get("after_seq"))
            self._say({"type": "attached", "id": sess.id, "cwd": sess.cwd,
                       "name": os.path.basename(sess.cwd) or sess.cwd, "cc": sess.cc_id,
                       "title": sess.title(), "ctx": sess.ctx, "usage": sess.usage,
                       "model": sess.model or "default", "display_model": sess.display_model,
                       "mode": sess.mode,
                       "busy": sess.busy, "ended": sess.ended, "events": events,
                       "events_delta": events_delta, "event_seq": sess.event_seq,
                       "turn_age": sess.turn_age(), "word": sess.turn_word, "effort": sess.effort,
                       "compacting": sess.compacting,
                       "activity": sess.last_activity,
                       "subagents": sess.subagents_snapshot()})
            # returning to an idle session that's been quiet a while → recap it now
            # (the periodic sweep may not have ticked yet); guarded against busy/dup
            if (RECAP_ENABLED and not sess.busy and not sess.ended and not sess.compacting
                    and sess.recap_for != sess.last_activity
                    and (time.time() - sess.last_activity) >= RECAP_IDLE_SEC):
                tornado.ioloop.IOLoop.current().spawn_callback(sess._make_recap)
        elif mt == "resume":
            if not CODEX_BIN or not os.path.exists(CODEX_BIN):
                self._say({"type": "error", "error": "codex CLI not found"})
                return
            cc = msg.get("cc")
            cwd = safe_cwd(msg.get("cwd") or HOME)
            if not cwd or not _valid_cc(cc):
                self._say({"type": "error", "error": "invalid resume target"})
                return
            live = next((s for s in CHAT_SESSIONS.values()
                         if s.cc_id == cc and not s.ended), None)
            if live:
                sess = live
            else:
                pf = load_prefs().get(cc) or {}   # restore this session's own
                r_mode = _sanitize_mode(pf.get("mode") or msg.get("mode"))
                r_model = pf.get("model") or msg.get("model") or ""
                r_effort = _sanitize_effort(
                    pf.get("effort") or msg.get("effort"), r_model)
                sess = ChatSession(secrets.token_hex(6), cwd, r_model,
                                   r_mode, resume_cc=cc, effort=r_effort)
                sess.preload()
                try:
                    await sess.start()
                except Exception as e:
                    self._say({"type": "error", "error": "resume spawn failed: %s" % e})
                    return
                CHAT_SESSIONS[sess.id] = sess
            if self.session and self.session is not sess:
                self.session.detach(self)
            self.session = sess
            sess.attach(self)
            self._say({"type": "attached", "id": sess.id, "cwd": sess.cwd,
                       "name": os.path.basename(sess.cwd) or sess.cwd, "cc": sess.cc_id,
                       "title": sess.title(), "ctx": sess.ctx, "usage": sess.usage,
                       "model": sess.model or "default", "display_model": sess.display_model,
                       "mode": sess.mode,
                       "busy": sess.busy, "ended": sess.ended, "events": sess.log,
                       "events_delta": False, "event_seq": sess.event_seq,
                       "turn_age": sess.turn_age(), "word": sess.turn_word, "effort": sess.effort,
                       "compacting": sess.compacting, "resumed": True,
                       "activity": sess.last_activity,
                       "subagents": sess.subagents_snapshot()})
        elif mt == "subagent_read" and self.session:
            res = await self.session.read_subagent_thread(
                msg.get("threadId"), msg.get("limit") or 160)
            self._say({"type": "subagent_thread", **res})
        elif mt == "approve" and self.session:
            self.session.resolve_approval(msg.get("aid"), bool(msg.get("allow")), bool(msg.get("always")))
        elif mt == "answer" and self.session:
            self.session.resolve_answer(msg.get("aid"), msg.get("answers"))
        elif mt == "interrupt" and self.session:
            self.session.interrupt()
        elif mt == "set_model" and self.session:
            self.session.set_model(msg.get("model") or "")
        elif mt == "set_mode" and self.session:
            self.session.set_mode(msg.get("mode") or "")
        elif mt == "configure":
            # ⚙ per-session model/permission/effort from the kebab Configure popover,
            # targeting a live session by id (the attached one or any other).
            sess = CHAT_SESSIONS.get(msg.get("id"))
            if sess and not sess.ended:
                if msg.get("model") is not None:
                    sess.set_model(msg.get("model") or "")
                if msg.get("mode") is not None:
                    sess.set_mode(msg.get("mode") or "")
                if msg.get("effort") is not None:
                    sess.set_effort(msg.get("effort"))
        elif mt == "set_effort" and self.session:
            # Codex reasoning effort is a per-turn parameter, so changing it just
            # updates the session — it takes effect on the next turn (no relaunch).
            sess = self.session
            if not sess.ended:
                sess.set_effort(msg.get("effort"))
        elif mt == "user" and self.session:
            self.session.send_user(msg.get("text", ""), msg.get("images"))
        elif mt == "unqueue" and self.session:
            self.session.unqueue(msg.get("qid"))
        elif mt == "del_resumable":
            # Sidebar 🗑: move a resumable session's transcript to the trash.
            cc = msg.get("cc")
            for s in list(CHAT_SESSIONS.values()):
                if s.cc_id == cc:          # end a live session resumed from it first
                    s.terminate()
                    CHAT_SESSIONS.pop(s.id, None)
            res = trash_transcript(cc)
            save_pref(cc, fav=False)       # a trashed session can't stay starred
            self._say({"type": "resumable_deleted", "cc": cc,
                       "ok": bool(res.get("ok")), "error": res.get("error")})
            self._broadcast_favorites()
            self._broadcast_tree(rescan=True)
        elif mt == "set_favorite":
            # Star/unstar a session; persisted server-side and broadcast so every
            # device shares one favorites list. Starring carries a metadata snapshot.
            cc = msg.get("cc")
            if _valid_cc(cc):
                if msg.get("fav"):
                    save_pref(cc, fav={"cwd": msg.get("cwd") or "",
                                       "name": (msg.get("name") or "")[:120],
                                       "title": (msg.get("title") or "")[:200],
                                       "ts": time.time()})
                else:
                    save_pref(cc, fav=False)
                self._broadcast_favorites()
        elif mt == "proj_fav":
            # Star/unstar a FOLDER. Favorites are project-level now: a starred
            # project keeps every session it owns, instead of pinning one thread.
            if save_project_meta(msg.get("path"), fav=bool(msg.get("fav"))):
                self._broadcast_tree()
        elif mt == "proj_rename":
            # Sidebar ✎ on a project: label the folder. Empty name resets to basename.
            if save_project_meta(msg.get("path"), name=msg.get("name") or ""):
                self._broadcast_tree()
        elif mt == "proj_unpin":
            # Drop an imported folder. Only removes the sidebar entry; a folder
            # that still owns sessions keeps showing up through the disk scan.
            if save_project_meta(msg.get("path"), pinned=False, fav=False):
                self._broadcast_tree()
        elif mt == "proj_refresh":
            self._broadcast_tree(rescan=True)
        elif mt == "rename":
            # Sidebar ✎: set/clear a custom label for a session (by codex thread id).
            cc = msg.get("cc")
            ok = set_name(cc, msg.get("name") or "")
            self._say({"type": "renamed", "cc": cc,
                       "name": (msg.get("name") or "").strip()[:120], "ok": bool(ok)})
            if ok:
                self._broadcast_favorites()   # a renamed session may be starred → refresh every device's list
                self._broadcast_tree(rescan=True)   # the session title shows in its project too
        elif mt == "end":
            # End a specific session by id (sidebar ✕) or the active one.
            # Ending a background session never disturbs the active stream.
            target = msg.get("id")
            cur = self.session
            if target and (not cur or target != cur.id):
                s = CHAT_SESSIONS.get(target)
                if s:
                    s.terminate()
                    CHAT_SESSIONS.pop(s.id, None)
                self._say({"type": "ended", "id": target})
            elif cur:
                cur.terminate()
                cur.detach(self)
                CHAT_SESSIONS.pop(cur.id, None)
                self.session = None
                self._say({"type": "ended", "id": cur.id})
        elif mt == "list":
            self._say({"type": "sessions", "sessions": [
                {"id": s.id, "cwd": s.cwd, "root": _norm_dir(s.cwd) or s.cwd,
                 "name": os.path.basename(s.cwd) or s.cwd,
                 "cc": s.cc_id, "model": s.model or "default", "display_model": s.display_model,
                 "mode": s.mode,
                 "effort": s.effort, "title": s.title(),
                 "busy": s.busy, "ended": s.ended,
                 "activity": s.last_activity, "event_seq": s.event_seq}
                for s in CHAT_SESSIONS.values()]})

    def on_close(self):
        if self.session:
            self.session.detach(self)   # keep the claude process alive
        ChatSocket.clients.discard(self)


class ConsoleHandler(AuthMixin, tornado.web.RequestHandler):
    def get(self):
        if not self._ok_auth():
            return
        self.set_header("Cache-Control", "no-store")
        self.write(CONSOLE_HTML.replace("__CODEX_CONSOLE_WEBFM_URL__", json.dumps(WEBFM_URL)))


CONSOLE_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,interactive-widget=resizes-content">
<title>Codex Console</title>
<script>try{var _t=localStorage.getItem('al_theme');if(_t&&_t!=='dark')document.documentElement.setAttribute('data-theme',_t);}catch(e){}</script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
/* ── theme palettes: 13 base vars each; panels/tints derived below ── */
:root{ /* Dark (default) */
  --bg:#1e1e1e;--bg2:#252526;--bg3:#2d2d2d;--line:#3c3c3c;--fg:#d4d4d4;--mut:#858585;
  --acc:#4fc1ff;--usr:#3794ff;--add:#2ea043;--del:#f85149;--tool:#e0c080;--think:#858585;--onacc:#04121f}
:root[data-theme="light"]{
  --bg:#ffffff;--bg2:#f6f8fa;--bg3:#eaeef2;--line:#d0d7de;--fg:#1f2328;--mut:#656d76;
  --acc:#0969da;--usr:#0969da;--add:#1a7f37;--del:#cf222e;--tool:#9a6700;--think:#767676;--onacc:#ffffff}
:root[data-theme="dracula"]{
  --bg:#282a36;--bg2:#21222c;--bg3:#343746;--line:#44475a;--fg:#f8f8f2;--mut:#8390b7;
  --acc:#bd93f9;--usr:#8be9fd;--add:#50fa7b;--del:#ff5555;--tool:#f1fa8c;--think:#8390b7;--onacc:#282a36}
:root[data-theme="nord"]{
  --bg:#2e3440;--bg2:#2b303b;--bg3:#3b4252;--line:#434c5e;--fg:#d8dee9;--mut:#919cb0;
  --acc:#88c0d0;--usr:#81a1c1;--add:#a3be8c;--del:#bf616a;--tool:#ebcb8b;--think:#929cae;--onacc:#2e3440}
:root[data-theme="solarized-light"]{
  --bg:#fdf6e3;--bg2:#eee8d5;--bg3:#e4ddc8;--line:#d6cfb8;--fg:#586e75;--mut:#657474;
  --acc:#268bd2;--usr:#268bd2;--add:#859900;--del:#dc322f;--tool:#b58900;--think:#657474;--onacc:#fdf6e3}
:root[data-theme="tokyo-night"]{
  --bg:#1a1b26;--bg2:#1f2335;--bg3:#292e42;--line:#3b4261;--fg:#c0caf5;--mut:#7981ab;
  --acc:#7aa2f7;--usr:#7dcfff;--add:#9ece6a;--del:#f7768e;--tool:#e0af68;--think:#7981ab;--onacc:#1a1b26}
:root[data-theme="catppuccin"]{
  --bg:#1e1e2e;--bg2:#181825;--bg3:#313244;--line:#45475a;--fg:#cdd6f4;--mut:#81869d;
  --acc:#89b4fa;--usr:#89dceb;--add:#a6e3a1;--del:#f38ba8;--tool:#f9e2af;--think:#82859a;--onacc:#1e1e2e}
:root[data-theme="gruvbox"]{
  --bg:#282828;--bg2:#1d2021;--bg3:#3c3836;--line:#504945;--fg:#ebdbb2;--mut:#a89984;
  --acc:#83a598;--usr:#8ec07c;--add:#b8bb26;--del:#fb4934;--tool:#fabd2f;--think:#9a8c7e;--onacc:#282828}
:root[data-theme="catppuccin-latte"]{
  --bg:#eff1f5;--bg2:#e6e9ef;--bg3:#dce0e8;--line:#ccd0da;--fg:#4c4f69;--mut:#6a6d82;
  --acc:#1e66f5;--usr:#04a5e5;--add:#40a02b;--del:#d20f39;--tool:#df8e1d;--think:#6a6d82;--onacc:#ffffff}
:root[data-theme="gruvbox-light"]{
  --bg:#fbf1c7;--bg2:#f2e5bc;--bg3:#ebdbb2;--line:#d5c4a1;--fg:#3c3836;--mut:#786b61;
  --acc:#458588;--usr:#689d6a;--add:#98971a;--del:#cc241d;--tool:#b57614;--think:#786b5e;--onacc:#fbf1c7}
:root[data-theme="rose-pine-dawn"]{
  --bg:#faf4ed;--bg2:#fffaf3;--bg3:#f2e9e1;--line:#dfdad9;--fg:#575279;--mut:#736d83;
  --acc:#907aa9;--usr:#286983;--add:#5b8a3a;--del:#b4637a;--tool:#ea9d34;--think:#736d83;--onacc:#faf4ed}
:root[data-theme="one-light"]{
  --bg:#fafafa;--bg2:#f0f0f0;--bg3:#e5e5e6;--line:#d4d4d6;--fg:#383a42;--mut:#72737b;
  --acc:#4078f2;--usr:#0184bc;--add:#50a14f;--del:#e45649;--tool:#c18401;--think:#72737b;--onacc:#ffffff}
:root[data-theme="ayu-light"]{
  --bg:#fcfcfc;--bg2:#f3f4f5;--bg3:#e7e8e9;--line:#dcdde0;--fg:#5c6166;--mut:#70757d;
  --acc:#399ee6;--usr:#55b4d4;--add:#86b300;--del:#e65050;--tool:#f2ae49;--think:#70757d;--onacc:#ffffff}
/* derived panel & accent-text tints — one set of formulas reused by every theme */
:root{
  --codebg:color-mix(in srgb, var(--bg) 88%, #000);
  --sel:color-mix(in srgb, var(--acc) 16%, var(--bg));
  --selln:color-mix(in srgb, var(--acc) 42%, var(--bg));
  --toolbg:color-mix(in srgb, var(--tool) 13%, var(--bg));
  --toolln:color-mix(in srgb, var(--tool) 34%, var(--bg));
  --okbg:color-mix(in srgb, var(--add) 20%, var(--bg));
  --nobg:color-mix(in srgb, var(--del) 20%, var(--bg));
  --infobg:color-mix(in srgb, var(--usr) 14%, var(--bg));
  --infoln:color-mix(in srgb, var(--usr) 40%, var(--bg));
  --addfg:color-mix(in srgb, var(--add) 62%, var(--fg));
  --delfg:color-mix(in srgb, var(--del) 58%, var(--fg));
  --dim:var(--mut);   /* was used in 9 places but never defined -> inherited --fg */
  --fsans:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,"Noto Sans CJK SC",sans-serif;
  --fmono:ui-monospace,SFMono-Regular,Menlo,"Noto Sans Mono CJK SC",monospace}
/* THEME-END */
html,body{height:100%;background:var(--bg);color:var(--fg);overflow:hidden;
  font-family:var(--fsans);font-size:14px}
body{display:flex;flex-direction:column}
header{display:flex;gap:6px;align-items:center;padding:6px 10px;background:var(--bg2);
  border-bottom:1px solid var(--line);flex-shrink:0;flex-wrap:wrap}
.sb-brand{font-weight:700;color:var(--acc);font-size:15px;padding:11px 12px 9px;border-bottom:1px solid var(--line);white-space:nowrap}
header select,header input{background:var(--bg3);color:var(--fg);border:1px solid var(--line);
  border-radius:5px;padding:4px 7px;font-size:12.5px}
header select#project{flex:1;min-width:120px;max-width:380px}
header input#cwd{flex:1;min-width:120px;display:none}
.status{font-size:11.5px;color:var(--mut);white-space:nowrap;margin-left:auto;display:flex;gap:6px;align-items:center}
.dot{width:8px;height:8px;border-radius:50%;background:var(--mut)}
.dot.on{background:var(--add);box-shadow:0 0 6px var(--add)}
.dot.busy{background:var(--tool);box-shadow:0 0 6px var(--tool);animation:pulse 1s infinite}
@keyframes pulse{50%{opacity:.35}}
/* floating status pill above the composer — a "ready/idle" light, or the
   animated working indicator (glyph + word + elapsed) while a turn runs */
.pillrow{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:6px;margin:0 0 7px}
#effort{flex:none;max-width:100%;padding:3px 11px;background:var(--bg3);border:1px solid var(--line);
  border-radius:9px;box-shadow:0 2px 10px rgba(0,0,0,.28);user-select:none;cursor:pointer;
  font-size:13px;line-height:1.1;color:var(--fg);white-space:nowrap}
#effort:hover{border-color:var(--acc);color:var(--acc)}
#thinking{flex:none;max-width:100%;padding:3px 12px;
  background:var(--bg3);border:1px solid var(--line);border-radius:9px;
  box-shadow:0 2px 10px rgba(0,0,0,.28);user-select:none}
#thinking .twrap{display:flex;align-items:center;gap:8px;font-size:13px;line-height:1.1}
#thinking .dot{flex:none}
#thinking.busy .dot{display:none}
#thinking .glyph{display:none;font-size:13px;color:var(--tool);width:1.1em;text-align:center;
  text-shadow:0 0 8px var(--tool);animation:thinkpulse 1.4s ease-in-out infinite}
#thinking.busy .glyph{display:inline-block}
#thinking.compacting .glyph{width:2.6em;text-align:left;letter-spacing:1px;font-weight:700}
#thinking .meta .mtx{font-family:var(--fmono);letter-spacing:1px}
#thinking .meta .mtx span{text-shadow:0 0 5px currentColor}
#thinking .meta .mtx.lite span{text-shadow:none;font-weight:600}   /* light bg: glow→smear, so drop it & bolden for contrast */
#thinking .word{color:var(--fg);font-weight:600}
#thinking.busy .word::after{content:'…'}
#thinking .meta{color:var(--mut);font-size:12px;font-variant-numeric:tabular-nums}
@keyframes thinkpulse{0%,100%{opacity:.45}50%{opacity:1}}
.btn{background:var(--bg3);color:var(--fg);border:1px solid var(--line);border-radius:5px;
  padding:4px 9px;font-size:12.5px;cursor:pointer;white-space:nowrap}
.btn:hover{background:var(--line)}

#chat{flex:1;overflow-y:auto;overflow-x:hidden;padding:14px;-webkit-overflow-scrolling:touch}
.wrap{max-width:820px;margin:0 auto}
.msg{margin-bottom:14px;line-height:1.5;word-wrap:break-word;overflow-wrap:anywhere}
.msg.user{display:flex;justify-content:flex-end}
.msg.user .b{background:var(--sel);border:1px solid var(--selln);border-radius:10px;padding:8px 12px;max-width:85%;white-space:pre-wrap}
.msg.asst .b{color:var(--fg);text-wrap:pretty}
.msg.asst.streaming .b{text-wrap:wrap}
.think{color:var(--think);font-style:italic;font-size:13px;border-left:2px solid var(--line);padding:3px 0 3px 10px;margin-bottom:12px;white-space:pre-wrap}
.think.hide{display:none}
.notice{color:var(--mut);font-size:11.5px;margin:6px 0}
.errline{color:var(--del);font-size:12px;font-family:var(--fmono);margin:4px 0;white-space:pre-wrap}
.plandock{position:sticky;top:0;z-index:7;max-width:820px;margin:0 auto 10px;padding:0 0 8px;
  background:linear-gradient(to bottom,var(--bg) 0%,var(--bg) calc(100% - 8px),transparent 100%)}
.plandock[hidden]{display:none}
.plandock .plan{margin:0;box-shadow:0 7px 18px rgba(0,0,0,.18)}
.plan{border:1px solid var(--infoln);border-radius:8px;margin:7px 0 12px;background:var(--infobg);overflow:hidden}
.plan .ph{display:flex;align-items:center;gap:7px;padding:7px 10px;color:var(--usr);font-weight:650}
.plan .pex{padding:0 10px 7px;color:var(--mut);font-size:12.5px;white-space:pre-wrap}
.plan .psteps{display:flex;flex-direction:column;border-top:1px solid var(--infoln)}
.plan .pst{display:grid;grid-template-columns:22px 1fr;gap:6px;align-items:start;padding:6px 10px;font-size:13px;line-height:1.35}
.plan .pst + .pst{border-top:1px solid color-mix(in srgb,var(--infoln) 55%,transparent)}
.plan .pi{font-family:var(--fmono);color:var(--mut)}
.plan .pst.done .pi{color:var(--addfg)}.plan .pst.active .pi{color:var(--tool)}.plan .pst.todo .pi{color:var(--mut)}
.plan .ptext{padding:8px 10px;border-top:1px solid var(--infoln);white-space:pre-wrap;font-family:var(--fmono);font-size:12px;color:var(--fg)}
.streaming{opacity:.95}
.localstatus{border:1px solid var(--line);border-radius:8px;margin:8px 0 12px;background:var(--bg2);padding:9px 10px;font-size:12px;line-height:1.45}
.localstatus .sh{display:flex;align-items:center;gap:8px;color:var(--fg);font-weight:650;margin-bottom:7px}
.localstatus .sg{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:5px 14px}
.localstatus .sk{color:var(--mut);margin-right:6px}
.localstatus .sv{font-family:var(--fmono);overflow-wrap:anywhere}
.localstatus .sf{margin-top:7px;color:var(--mut);font-family:var(--fmono);overflow-wrap:anywhere}
.recap{color:var(--mut);font-size:12px;margin:7px 0;line-height:1.45}
.recap .rk{font-weight:600;letter-spacing:.2px}
.recap .rt{font-style:italic;opacity:.92}
.doneline{color:var(--mut);font-size:11.5px;margin:6px 0}
.doneline .dg{color:var(--tool)}

/* collapsed change/tool cards */
.tool{border:1px solid var(--line);border-radius:8px;margin:6px 0 12px;background:var(--bg2);overflow:hidden}
.tool .th{padding:7px 10px;cursor:pointer;display:flex;gap:8px;align-items:center;user-select:none}
.tool .th:hover{background:var(--bg3)}
.tool .ico{flex-shrink:0}
.tool .tn{color:var(--tool);font-weight:600;font-family:var(--fmono);font-size:12.5px;flex-shrink:0}
.tool .tp{color:var(--mut);font-family:var(--fmono);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
.tool .cnt{font-size:11.5px;font-family:var(--fmono);flex-shrink:0}
.tool .cnt .a{color:var(--addfg)}.tool .cnt .d{color:var(--delfg)}
.tool .eye{color:var(--mut);flex-shrink:0;display:inline-flex;align-items:center;transition:color .15s}
.tool .eye svg{width:16px;height:16px;display:block}
.tool .eye .e-open{display:none}            /* collapsed → closed eye */
.tool.open .eye .e-shut{display:none}
.tool.open .eye .e-open{display:block}      /* expanded → open eye */
.tool .th:hover .eye{color:var(--fg)}
.tool .tb{display:none;border-top:1px solid var(--line);padding:8px 10px}
.tool.open .tb{display:block}
.tool.err .tn{color:var(--del)}
.tool.done .tn{opacity:.85}
.tool .progress,.ecard .progress{font-size:11.5px;color:var(--mut);font-family:var(--fmono);white-space:pre-wrap;margin:0 0 5px}
.tool .resmeta,.ecard .resmeta{font-size:11px;color:var(--mut);margin-left:6px}
pre{background:var(--codebg);border:1px solid var(--line);border-radius:6px;padding:8px;overflow-x:auto;margin:5px 0;
  font-family:var(--fmono);font-size:12.5px;line-height:1.45}
code{font-family:var(--fmono);font-size:12.5px;background:var(--codebg);border:1px solid var(--line);border-radius:3px;padding:0 4px}
pre code{background:none;border:none;padding:0}
.bubble h1{font-size:16px;font-weight:700;line-height:1.3;margin:14px 0 5px;text-wrap:balance}
.bubble h2{font-size:15px;font-weight:700;line-height:1.35;margin:12px 0 4px;text-wrap:balance}
.bubble h3{font-size:14px;font-weight:600;line-height:1.4;margin:10px 0 3px;text-wrap:balance}
.bubble h1:first-child,.bubble h2:first-child,.bubble h3:first-child{margin-top:0}
.bubble table{border-collapse:collapse;margin:7px 0;font-size:12px;display:block;overflow-x:auto;max-width:100%}
.bubble th,.bubble td{border:1px solid var(--line);padding:3px 9px;text-align:left}
.bubble thead th{background:var(--bg3);font-weight:600}
.msg.asst ul,.msg.asst ol{margin:4px 0 4px 20px}
.msg.asst a{color:var(--acc)}
.diffline{font-family:var(--fmono);font-size:12px;white-space:pre-wrap;line-height:1.4}
.dl-add{color:var(--addfg)}.dl-del{color:var(--delfg)}.dl-hdr{color:var(--acc)}.dl-ctx{color:var(--mut)}
.reslabel{font-size:11px;color:var(--mut);margin:6px 0 2px}

#composer{flex-shrink:0;border-top:1px solid var(--line);background:var(--bg2);padding:8px 10px;
  padding-bottom:calc(8px + env(safe-area-inset-bottom));display:flex;flex-direction:column;gap:0;align-items:stretch}
#composer .wrap2{width:100%;display:flex;gap:8px;align-items:flex-end}
#attach{display:none;flex-wrap:wrap;gap:7px;padding:0 0 8px}
#attach.on{display:flex}
#attach .att{position:relative;width:54px;height:54px;border-radius:8px;overflow:hidden;border:1px solid var(--line);background:var(--bg3)}
#attach .att img{width:100%;height:100%;object-fit:cover;display:block}
#attach .att .rm{position:absolute;top:1px;right:1px;width:17px;height:17px;border-radius:50%;border:none;
  background:rgba(0,0,0,.6);color:#fff;cursor:pointer;font-size:12px;line-height:17px;text-align:center;padding:0}
#attach .att .rm:hover{background:var(--del)}
.msg.user .imgs{margin-top:5px;font-size:11.5px;color:var(--mut)}
/* queued messages (typed while the agent is busy) */
#queue{width:100%;display:none;flex-direction:column;gap:5px;padding:0 0 8px}
#queue.on{display:flex}
#queue .qmsg{display:flex;align-items:center;gap:8px;background:var(--bg3);border:1px solid var(--line);border-radius:8px;padding:5px 9px;font-size:12.5px;color:var(--fg);cursor:pointer}
#queue .qmsg:hover{border-color:var(--acc)}
#queue .qmsg .qicon{flex:none;color:var(--tool);font-size:12px}
#queue .qmsg .qtext{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#queue .qmsg .qx{flex:none;color:var(--mut);font-size:13px;padding:0 2px}
#queue .qmsg:hover .qx{color:var(--del)}
#ta{flex:1;background:var(--bg3);color:var(--fg);border:1px solid var(--line);border-radius:10px;
  padding:9px 12px;font-size:14px;font-family:inherit;resize:none;max-height:160px;line-height:1.4}
#ta:focus{outline:1px solid var(--acc)}
#send{background:var(--acc);color:var(--onacc);border:none;border-radius:10px;width:38px;height:38px;flex:none;display:inline-flex;align-items:center;justify-content:center;font-size:15px;font-weight:700;cursor:pointer}
#send:disabled{background:var(--line);color:var(--mut);cursor:default}

#drawer{position:fixed;top:0;right:0;width:var(--drw,min(560px,92vw));height:100%;background:var(--bg);border-left:1px solid var(--line);
  transform:translateX(100%);transition:transform .34s cubic-bezier(.32,.72,0,1);z-index:20;display:flex;flex-direction:column}
#drawer.open{transform:none}
#drresize{position:absolute;left:0;top:0;width:6px;height:100%;cursor:col-resize;background:transparent;transition:background .12s;z-index:30}
#drresize:hover,#drresize.drag{background:var(--acc)}
#drawer .dh{padding:8px 12px;background:var(--bg2);border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
#drawer .dh .grow{flex:1}
#drawer .dc{flex:1;overflow:auto;padding:10px}
.gfile{font-family:var(--fmono);font-size:12px;padding:1px 0}.gfile .st{display:inline-block;width:24px;color:var(--tool);font-weight:700}
.empty{color:var(--mut);padding:18px;text-align:center}
#agentPanel{position:fixed;top:0;right:0;width:var(--agw,min(760px,96vw));height:100%;background:var(--bg);border-left:1px solid var(--line);
  transform:translateX(100%);transition:transform .34s cubic-bezier(.32,.72,0,1);z-index:22;display:flex;flex-direction:column}
#agentPanel.open{transform:none}
#agentPanel .agh{padding:8px 12px;background:var(--bg2);border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
#agentPanel .agh .agtit{font-size:13px;font-weight:700;color:var(--fg)}
#agentPanel .agh .agmeta{font-size:11.5px;color:var(--mut);font-family:var(--fmono)}
#agentPanel .agh .grow{flex:1}
#agentPanel .agbody{flex:1;min-height:0;display:flex}
#agentList{width:260px;flex:0 0 auto;border-right:1px solid var(--line);overflow:auto;background:var(--bg2)}
#agentDetail{flex:1;min-width:0;overflow:auto;background:var(--bg)}
.agsec{font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut);padding:9px 10px 4px}
.agrow{display:flex;gap:8px;align-items:flex-start;padding:8px 10px;border-left:2px solid transparent;cursor:pointer}
.agrow:hover{background:var(--bg3)}
.agrow.on{background:var(--sel);border-left-color:var(--acc)}
.agrow.done{opacity:.72}
.agdot{width:7px;height:7px;border-radius:50%;background:var(--mut);flex:0 0 auto;margin-top:5px}
.agdot.run{background:var(--tool);box-shadow:0 0 5px var(--tool);animation:pulse 1s infinite}
.agdot.ok{background:var(--add)}
.agdot.bad{background:var(--del)}
.agrow .agm{min-width:0;flex:1}
.agrow .agn{font-size:12.5px;color:var(--fg);font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.agrow .ags{font-size:11px;color:var(--mut);line-height:1.35;word-break:break-word;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.aghead{padding:12px 14px;border-bottom:1px solid var(--line);background:var(--bg2)}
.aghead .agt{font-size:13.5px;font-weight:700;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.aghead .agsub{font-size:11.5px;color:var(--mut);font-family:var(--fmono);line-height:1.45;word-break:break-word}
.agmsgs{padding:6px 0 14px}
.agmsg{padding:8px 14px;border-left:3px solid transparent}
.agmsg .agr{font-size:10.5px;color:var(--dim);margin-bottom:2px;letter-spacing:.03em}
.agmsg .agtxt{font-size:12.5px;line-height:1.55;color:var(--fg);white-space:pre-wrap;word-break:break-word}
.agmsg.user{border-left-color:var(--acc)}
.agmsg.assistant{border-left-color:var(--line)}
.agmsg.thinking{border-left-color:var(--tool)}
.agmsg.plan{border-left-color:var(--usr)}
.agmsg.system,.agmsg.agent{border-left-color:var(--mut)}
.agmsg.tool .agtxt{font-family:var(--fmono);font-size:11.5px;color:var(--dim)}
.agmsg.tool pre{margin:0}
.agmark{font-size:12px;color:var(--acc);background:var(--infobg);border:1px solid var(--infoln);border-radius:6px;
  padding:3px 9px;margin:2px 0 12px;display:inline-flex;gap:7px;cursor:pointer;font-family:var(--fmono);align-items:center}
.agmark:hover{filter:brightness(1.18)}
.agmark .mut{color:var(--mut)}
/* edits-out-of-chat */
.dh .tab{cursor:pointer;padding:3px 9px;border-radius:5px;color:var(--mut);font-size:12.5px;user-select:none}
.dh .tab.on{background:var(--bg3);color:var(--fg)}
.dh .tab span{font-size:10px;opacity:.8}
.emark{font-size:12px;color:var(--tool);background:var(--toolbg);border:1px solid var(--toolln);border-radius:6px;
  padding:3px 9px;margin:2px 0 12px;display:inline-flex;gap:7px;cursor:pointer;font-family:var(--fmono);align-items:center}
.emark:hover{filter:brightness(1.25)}
.emark .a{color:var(--addfg)}.emark .d{color:var(--delfg)}.emark .mut{color:var(--mut)}
.ecard{border:1px solid var(--line);border-radius:8px;margin-bottom:10px;background:var(--bg2);overflow:hidden}
.ecard.turndiff .eh{background:var(--infobg)}
.ecard .eh{padding:7px 9px;display:flex;gap:7px;align-items:center;background:var(--toolbg);border-bottom:1px solid var(--line)}
.ecard .ef{color:var(--tool);font-family:var(--fmono);font-size:12px;font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ecard .cnt{font-size:11px;font-family:var(--fmono)}.ecard .cnt .a{color:var(--addfg)}.ecard .cnt .d{color:var(--delfg)}
.ecard.flash{outline:2px solid var(--acc);outline-offset:-2px}
.efocus{font-size:11.5px;color:var(--mut);margin:0 0 9px;padding:5px 8px;background:var(--bg2);border:1px solid var(--line);border-radius:6px}
.efocus .showall{color:var(--acc);cursor:pointer;text-decoration:underline}
.ecard .ed{max-height:320px;overflow:auto;padding:6px 9px}
/* single-file focus: let the one diff fill the drawer height instead of capping at 320px */
#edits.focusone{display:flex;flex-direction:column;overflow:hidden}
#edits.focusone .ecard{flex:1;min-height:0;display:flex;flex-direction:column;margin-bottom:0}
#edits.focusone .ecard .ed{flex:1;min-height:0;max-height:none}
.ecard .res{padding:0 9px}
/* approval prompts */
.approval{border:1px solid var(--toolln);border-radius:8px;margin:6px 0 14px;background:var(--toolbg);overflow:hidden}
.approval .ah{padding:8px 10px;color:var(--tool);font-weight:600;display:flex;gap:7px;align-items:center}
.approval .ah .tp{color:var(--mut);font-family:var(--fmono);font-weight:400;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.approval .abody{max-height:240px;overflow:auto;padding:4px 10px;border-top:1px solid var(--toolln)}
.approval .abtns{display:flex;gap:8px;padding:8px 10px;border-top:1px solid var(--toolln)}
.approval .abtns button{flex:1;padding:9px;border-radius:6px;cursor:pointer;font-size:14px;font-weight:700}
.approval .appr{background:var(--okbg);color:var(--addfg);border:1px solid var(--add)}
.approval .apprall{background:var(--infobg);color:var(--acc);border:1px solid var(--infoln)}
.approval .deny{background:var(--nobg);color:var(--delfg);border:1px solid var(--del)}
.approval .abtns button{white-space:nowrap}
.approval.done .abtns{opacity:.85}
.approval .ok{color:var(--addfg);font-weight:700}.approval .no{color:var(--delfg);font-weight:700}
/* question prompts (AskUserQuestion) */
.question{border:1px solid var(--infoln);border-radius:8px;margin:6px 0 14px;background:var(--infobg);overflow:hidden}
.question .qh{padding:8px 10px;color:var(--usr);font-weight:600;display:flex;gap:7px;align-items:center}
.question .qblk{padding:8px 10px;border-top:1px solid var(--infoln)}
.question .qtext{font-weight:600;margin-bottom:7px}
.question .qtext .chip{font-weight:600;color:var(--mut);font-size:11px;border:1px solid var(--line);border-radius:4px;padding:1px 5px;margin-right:6px}
.question .qopts{display:flex;flex-direction:column;gap:6px}
.question .qopt{text-align:left;padding:8px 10px;border-radius:6px;cursor:pointer;background:var(--bg3);
  color:var(--fg);border:1px solid var(--line);font-size:13px;line-height:1.35}
.question .qopt:hover{border-color:var(--usr)}
.question .qopt.sel{background:var(--sel);border-color:var(--usr);color:var(--fg)}
.question .qopt .od{display:block;color:var(--mut);font-size:11.5px;margin-top:2px}
.question .qother{margin-top:7px;width:100%;background:var(--bg3);color:var(--fg);
  border:1px solid var(--line);border-radius:6px;padding:7px;font-size:13px}
.question .qbtns{display:flex;gap:8px;padding:8px 10px;border-top:1px solid var(--infoln)}
.question .qbtns button{flex:1;padding:9px;border-radius:6px;cursor:pointer;font-size:14px;font-weight:700;
  background:var(--okbg);color:var(--addfg);border:1px solid var(--add)}
.question .qbtns button:disabled{opacity:.45;cursor:not-allowed}
.question.done .qbtns{display:none}
.question.done .qopt,.question.done .qother{pointer-events:none;opacity:.7}
.question .qdone{padding:8px 10px;border-top:1px solid var(--infoln);color:var(--addfg);font-weight:600}
#stop{background:var(--nobg);color:var(--delfg);border:1px solid var(--del);border-radius:10px;width:38px;height:38px;flex:none;display:inline-flex;align-items:center;justify-content:center;font-size:15px;font-weight:700;cursor:pointer}

/* sessions sidebar + shell layout */
.iconbtn{background:none;border:none;color:var(--fg);font-size:17px;cursor:pointer;padding:2px 5px;line-height:1}
.curname{font-size:13px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0;max-width:46vw}
.ctx{display:none;align-items:center;gap:6px;font-size:12.5px;font-weight:600;color:var(--fg);white-space:nowrap;font-family:var(--fmono)}
.usage{display:none;align-items:center;gap:6px;font-size:12.5px;font-weight:600;color:var(--fg);white-space:nowrap;font-family:var(--fmono);
  border-left:1px solid var(--line);padding-left:11px;margin-left:4px}
.ctx .ulabel,.usage .ulabel{opacity:.7}
.usage .useg{display:inline-flex;align-items:center;gap:5px}
.usage .useg + .useg::before{content:"|";opacity:.3;font-weight:400}   /* divider between 5h | 7d */
.agentbtn{display:none;align-items:center;gap:5px;font-size:13px;color:var(--mut);font-family:var(--fmono);
  border-left:1px solid var(--line);padding-left:10px;margin-left:4px}
.agentbtn.on{display:inline-flex;color:var(--acc)}
.agentbtn .agentn{min-width:16px;height:16px;border-radius:8px;background:var(--bg3);border:1px solid var(--line);
  display:inline-flex;align-items:center;justify-content:center;font-size:10px;color:var(--fg);padding:0 4px}
.agentbtn.busy .agentn{border-color:var(--tool);color:var(--tool);box-shadow:0 0 5px var(--tool)}
/* shared segmented meter: 5 cells × 20%, whole bar coloured by the total % (Context + Usage) */
.cells{display:inline-flex;gap:2px;align-items:center}
.cells .cell{width:7px;height:13px;border-radius:2px;background:var(--bg3);border:1px solid var(--line);box-sizing:border-box;transition:background .25s,box-shadow .25s}
/* Meter signal ramp — FIXED hues, deliberately not the semantic tokens.
   green/amber/orange/red mean the same thing in every theme, and the semantic
   tokens are tuned for text: pushing them to a text contrast ratio turns the
   ramp muddy (a dark olive "yellow"). Light themes get the same hues one notch
   deeper, because a bright yellow on a white ground is otherwise invisible. */
:root{--mg:#2fbf4f;--my:#ecc020;--mo:#ff8c1a;--mr:#f5483b}
:root[data-theme="light"],:root[data-theme="solarized-light"],:root[data-theme="catppuccin-latte"],
:root[data-theme="gruvbox-light"],:root[data-theme="rose-pine-dawn"],:root[data-theme="one-light"],
:root[data-theme="ayu-light"]{--mg:#22a83f;--my:#d9a800;--mo:#f57c00;--mr:#ef3f31}
.cells.lv-g{color:var(--mg)}.cells.lv-y{color:var(--my)}
.cells.lv-o{color:var(--mo)}.cells.lv-r{color:var(--mr)}
.cells .cell.on{background:currentColor;border-color:currentColor;box-shadow:0 0 4px currentColor}
@media(max-width:680px){.usage{display:none!important}.agentbtn>span:first-child{display:none}.agentbtn{padding-left:6px}}
@media(max-width:760px){
  #agentPanel .agbody{flex-direction:column}
  #agentList{width:auto;max-height:36vh;border-right:none;border-bottom:1px solid var(--line)}
}
#shell{flex:1;display:flex;min-height:0;position:relative}
#mainCol{flex:1;display:flex;flex-direction:column;min-width:0}
#sessionTabs{height:38px;flex:0 0 38px;display:flex;align-items:stretch;overflow-x:auto;overflow-y:hidden;
  background:var(--bg2);border-bottom:1px solid var(--line);scrollbar-width:thin;scrollbar-color:var(--line) transparent}
#sessionTabs[hidden]{display:none}
.stab{height:37px;flex:0 0 auto;max-width:250px;display:flex;align-items:center;border-right:1px solid var(--line);
  background:var(--bg2);color:var(--mut);position:relative}
.stab.active{background:var(--bg);color:var(--fg);box-shadow:inset 0 -2px var(--acc)}
.stab.unread:not(.active){color:var(--fg)}
.stab-main{height:37px;min-width:90px;max-width:216px;display:flex;align-items:center;gap:7px;padding:0 5px 0 10px;
  border:0;background:transparent;color:inherit;font:inherit;cursor:pointer;overflow:hidden}
.stab-dot{width:7px;height:7px;flex:none;border-radius:50%;background:var(--add)}
.stab-dot.busy{background:var(--tool);box-shadow:0 0 5px var(--tool);animation:pulse 1s infinite}
.stab-dot.ended{background:var(--mut);box-shadow:none}
.stab-text{min-width:0;display:flex;align-items:baseline;overflow:hidden;white-space:nowrap}
.stab-label{font-size:12.5px;overflow:hidden;text-overflow:ellipsis}
.stab-unread{display:none;width:5px;height:5px;flex:none;border-radius:50%;background:var(--acc)}
.stab.unread:not(.active) .stab-unread{display:inline-block}
.stab-close{width:27px;height:27px;flex:none;display:inline-flex;align-items:center;justify-content:center;border:0;
  background:transparent;color:var(--mut);font-size:15px;line-height:1;cursor:pointer;padding:0}
.stab-close:hover{color:var(--fg);background:var(--bg3)}
@media(max-width:680px){.stab{max-width:190px}.stab-main{max-width:158px}}
#sidebar{width:var(--sbw,270px);flex-shrink:0;background:var(--bg2);border-right:1px solid var(--line);display:flex;flex-direction:column;overflow-y:auto}
#sidebar.collapsed{display:none}
/* desktop: drag handle on the sidebar's right edge to resize */
#sbresize{flex-shrink:0;width:5px;cursor:col-resize;background:transparent;transition:background .12s;z-index:5}
#sbresize:hover,#sbresize.drag{background:var(--acc)}
#sidebar.collapsed + #sbresize{display:none}
.sb-new{padding:8px;border-bottom:1px solid var(--line);display:flex;flex-direction:column;gap:6px}
.sb-new select,.sb-new input{width:100%;background:var(--bg3);color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:5px 7px;font-size:12.5px}
.cwdwrap{position:relative;display:none}
.cwdwrap.show{display:block}
#cwdac{position:absolute;left:0;right:0;top:100%;z-index:50;background:var(--bg3);border:1px solid var(--acc);border-top:none;border-radius:0 0 6px 6px;max-height:240px;overflow:auto;display:none;box-shadow:0 8px 18px rgba(0,0,0,.5)}
#cwdac.on{display:block}
.acitem{padding:5px 8px;cursor:pointer;border-bottom:1px solid var(--line)}
.acitem:last-child{border-bottom:none}
.acitem:hover,.acitem.sel{background:var(--acc)}
.acname{font-size:12px;font-family:var(--fmono);color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.acpath{font-size:10px;font-family:var(--fmono);color:var(--mut);overflow-wrap:anywhere;line-height:1.3}
.acitem:hover .acname,.acitem.sel .acname,.acitem:hover .acpath,.acitem.sel .acpath{color:var(--onacc)}
.acmore{padding:5px 8px;font-size:10px;color:var(--mut);font-style:italic}
.sb-row2{display:flex;gap:6px}.sb-row2 select{flex:1;min-width:0}
#mrefresh{flex:0 0 auto;width:28px;padding:0;background:var(--bg3);color:var(--fg);border:1px solid var(--line);border-radius:5px;font-size:13px;cursor:pointer;line-height:1}
#mrefresh:hover{color:var(--acc);border-color:var(--acc)}
#mrefresh.busy{color:var(--acc);animation:mrspin .8s linear infinite;pointer-events:none}
@keyframes mrspin{to{transform:rotate(360deg)}}
.newbtn{background:var(--acc);color:var(--onacc);font-weight:700;border:none;border-radius:6px;padding:7px;font-size:13px;cursor:pointer}
.newbtn:hover{filter:brightness(1.08)}
#srchopen{display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;
  padding:6px 8px;background:var(--bg3);color:var(--dim);
  border:1px solid var(--line);border-radius:6px;font-size:12px;cursor:pointer}
#srchopen:hover{color:var(--fg);border-color:var(--acc)}
#srchopen kbd{font:inherit;font-size:10.5px;color:var(--dim);border:1px solid var(--line);
  border-radius:3px;padding:0 4px;background:var(--bg2)}
.impline{display:flex;flex-direction:column;gap:5px;margin-top:1px}
#impbtn{width:100%;background:var(--bg3);color:var(--acc);font-weight:700;
  border:1px solid var(--acc);border-radius:6px;padding:7px;font-size:13px;cursor:pointer}
#impbtn:hover{background:var(--acc);color:var(--onacc)}
#impmsg{font-size:11px;color:var(--dim);line-height:1.35;word-break:break-word}
#impmsg:empty{display:none}
#impmsg.bad{color:var(--delfg)}
.sb-sec{border-bottom:1px solid var(--line);padding:4px 0 6px}
.sb-h{font-size:11px;letter-spacing:.05em;text-transform:uppercase;color:var(--mut);padding:6px 10px 4px;display:flex;align-items:center;gap:6px}
.sb-h .cnt{background:var(--bg3);border-radius:8px;padding:0 6px;font-size:10px;color:var(--mut)}
.sb-h .grow{flex:1}
.sb-ref{cursor:pointer}.sb-ref:hover{color:var(--fg)}
.sb-foot{margin-top:auto;padding:8px 10px;border-top:1px solid var(--line);display:flex;align-items:center;gap:8px}
.sb-foot-l{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);white-space:nowrap}
.sb-foot select{flex:1;min-width:0;background:var(--bg3);color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:4px 6px;font-size:12px}
.sb-empty{color:var(--mut);font-size:12px;padding:5px 10px;line-height:1.4}
.srow{padding:7px 9px;cursor:pointer;display:flex;gap:8px;align-items:center;border-left:2px solid transparent}
.srow:hover{background:var(--bg3)}
.srow.active{background:var(--sel);border-left-color:var(--acc)}
.srow.ended{opacity:.6}
.srow .sdot,.prow .sdot{width:7px;height:7px;border-radius:50%;background:var(--mut);flex-shrink:0}
.srow .sdot.on,.prow .sdot.on{background:var(--add)}
.srow .sdot.busy,.prow .sdot.busy{background:var(--tool);box-shadow:0 0 5px var(--tool);animation:pulse 1s infinite}
.srow .smeta{flex:1;min-width:0}
.srow .sname{font-size:12.5px;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.srow .ssub{font-size:11px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.srow .skebab{flex-shrink:0;font-size:18px;line-height:1;padding:1px 6px;color:var(--fg);cursor:pointer;opacity:.9;border-radius:5px;user-select:none}
.srow:hover .skebab{opacity:1}.srow .skebab:hover{color:var(--acc);background:var(--bg3)}
/* A project's actions button is a horizontal ··· in a padded box: the vertical ⋮
   stays the session affordance, so the two never read as the same control, and
   the 26x24 target is hittable — the bare glyph was a ~10px-wide tap area.
   Drawn as three dots rather than typed, so it can't shift with the font. */
.prow .pkebab{flex-shrink:0;display:flex;align-items:center;justify-content:center;
  gap:2.5px;width:26px;height:24px;padding:0;border-radius:6px;opacity:.55}
.prow .pkebab::before,.prow .pkebab::after,.prow .pkebab>i{content:"";width:3px;height:3px;
  border-radius:50%;background:currentColor}
.prow:hover .pkebab{opacity:1}
.prow .pkebab:hover{color:var(--acc);background:var(--bg3)}
/* project groups — the sidebar is grouped by folder, so a row is a project and
   the sessions it owns nest under it. A project is Live when one of its sessions
   is running, which is why the Live section lists folders, not threads. */
.pgroup{border-left:2px solid transparent}
.prow{padding:6px 9px;cursor:pointer;display:flex;gap:7px;align-items:center;user-select:none}
.prow:hover{background:var(--bg3)}
.prow .caret{display:inline-flex;align-items:center;justify-content:center;flex:none;
  width:15px;height:15px;border-radius:5px;color:var(--fg);opacity:.65;transition:opacity .15s}
.prow .caret::before{content:"";width:6px;height:6px;border-right:2px solid currentColor;
  border-bottom:2px solid currentColor;border-radius:1.5px;
  transform:translate(-2px,0) rotate(-45deg);transition:transform .2s ease}
.pgroup.open .prow .caret::before{transform:translate(-1px,-2px) rotate(45deg)}
.prow:hover .caret{opacity:1}
.prow .pmeta{flex:1;min-width:0}
.prow .pname{font-size:12.5px;font-weight:600;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prow .pstar{color:var(--tool);font-size:11px}
.prow .psub{font-size:11px;color:var(--mut);font-family:var(--fmono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prow .pn{flex:none;font-size:10px;color:var(--mut);background:var(--bg3);border:1px solid var(--line);
  border-radius:8px;padding:0 6px;line-height:15px}
.pgroup .plist{display:none}
.pgroup.open .plist{display:block}
.pgroup .plist .srow{padding-left:31px;border-left:none}
.pgroup .plist .srow .sname{font-size:12px}
.pgroup .plist .sb-empty{padding-left:31px;font-size:11px}

/* Manage sessions — a project can accumulate a lot of threads (sub-agents in
   particular), so the ⋮ menu opens a checklist for deleting several at once. */
#pman{position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.55);
  display:flex;justify-content:center;align-items:flex-start;padding:9vh 16px 16px}
#pman[hidden]{display:none}
#pmanpanel{width:min(680px,100%);max-height:78vh;display:flex;flex-direction:column;
  background:var(--bg2);border:1px solid var(--line);border-radius:10px;
  box-shadow:0 18px 60px rgba(0,0,0,.5);overflow:hidden}
#pman .pmsub{flex:1;min-width:0;font-family:var(--fmono);font-size:11.5px;color:var(--mut);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pmbar{display:flex;align-items:center;gap:10px;padding:7px 12px;
  border-bottom:1px solid var(--line);font-size:12px;color:var(--mut)}
.pmbar label{display:flex;align-items:center;gap:6px;cursor:pointer;color:var(--fg);user-select:none}
.pmbar .grow{flex:1}
#pmanlist{flex:1;min-height:0;overflow:auto}
.pmrow{display:flex;align-items:center;gap:9px;padding:7px 12px;
  border-bottom:1px solid var(--line);cursor:pointer}
.pmrow:hover{background:var(--bg3)}
.pmrow:last-child{border-bottom:none}
.pmrow input{flex:none;cursor:pointer;width:15px;height:15px;accent-color:var(--acc)}
.pmrow .pmmeta{flex:1;min-width:0}
.pmrow .pmt{font-size:12.5px;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pmrow .pmd{font-size:11px;color:var(--mut);font-family:var(--fmono)}
.pmrow .pmlive{flex:none;font-size:10px;color:var(--addfg);border:1px solid var(--add);
  border-radius:8px;padding:0 6px;line-height:15px}
#pman .fbtns{padding:10px;border-top:1px solid var(--line);display:flex;gap:8px;justify-content:flex-end}
#pman .fbtns button{padding:7px 14px;border-radius:6px;font-size:13px;font-weight:700;cursor:pointer}
#pmandel{background:var(--nobg);color:var(--delfg);border:1px solid var(--del)}
#pmandel:disabled{opacity:.45;cursor:not-allowed}

/* Import folder — pick a directory and it becomes a sidebar project even before
   it owns a session. Same dircomplete backend as the custom-path box. */
#fimp{position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.55);
  display:flex;justify-content:center;align-items:flex-start;padding:12vh 16px 16px}
#fimp[hidden]{display:none}
#fimppanel{width:min(620px,100%);max-height:70vh;display:flex;flex-direction:column;
  background:var(--bg2);border:1px solid var(--line);border-radius:10px;
  box-shadow:0 18px 60px rgba(0,0,0,.5);overflow:hidden}
#fimp .fh,#pman .fh{display:flex;gap:10px;align-items:baseline;padding:10px 12px;
  border-bottom:1px solid var(--line)}
#fimp .fh .ft{flex:1;font-size:13px;font-weight:600;color:var(--fg)}
#pman .fh .ft{flex:none;font-size:13px;font-weight:600;color:var(--fg);white-space:nowrap}
#pman .fh .btn{align-self:center}
#fimpq{width:100%;background:var(--bg3);color:var(--fg);border:1px solid var(--line);
  border-radius:6px;padding:9px 11px;font-size:13px;font-family:var(--fmono);outline:none}
#fimpq:focus{border-color:var(--acc)}
#fimp .fbody{padding:10px;display:flex;flex-direction:column;gap:8px;min-height:0}
#fimpac{flex:1;min-height:0;overflow:auto;border:1px solid var(--line);border-radius:6px;background:var(--bg)}
#fimpmsg{font-size:11.5px;color:var(--mut);min-height:1em}
#fimpmsg.bad{color:var(--delfg)}
#fimp .fbtns{display:flex;gap:8px;justify-content:flex-end}
#fimp .fbtns button{padding:7px 14px;border-radius:6px;font-size:13px;font-weight:700;cursor:pointer}
#fimp .fadd{background:var(--acc);color:var(--onacc);border:none}
#fimp .fcancel{background:var(--bg3);color:var(--fg);border:1px solid var(--line)}
.improw{display:flex;gap:5px}
.improw button{flex:1;min-width:0}
#fimpbtn{background:var(--bg3);color:var(--fg);border:1px solid var(--line);
  border-radius:6px;padding:7px;font-size:13px;cursor:pointer;white-space:nowrap}
#fimpbtn:hover{border-color:var(--acc);color:var(--acc)}

/* collapsible past-session sections */
.sb-h.sb-toggle{cursor:pointer;user-select:none}
.sb-h .caret{display:inline-flex;align-items:center;justify-content:center;flex:none;
  width:18px;height:18px;margin-left:-3px;border-radius:6px;color:var(--fg);opacity:.72;
  transition:background .15s,opacity .15s}
.sb-h .caret::before{content:"";width:7px;height:7px;border-right:2px solid currentColor;
  border-bottom:2px solid currentColor;border-radius:1.5px;
  transform:translate(-1px,-2px) rotate(45deg);transition:transform .2s ease}
.sb-sec.collapsed .caret::before{transform:translate(-2px,0) rotate(-45deg)}
.sb-h.sb-toggle:hover .caret{opacity:1;background:var(--bg3)}
.sb-sec.collapsed .seclist{display:none}
/* project groups expand in place, so the section must not be its own scroll
   box — a nested scrollbar swallows the sessions you just expanded. The
   sidebar itself scrolls instead. */
.seclist{max-height:none}
/* shared per-card action menu (⋯) */
#cardMenu{position:fixed;z-index:60;min-width:152px;background:var(--bg2);border:1px solid var(--line);border-radius:8px;box-shadow:0 8px 26px rgba(0,0,0,.55);padding:4px;display:none}
#cardMenu.on{display:block}
#cardMenu .mi{padding:7px 10px;font-size:12.5px;color:var(--fg);cursor:pointer;border-radius:5px;white-space:nowrap}
#cardMenu .mi:hover{background:var(--bg3)}
#cardMenu .mi.danger{color:var(--delfg)}#cardMenu .mi.danger:hover{background:var(--nobg)}
#cardMenu .cfgrow{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:5px 9px;font-size:12.5px;color:var(--mut)}
#cardMenu .cfgrow .cfgsel{background:var(--bg3);color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:3px 6px;font-size:12px;cursor:pointer}
.fscope{font-size:10px;color:var(--mut);font-family:var(--fmono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:130px}
#srch{position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.55);
  display:flex;justify-content:center;align-items:flex-start;padding:8vh 16px 16px}
#srch[hidden]{display:none}
#srchpanel{width:min(900px,100%);max-height:78vh;display:flex;flex-direction:column;
  background:var(--bg2);border:1px solid var(--line);border-radius:10px;
  box-shadow:0 18px 60px rgba(0,0,0,.5);overflow:hidden}
.srchtop{display:flex;gap:8px;padding:10px;border-bottom:1px solid var(--line);align-items:center}
#srchq{flex:1;min-width:0;background:var(--bg3);color:var(--fg);border:1px solid var(--line);
  border-radius:6px;padding:9px 11px;font-size:14px;outline:none}
#srchq:focus{border-color:var(--acc)}
#srchscope{flex:0 0 auto;background:var(--bg3);color:var(--fg);border:1px solid var(--line);
  border-radius:6px;padding:8px;font-size:12px}
#srchx{flex:0 0 auto;background:none;border:none;color:var(--dim);font-size:16px;cursor:pointer;padding:4px 8px}
#srchx:hover{color:var(--fg)}
#srchmeta{padding:7px 12px;font-size:11.5px;color:var(--dim);border-bottom:1px solid var(--line)}
#srchres{overflow:auto;padding:4px 0}
.sres{padding:9px 12px;border-bottom:1px solid var(--line);cursor:pointer}
.sres:hover{background:var(--bg3)}
.sres .sh1{display:flex;gap:8px;align-items:baseline;font-size:11.5px;color:var(--dim);margin-bottom:3px}
.sres .sh1 b{color:var(--fg);font-weight:600;font-size:12.5px}
.sres .sh1 .role{border:1px solid var(--line);border-radius:3px;padding:0 4px;font-size:10px}
.sres .sh2{font-size:12.5px;line-height:1.5;color:var(--fg);word-break:break-word;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.sres .sh2 mark{background:var(--acc);color:var(--bg);border-radius:2px;padding:0 1px}
#srchthread{display:flex;flex-direction:column;min-height:0;overflow:hidden}
#srchthread[hidden]{display:none}
.thhead{display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--line)}
.thhead .thtitle{flex:1;min-width:0;font-size:12.5px;color:var(--fg);font-weight:600;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.thhead button{flex:0 0 auto;background:var(--bg3);color:var(--fg);border:1px solid var(--line);
  border-radius:5px;padding:4px 9px;font-size:11.5px;cursor:pointer}
.thhead button:hover{border-color:var(--acc);color:var(--acc)}
#thopen{color:var(--acc);border-color:var(--acc)}
#thopen:hover{background:var(--acc);color:var(--onacc)}
#thbody{overflow:auto;padding:6px 0 12px}
.thmsg{padding:7px 14px;border-left:3px solid transparent}
.thmsg .thr{font-size:10.5px;color:var(--dim);margin-bottom:2px;letter-spacing:.03em}
.thmsg .tht{font-size:12.5px;line-height:1.55;color:var(--fg);white-space:pre-wrap;word-break:break-word}
.thmsg.user{border-left-color:var(--acc)}
.thmsg.assistant{border-left-color:var(--line)}
.thmsg.tool .tht{font-family:var(--fmono);font-size:11.5px;color:var(--dim)}
.thmsg.target{background:var(--bg3)}
.thmsg .tht mark{background:var(--acc);color:var(--bg);border-radius:2px;padding:0 1px}
.thmore{display:block;width:calc(100% - 24px);margin:6px 12px;padding:5px;background:var(--bg3);
  color:var(--dim);border:1px dashed var(--line);border-radius:5px;font-size:11.5px;cursor:pointer}
.thmore:hover{color:var(--acc);border-color:var(--acc)}
.thend{text-align:center;font-size:11px;color:var(--mut);padding:6px}
#sb-backdrop{display:none}
@media(max-width:860px){
  #sidebar{position:fixed;left:0;top:0;bottom:0;z-index:40;transform:translateX(-100%);transition:transform .34s cubic-bezier(.32,.72,0,1);width:min(310px,86vw);box-shadow:2px 0 14px rgba(0,0,0,.5)}
  #sidebar.open{transform:none}
  #sidebar.collapsed{display:flex}
  #sb-backdrop.show{display:block;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:35}
  #sbresize,#drresize{display:none}
}
.bubble .math.display{display:block;margin:6px 0;overflow-x:auto;overflow-y:hidden;max-width:100%}
.katex-display{margin:.35em 0!important}

/* keyboard focus — every interactive element gets a ring; mouse clicks stay clean */
button:focus-visible,select:focus-visible,input:focus-visible,textarea:focus-visible,
a:focus-visible,[tabindex]:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
/* press feedback on the commit actions only (approve/deny/answer/send/stop/new/import).
   High-frequency toolbar buttons stay instant — motion there charges its cost every click. */
.approval .abtns button,.question .qbtns button,#send,#stop,.newbtn,#impbtn{
  transition:transform .1s cubic-bezier(.2,0,0,1)}
.approval .abtns button:active,.question .qbtns button:active,
#send:not(:disabled):active,#stop:active,.newbtn:active,#impbtn:active{transform:scale(.97)}

/* reduced motion: kill the loops, keep the static cue (busy dot keeps its --tool colour) */
@media(prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms!important;animation-iteration-count:1!important;
    transition-duration:.01ms!important}
  .dot.busy,.srow .sdot.busy,.stab-dot.busy,#thinking .glyph{animation:none!important;opacity:1!important}
}
</style>
<link rel="stylesheet" href="/static/katex/katex.min.css">
<script src="/static/katex/katex.min.js"></script>
</head>
<body>
<header>
  <button class="iconbtn" id="navtoggle" title="sessions">☰</button>
  <span class="curname" id="curname">— no session —</span>
  <span class="ctx" id="ctx" title="context-window usage"></span>
  <span class="usage" id="usage" title="usage limits (5h / 7d)"></span>
  <button class="iconbtn agentbtn" id="agentbtn" title="subagents"><span>Agents</span><span class="agentn" id="agentN">0</span></button>
</header>

<div id="shell">
  <aside id="sidebar">
    <div class="sb-brand">⬡ Codex Console</div>
    <div class="sb-new">
      <button id="srchopen" title="search all conversation history"><span>🔍 Search history</span><kbd>⌘K</kbd></button>
      <select id="project" title="working directory for a new session"></select>
      <div class="cwdwrap" id="cwdwrap"><input id="cwd" placeholder="type a path…  ↑↓ to pick" autocomplete="off"><div id="cwdac"></div></div>
      <div class="sb-row2">
        <select id="model" title="model"><option value="default">model: default</option></select>
        <button id="mrefresh" title="refresh model list from Codex">↻</button>
        <select id="mode" title="approval policy"><option value="full-access" selected>🔓 Full access</option><option value="on-request">🔐 Approve</option><option value="auto">⚡ Auto (sandbox)</option><option value="read-only">👁 Read-only</option></select>
      </div>
      <button class="newbtn" id="newbtn">＋ New session</button>
      <div class="impline">
        <div class="improw">
          <input type="file" id="impfile" accept=".jsonl,application/x-ndjson" hidden>
          <button id="impbtn" title="adopt a Codex rollout exported from another machine">Import session</button>
          <button id="fimpbtn" title="add a folder to the sidebar as a project">Import folder</button>
        </div>
        <span id="impmsg"></span>
      </div>
    </div>
    <div class="sb-sec">
      <div class="sb-h">Live <span id="liveN" class="cnt">0</span></div>
      <div id="liveList"><div class="sb-empty">no active project</div></div>
    </div>
    <div class="sb-sec" id="secFav">
      <div class="sb-h sb-toggle"><span class="caret"></span>★ Favorites <span id="favN" class="cnt">0</span></div>
      <div id="favList" class="seclist"><div class="sb-empty">star a project to pin it here</div></div>
    </div>
    <div class="sb-sec" id="secRecent">
      <div class="sb-h sb-toggle"><span class="caret"></span>🕘 Recent <span class="grow"></span><span class="sb-ref" id="resumeRef" title="rescan">↻</span></div>
      <div id="recentList" class="seclist"><div class="sb-empty">—</div></div>
    </div>
    <div class="sb-foot">
      <span class="sb-foot-l">Theme</span>
      <select id="theme" title="color theme">
        <optgroup label="Dark">
          <option value="dark">Dark</option>
          <option value="dracula">Dracula</option>
          <option value="nord">Nord</option>
          <option value="tokyo-night">Tokyo Night</option>
          <option value="catppuccin">Catppuccin Mocha</option>
          <option value="gruvbox">Gruvbox Dark</option>
        </optgroup>
        <optgroup label="Light">
          <option value="light">Light</option>
          <option value="solarized-light">Solarized Light</option>
          <option value="catppuccin-latte">Catppuccin Latte</option>
          <option value="gruvbox-light">Gruvbox Light</option>
          <option value="rose-pine-dawn">Rosé Pine Dawn</option>
          <option value="one-light">One Light</option>
          <option value="ayu-light">Ayu Light</option>
        </optgroup>
      </select>
    </div>
  </aside>
  <div id="sbresize"></div>
  <div id="sb-backdrop"></div>
  <div id="srch" hidden><div id="srchpanel">
    <div class="srchtop">
      <input id="srchq" type="text" placeholder="search every conversation..." autocomplete="off" spellcheck="false">
      <select id="srchscope" title="how much history to search">
        <option value="all">all history</option>
        <option value="project">this folder + subfolders</option>
        <option value="session">this conversation</option>
      </select>
      <button id="srchx" title="close (Esc)">✕</button>
    </div>
    <div id="srchmeta">Search Codex history: user prompts, assistant answers, and touched files or commands.</div>
    <div id="srchres"></div>
    <div id="srchthread" hidden>
      <div class="thhead">
        <button id="thback" title="back to results">← results</button>
        <div class="thtitle"></div>
        <button id="thopen" title="resume this conversation in the chat pane">Open session</button>
      </div>
      <div id="thbody"></div>
    </div>
  </div></div>
  <div id="mainCol">
    <div id="sessionTabs" role="tablist" aria-label="Open sessions" hidden></div>
    <div id="chat"><div class="plandock" id="planDock" hidden></div><div class="wrap" id="stream"></div></div>
    <div id="composer">
      <div class="pillrow">
        <div id="thinking"><div class="twrap"><span class="dot" id="dot"></span><span class="glyph">✶</span><span class="word">idle</span><span class="meta"></span></div></div>
        <span id="effort" title="thinking effort — click to change">🧠 max</span>
      </div>
      <div id="queue"></div>
      <div id="attach"></div>
      <div class="wrap2">
      <textarea id="ta" rows="1" placeholder="Type a message…  (Enter to send · Shift+Enter newline · paste an image)" disabled></textarea>
      <button id="stop" title="interrupt / stop" style="display:none">⏹</button>
      <button id="send" disabled>➤</button>
    </div></div>
  </div>
</div>

<div id="drawer">
  <div id="drresize"></div>
  <div class="dh"><span class="tab on" id="tabEdits">Edits <span id="editN">0</span></span><span class="tab" id="tabGit">Git diff</span><span class="grow"></span><span class="btn" id="grefresh">↻</span><span class="btn" id="dclose">✕</span></div>
  <div class="dc" id="edits"><div class="empty">no file changes yet</div></div>
  <div class="dc" id="gitc" style="display:none"><div class="empty">—</div></div>
</div>
<div id="agentPanel">
  <div class="agh"><span class="agtit">Subagents</span><span class="agmeta" id="agentMeta"></span><span class="grow"></span><span class="btn" id="agentClose">✕</span></div>
  <div class="agbody">
    <div id="agentList"><div class="empty">no subagents yet</div></div>
    <div id="agentDetail"><div class="empty">select a subagent</div></div>
  </div>
</div>
<div id="fimp" hidden>
  <div id="fimppanel">
    <div class="fh"><span class="ft">📁 Import folder</span>
      <button class="btn" id="fimpx" title="close">✕</button></div>
    <div class="fbody">
      <input id="fimpq" placeholder="type a path…  ↑↓ to pick, Enter to add" autocomplete="off" spellcheck="false">
      <div id="fimpac"></div>
      <div id="fimpmsg"></div>
      <div class="fbtns"><button class="fcancel" id="fimpcancel">Cancel</button><button class="fadd" id="fimpok">Add project</button></div>
    </div>
  </div>
</div>
<div id="pman" hidden>
  <div id="pmanpanel">
    <div class="fh"><span class="ft">☑ Manage</span><span id="pmansub" class="pmsub"></span>
      <button class="btn" id="pmanx" title="close">✕</button></div>
    <div class="pmbar">
      <label><input type="checkbox" id="pmanall"> Select all</label>
      <span class="grow"></span><span id="pmancount">0 selected</span>
    </div>
    <div id="pmanlist"></div>
    <div class="fbtns"><button class="fcancel" id="pmanclose">Close</button>
      <button id="pmandel" disabled>🗑 Delete selected</button></div>
  </div>
</div>
<div id="cardMenu"></div>

<script>
const $=s=>document.querySelector(s);
const stream=$('#stream'), ta=$('#ta'), sendBtn=$('#send');
let ws=null, running=false, ready=false, compacting=false, cwd='', tools={};
let tokUp=0,tokOut=0,tokShow=false;   /* live streaming token counts shown in the pill */
let sid=null, curCC=null, editCount=0, pendingStart=false, reconnectT=0;
const FALLBACK_EFFORTS=['minimal','low','medium','high','xhigh'];
let MODELS=[];   /* live Codex app-server catalog: [{id,name,isDefault,...}] */
let DEFAULT_MODEL_ID='';   /* resolved from config.toml, not catalog isDefault */
let MODEL_OPTIONS=[['default','model: default']];
let modelRetryT=0;
const WEBFM_CONFIG_URL = __CODEX_CONSOLE_WEBFM_URL__;
let curEffort=localStorage.getItem('al_effort')||'xhigh';
let activeModel='default';
let showThink=false;
let liveSessions=[], projData=[], HOMEDIR='';
let subagents={}, subagentCurrent='';
let currentCtx=null,sessionViewCache=new Map(),restoredViewId='';
/* which project groups are expanded — per device, survives reloads */
let pExp=new Set();try{pExp=new Set(JSON.parse(localStorage.getItem('al_pexp')||'[]'));}catch(e){}
const EDIT_TOOLS=new Set(['Edit','MultiEdit','Write','NotebookEdit','apply_patch']);
const SKEY='al_session';
const TABSKEY='al_session_tabs';
let sessionTabState=[],tabsValidated=false;
try{const savedTabs=JSON.parse(localStorage.getItem(TABSKEY)||'[]');
  if(Array.isArray(savedTabs))sessionTabState=savedTabs.filter(t=>t&&typeof t.id==='string').slice(0,24);}catch(e){}

function esc(s){return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function escAttr(s){return esc(s).replace(/"/g,'&quot;');}
function unescHtml(s){return (s||'').replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');}
function webfmBase(){
  const cfg=(WEBFM_CONFIG_URL||'').replace(/\/+$/,'');
  return cfg || (location.protocol+'//'+location.hostname+':7701');
}
function webfmOpenUrl(path){return webfmBase()+'/?open='+encodeURIComponent(path);}
function cleanLinkTarget(raw){
  let t=unescHtml((raw||'').trim());
  if(t[0]==='<'&&t[t.length-1]==='>')t=t.slice(1,-1).trim();
  try{t=decodeURI(t);}catch(e){}
  return t;
}
function isLocalTarget(t){return t==='~'||t.startsWith('~/')||t.startsWith('/')||t.startsWith('file://');}
function stripLineRef(t){return t.replace(/:(\d+)(?::\d+)?$/,'');}
function protectMarkdownLinks(h,links){
  return h.replace(/\[([^\]\n]+)\]\((?:&lt;([^\n]*?)&gt;|([^)\s]+))\)/g,(m,label,angle,bare)=>{
    const target=cleanLinkTarget(angle!=null?angle:bare);
    let href='';
    if(/^https?:\/\//i.test(target))href=target;
    else if(isLocalTarget(target))href=webfmOpenUrl(stripLineRef(target));
    else return m;
    links.push('<a href="'+escAttr(href)+'" target="_blank" rel="noopener">'+label+'</a>');
    return '%%LK'+(links.length-1)+'%%';
  });
}
function linkLocalPaths(h){
  const exts='pdf|png|jpe?g|gif|webp|svg|avif|heic|txt|md|markdown|rst|log|py|pyi|ipynb|js|mjs|ts|tsx|jsx|css|json|ya?ml|toml|ini|cfg|conf|xml|csv|tsv|sh|bash|zsh|fish|c|h|cpp|cc|hpp|rs|go|java|kt|rb|php|pl|lua|sql|tex|bib|m|jl|r|swift|html?';
  const re=new RegExp('(^|[\\s(>])((?:~|/)[^\\n<]*?\\.('+exts+'))(?=$|[\\s.,;:!?)}\\]]|<br>)','gi');
  return h.replace(re,(m,pre,path)=>pre+'<a href="'+escAttr(webfmOpenUrl(unescHtml(path)))+'" target="_blank" rel="noopener" class="filelink">'+path+'</a>');
}
function mdTable(h){
  if(h.indexOf('|')<0)return h;
  const L=h.split('\n'),out=[];let i=0;
  const sep=l=>/^[\s:|-]+$/.test(l)&&l.includes('-')&&l.includes('|');
  const row=l=>l.replace(/^\s*\|/,'').replace(/\|\s*$/,'').split('|').map(c=>c.trim());
  while(i<L.length){
    if(L[i].includes('|')&&i+1<L.length&&sep(L[i+1])){
      const head=row(L[i]);i+=2;const rows=[];
      while(i<L.length&&L[i].trim()&&L[i].includes('|')){rows.push(row(L[i]));i++;}
      let t='<table><thead><tr>'+head.map(c=>'<th>'+c+'</th>').join('')+'</tr></thead><tbody>';
      for(const r of rows)t+='<tr>'+head.map((_,j)=>'<td>'+(r[j]==null?'':r[j])+'</td>').join('')+'</tr>';
      out.push(t+'</tbody></table>');
    }else{out.push(L[i]);i++;}
  }
  return out.join('\n');
}
function md(src){
  src=src||''; const bl=[], ml=[], il=[], ll=[];
  src=src.replace(/```(\w*)\n?([\s\S]*?)```/g,(m,l,c)=>{
    const lang=(l||'').toLowerCase();
    if(lang==='math'){   /* fenced math (GitHub/Zulip ```math convention) → display equation */
      let mc=c.replace(/\n$/,'').trim();
      /* codex often nests display/inline delimiters INSIDE the fence; strip one
         layer (the fence already means display math, and a literal \[ … \] passed
         to KaTeX would error and fall back to showing the raw source) */
      mc=mc.replace(/^\\\[([\s\S]*?)\\\]$/,'$1').replace(/^\$\$([\s\S]*?)\$\$$/,'$1').replace(/^\\\(([\s\S]*?)\\\)$/,'$1');
      ml.push({t:mc.trim(),d:1});return ' %%MJ'+(ml.length-1)+'%% ';}
    bl.push('<pre><code>'+esc(c.replace(/\n$/,''))+'</code></pre>');return ' %%CB'+(bl.length-1)+'%% ';});
  /* protect LaTeX from the markdown passes; KaTeX renders it after insert.
     display ($$…$$, \[…\]) before inline (\(…\), $…$). */
  const mj=(t,d)=>{ml.push({t:t,d:d});return '%%MJ'+(ml.length-1)+'%%';};
  src=src.replace(/\$\$([\s\S]+?)\$\$/g,(m,c)=>mj(c,1));
  src=src.replace(/\\\[([\s\S]+?)\\\]/g,(m,c)=>mj(c,1));
  src=src.replace(/\\\(([\s\S]+?)\\\)/g,(m,c)=>mj(c,0));
  src=src.replace(/\$(?!\s)([^\n$]*?[^\s$])\$(?!\d)/g,(m,c)=>mj(c,0));
  let h=esc(src);
  h=mdTable(h);
  h=h.replace(/`([^`\n]+)`/g,(m,c)=>{il.push('<code>'+c+'</code>');return '%%IC'+(il.length-1)+'%%';});
  h=protectMarkdownLinks(h,ll);
  h=linkLocalPaths(h);
  h=h.replace(/%%IC(\d+)%%/g,(m,i)=>il[+i]);
  h=h.replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>');
  h=h.replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h2>$1</h2>').replace(/^# (.*)$/gm,'<h2>$1</h2>');
  h=h.replace(/%%LK(\d+)%%/g,(m,i)=>ll[+i]);
  h=h.replace(/^(\d+)\. (.*)$/gm,'<oli>$2</oli>').replace(/^[\-\*] (.*)$/gm,'<uli>$1</uli>');
  h=h.replace(/(<uli>.*?<\/uli>(?:\n<uli>.*?<\/uli>)*)/g,m=>'<ul>'+m.replace(/\n/g,'').replace(/uli>/g,'li>')+'</ul>');
  h=h.replace(/(<oli>.*?<\/oli>(?:\n<oli>.*?<\/oli>)*)/g,m=>'<ol>'+m.replace(/\n/g,'').replace(/oli>/g,'li>')+'</ol>');
  h=h.replace(/\*(\S[^*\n]*?\S|\S)\*/g,'<i>$1</i>');
  h=h.replace(/\n/g,'<br>').replace(/<br>(<(?:pre|h2|h3|ul|ol|table)>)/g,'$1').replace(/(<\/(?:pre|h2|h3|ul|ol|table)>)<br>/g,'$1');
  h=h.replace(/%%CB(\d+)%%/g,(m,i)=>bl[+i]);
  h=h.replace(/%%MJ(\d+)%%/g,(m,i)=>'<span class="math'+(ml[+i].d?' display':'')+'" data-d="'+ml[+i].d+'">'+esc(ml[+i].t)+'</span>');
  return h;
}
/* render protected LaTeX spans with KaTeX (textContent decodes the escaped TeX) */
/* lazy KaTeX: defer typesetting until a math node scrolls near the viewport — a big
   win when replaying a long, math-heavy transcript on attach/switch (no up-front burst). */
let _mathObs=null;
function _mathObserver(){if(_mathObs)return _mathObs;
  _mathObs=new IntersectionObserver(es=>{es.forEach(en=>{if(en.isIntersecting){_renderMathEl(en.target);_mathObs.unobserve(en.target);}});},
    {root:$('#chat'),rootMargin:'1200px 0px'});return _mathObs;}
function _renderMathEl(el){if(!window.katex||el.dataset.done)return;el.dataset.done='1';
  try{katex.render(el.textContent,el,{displayMode:el.dataset.d==='1',throwOnError:false,errorColor:'#f85149'});}catch(e){}}
function typesetMath(root,eager){if(!window.katex)return;
  root.querySelectorAll('.math:not([data-done])').forEach(el=>{
    if(eager)_renderMathEl(el);else _mathObserver().observe(el);});}
function diffHtml(t){return t.split('\n').map(l=>{let c='dl-ctx';
  if(l.startsWith('@@')||l.startsWith('diff ')||l.startsWith('+++')||l.startsWith('---')||l.startsWith('***'))c='dl-hdr';
  else if(l.startsWith('+'))c='dl-add';else if(l.startsWith('-'))c='dl-del';
  return '<div class="diffline '+c+'">'+esc(l||' ')+'</div>';}).join('');}
let replaying=false;   /* true while bulk-replaying a session log on attach — suppresses
                          per-event atBottom/scroll so we don't force 1000s of reflows;
                          a single scroll-to-bottom runs once at the end instead. */
function atBottom(){if(replaying)return false;const c=$('#chat');return c.scrollHeight-c.scrollTop-c.clientHeight<140;}
function scroll(){if(replaying)return;const c=$('#chat');c.scrollTop=c.scrollHeight;}

const ICON={Edit:'✏️',MultiEdit:'✏️',Write:'📝',Bash:'▶',Read:'📖',List:'📂',Search:'🔍',
  Glob:'🔍',Grep:'🔍',Task:'🤖',Agent:'🤖',ViewImage:'🖼',ImageGeneration:'🖼',
  WebFetch:'🌐',WebSearch:'🌐',TodoWrite:'☑️',NotebookEdit:'📓',
  apply_patch:'✏️',shell:'▶',web_search:'🌐'};   /* codex tools */
function primaryArg(i){if(!i)return '';if(typeof i==='string')return i.slice(0,80);
  if(i.file_path)return i.file_path.split('/').slice(-2).join('/');
  if(i.display)return (''+i.display).split('\n')[0].slice(0,90);
  if(i.query)return (''+i.query).split('\n')[0].slice(0,90);
  if(Array.isArray(i.queries)&&i.queries.length)return (''+i.queries[0]).split('\n')[0].slice(0,90);
  if(i.url)return i.url;
  if(i.command)return (''+i.command).split('\n')[0].slice(0,90);
  if(i.prompt)return (''+i.prompt).split('\n')[0].slice(0,90);
  if(i.path)return (''+i.path).split('/').slice(-2).join('/');
  if(i.pattern)return i.pattern;if(i.description)return i.description.slice(0,80);
  return '';}
function counts(ev){const i=ev.input||{};
  if(ev.tool==='Edit'&&i.new_string!==undefined)return {a:(i.new_string.match(/\n/g)||[]).length+1,d:(i.old_string.match(/\n/g)||[]).length+1};
  if(ev.tool==='Write'&&i.content!==undefined)return {a:(i.content.match(/\n/g)||[]).length+1,d:0};
  if(ev.tool==='apply_patch'&&typeof i.diff==='string'){let a=0,d=0;   /* codex unified diff */
    for(const l of i.diff.split('\n')){if(l[0]==='+'&&l[1]!=='+')a++;else if(l[0]==='-'&&l[1]!=='-')d++;}return {a,d};}
  return null;}
function toolBody(ev){const i=ev.input||{},t=ev.tool;
  if(t==='apply_patch'&&typeof i.diff==='string')return diffHtml(i.diff);   /* codex: render the unified diff directly */
  if(t==='Edit'&&i.old_string!==undefined)return diffHtml(i.old_string.split('\n').map(x=>'-'+x).join('\n')+'\n'+i.new_string.split('\n').map(x=>'+'+x).join('\n'));
  if(t==='Write'&&i.content!==undefined)return '<div class="reslabel">new file content</div>'+diffHtml(i.content.split('\n').map(x=>'+'+x).join('\n'));
  if(t==='web_search'){
    const act=i.action||'search';
    if(act==='search')return '<div class="reslabel">search</div><pre><code>'+
      esc((Array.isArray(i.queries)&&i.queries.length?i.queries.join('\n'):(i.query||'')))+'</code></pre>';
    if(act==='openPage')return '<div class="reslabel">open page</div><pre><code>'+esc(i.url||'')+'</code></pre>';
    if(act==='findInPage')return '<div class="reslabel">find in page</div><pre><code>'+esc((i.pattern||'')+(i.url?('\n'+i.url):''))+'</code></pre>';
  }
  if((t==='Bash'||t==='shell'||t==='Read'||t==='List'||t==='Search')&&i.command)return '<pre><code>'+esc(i.command)+'</code></pre>';
  if(typeof i==='string')return '<pre><code>'+esc(i)+'</code></pre>';
  return '<pre><code>'+esc(JSON.stringify(i,null,2))+'</code></pre>';}

let textItems={},thinkItems={},planItems={},turnDiffCards={},asstRenderT=0;
const STREAM_RENDER_MS=50;
function addUser(text,nImg){const s=atBottom();const d=document.createElement('div');d.className='msg user';
  d.innerHTML='<div class="b">'+esc(text)+'</div>'+(nImg?'<div class="imgs">🖼 '+nImg+' image'+(nImg>1?'s':'')+' attached</div>':'');
  stream.appendChild(d);scroll();}
function addAsst(text){const s=atBottom();const d=document.createElement('div');d.className='msg asst';
  d.innerHTML='<div class="b bubble">'+md(text)+'</div>';typesetMath(d,!replaying);stream.appendChild(d);if(s)scroll();}
function addThink(text){const s=atBottom();const d=document.createElement('div');d.className='think'+(showThink?'':' hide');d.dataset.t=1;
  d.textContent=text;stream.appendChild(d);if(s)scroll();}
function renderAsst(rec,final){const stick=!replaying&&(rec.stick||final)&&atBottom();
  const b=rec.el.querySelector('.bubble');b.innerHTML=md(rec.text);rec.dirty=false;rec.stick=false;
  if(final)typesetMath(rec.el,!replaying);if(final)rec.el.classList.remove('streaming');
  if(stick){scroll();if(final)requestAnimationFrame(scroll);}}
function flushAsstRenders(final){if(asstRenderT){clearTimeout(asstRenderT);asstRenderT=0;}
  Object.values(textItems).forEach(rec=>{
    if(rec.dirty||(final&&rec.el.classList.contains('streaming')))renderAsst(rec,final);});}
function scheduleAsstRender(){if(replaying||asstRenderT)return;
  asstRenderT=setTimeout(()=>{asstRenderT=0;flushAsstRenders(false);},STREAM_RENDER_MS);}
function upsertAsst(ev){const id=ev.itemId||'asst-'+Object.keys(textItems).length;
  let rec=textItems[id],s=atBottom();
  if(!rec){const d=document.createElement('div');d.className='msg asst streaming';
    d.innerHTML='<div class="b bubble"></div>';stream.appendChild(d);rec=textItems[id]={el:d,text:'',dirty:false,stick:false};}
  rec.text=ev.text!=null?ev.text:(rec.text+(ev.delta||''));rec.dirty=true;rec.stick=rec.stick||s;
  if(ev.text!=null)renderAsst(rec,true);else scheduleAsstRender();}
function upsertThink(ev){const id=ev.itemId||'think-'+Object.keys(thinkItems).length;
  let rec=thinkItems[id],s=atBottom();
  if(!rec){const d=document.createElement('div');d.className='think'+(showThink?'':' hide')+' streaming';
    d.dataset.t=1;stream.appendChild(d);rec=thinkItems[id]={el:d,text:''};}
  rec.text=ev.text!=null?ev.text:(rec.text+(ev.delta||''));rec.el.textContent=rec.text;
  if(ev.text!=null)rec.el.classList.remove('streaming');if(s)scroll();}
function planStepClass(st){st=(''+(st||'')).toLowerCase();
  if(st.includes('complete')||st==='done')return ['done','●'];
  if(st.includes('progress')||st==='active')return ['active','◐'];
  return ['todo','○'];}
function planHTML(ev,rec){let h='<div class="ph">☑ Plan</div>';
  if(ev.explanation)h+='<div class="pex">'+esc(ev.explanation)+'</div>';
  if(Array.isArray(ev.plan)&&ev.plan.length){h+='<div class="psteps">';
    ev.plan.forEach(x=>{const pc=planStepClass(x.status),step=x.step||x.text||'';
      h+='<div class="pst '+pc[0]+'"><span class="pi">'+pc[1]+'</span><span>'+esc(step)+'</span></div>';});
    h+='</div>';}
  else{const text=(ev.text!=null?ev.text:(rec&&rec.text)||'')+(ev.delta||'');
    h+='<div class="ptext">'+esc(text||'planning…')+'</div>';}
  return h;}
function upsertPlan(ev){const id='plan-current';
  let rec=planItems[id],s=atBottom(),host=$('#planDock');
  if(!rec)rec=planItems[id]={text:'',explanation:'',plan:null};
  if(!host)return;
  if(ev.text!=null)rec.text=ev.text;else if(ev.delta)rec.text+=ev.delta;
  if(ev.explanation!=null)rec.explanation=ev.explanation;
  if(Array.isArray(ev.plan))rec.plan=ev.plan;
  const stable={text:rec.text,explanation:rec.explanation,plan:rec.plan};
  host.hidden=false;
  host.innerHTML='<div class="plan '+((ev.plan||ev.text!=null)?'':'streaming')+'">'+planHTML(stable,rec)+'</div>';
  if(s)scroll();}
function addGoal(ev){const g=ev.goal;if(!g){addNotice('goal cleared');return;}
  const used=g.tokensUsed!=null?(' · '+fmtTok(g.tokensUsed)+(g.tokenBudget?('/'+fmtTok(g.tokenBudget)):'')+' tokens'):'';
  addNotice('goal '+(g.status||'updated')+': '+(g.objective||'')+used);}
function addNotice(t){const d=document.createElement('div');d.className='notice';d.textContent=t;stream.appendChild(d);}
function addStatus(ev){const s=atBottom(),st=ev.status||{},se=st.session||{},sv=st.service||{},g=st.git||{};
  const ctx=st.context||{},u=st.usage||{};
  const state=se.ended?'ended':(se.compacting?'compacting':(se.busy?'busy '+fmtSecs((se.turn_age||0)*1000):'ready'));
  let ctxText=ctx.percentage!=null?(ctx.percentage+'% · '+fmtTok(ctx.totalTokens)+' / '+fmtTok(ctx.maxTokens)):'unknown';
  const win=(o)=>o&&o.utilization!=null?Math.round(o.utilization)+'%':'unknown';
  const usageText='5h '+win(u.five_hour)+' · weekly '+win(u.seven_day);
  const gitText=g.ok?(g.branch+'@'+(g.head||'?')+' · '+(g.dirty?('dirty '+g.file_count+' files'):'clean')):(g.error||'unknown');
  const rows=[
    ['state',state],['cwd',se.cwd||cwd||''],['thread',se.thread_id||'not started'],
    ['model',(se.display_model||se.model||'default')+(se.effort?(' · '+se.effort):'')],
    ['approval',(se.mode||'')+' · '+(se.approval_policy||'')+' · '+(se.sandbox||'')],
    ['queue/viewers',(se.queued||0)+' queued · '+(se.viewers||0)+' viewer'+((se.viewers||0)===1?'':'s')],
    ['context',ctxText],['limits',usageText],
    ['service',(sv.bind||'')+':'+(sv.port||'')+' · auth '+(sv.auth?'on':'off')+' · recap '+(sv.recap?'on':'off')+
      (sv.configured_context_window?(' · config ctx '+fmtTok(sv.configured_context_window)):'')],
    ['git',gitText],
  ];
  const gf=g.files||[],files=gf.map(f=>(f.status||'?')+' '+(f.path||'')).join(' · ');
  const d=document.createElement('div');d.className='localstatus';
  d.innerHTML='<div class="sh">/status</div><div class="sg">'+rows.map(([k,v])=>
    '<div><span class="sk">'+esc(k)+'</span><span class="sv">'+esc(v||'')+'</span></div>').join('')+
    '</div>'+(files?'<div class="sf">'+esc(files)+(g.file_count>gf.length?' · …':'')+'</div>':'');
  stream.appendChild(d);if(s)scroll();}
function addRecap(t){const s=atBottom();const d=document.createElement('div');d.className='recap';
  d.innerHTML='<span class="rk">※ recap:</span> <span class="rt"></span>';
  d.querySelector('.rt').textContent=t;stream.appendChild(d);if(s)scroll();}
const AGENT_DONE=new Set(['completed','interrupted','errored','shutdown','notfound']);
function agentRows(){return Object.values(subagents).filter(a=>a&&a.threadId).sort((a,b)=>{
  const aa=agentActive(a),bb=agentActive(b);
  if(aa!==bb)return aa?-1:1;
  return (b.updatedAt||0)-(a.updatedAt||0);});}
function agentState(a){return (''+(a&&a.state||'running')).toLowerCase();}
function agentActive(a){return a&&a.active!==false&&!AGENT_DONE.has(agentState(a));}
function agentDot(a){const st=agentState(a);
  if(st==='completed')return 'ok';
  if(st==='errored'||st==='notfound')return 'bad';
  return agentActive(a)?'run':'';}
function agentMeta(a){const bits=[];
  if(a.state)bits.push(a.state);
  if(a.model)bits.push(a.model);
  if(a.reasoningEffort)bits.push(a.reasoningEffort);
  if(a.updatedAt)bits.push(reltime(a.updatedAt));
  return bits.join(' · ');}
function mergeSubagents(list,reset){
  if(reset){subagents={};subagentCurrent='';}
  (list||[]).forEach(a=>{if(a&&a.threadId)subagents[a.threadId]=Object.assign({},subagents[a.threadId]||{},a);});
  const rows=agentRows();
  if(subagentCurrent&&!subagents[subagentCurrent])subagentCurrent='';
  if(!subagentCurrent&&rows.length)subagentCurrent=rows[0].threadId;
  renderSubagentBadge();renderSubagents();}
function renderSubagentBadge(){const btn=$('#agentbtn');if(!btn)return;
  const rows=agentRows(),active=rows.filter(agentActive).length;
  btn.classList.toggle('on',rows.length>0);btn.classList.toggle('busy',active>0);
  $('#agentN').textContent=active||rows.length||0;
  btn.title=rows.length?('subagents: '+active+' active / '+rows.length+' total'):'subagents';
  if(!rows.length&&agentPanelOpen())closeSubagents();}
function agentPanelOpen(){const p=$('#agentPanel');return p&&p.classList.contains('open');}
function openSubagents(threadId){if(threadId)subagentCurrent=threadId;
  const p=$('#agentPanel');if(!p)return;
  $('#drawer').classList.remove('open');
  p.classList.add('open');renderSubagents();
  if(subagentCurrent)loadSubagentThread(subagentCurrent);}
function closeSubagents(){const p=$('#agentPanel');if(p)p.classList.remove('open');}
function renderSubagents(){
  const list=$('#agentList'),detail=$('#agentDetail'),meta=$('#agentMeta');if(!list||!detail)return;
  const rows=agentRows();if(!subagentCurrent&&rows.length)subagentCurrent=rows[0].threadId;
  const active=rows.filter(agentActive),done=rows.filter(a=>!agentActive(a));
  if(meta)meta.textContent=rows.length?(active.length+' active · '+rows.length+' total'):'';
  if(!rows.length){list.innerHTML='<div class="empty">no subagents yet</div>';detail.innerHTML='<div class="empty">select a subagent</div>';return;}
  const section=(title,items)=>items.length?('<div class="agsec">'+title+'</div>'+
    items.map(a=>agentRowHTML(a)).join('')):'';
  list.innerHTML=section('active',active)+section('done',done);
  list.querySelectorAll('.agrow').forEach(el=>el.onclick=()=>{subagentCurrent=el.dataset.tid;renderSubagents();loadSubagentThread(subagentCurrent);});
}
function agentRowHTML(a){
  const sub=a.message||a.prompt||a.title||a.threadId||'';
  return '<div class="agrow '+(subagentCurrent===a.threadId?'on ':'')+(agentActive(a)?'':'done')+'" data-tid="'+escAttr(a.threadId)+'">'+
    '<span class="agdot '+agentDot(a)+'"></span><div class="agm"><div class="agn">'+esc(a.label||'subagent')+'</div>'+
    '<div class="ags">'+esc(agentMeta(a)+(sub?(' · '+sub):''))+'</div></div></div>';
}
function loadSubagentThread(threadId){
  if(!threadId)return;
  const d=$('#agentDetail');if(d)d.innerHTML='<div class="empty">loading subagent thread...</div>';
  wsSend({type:'subagent_read',threadId:threadId,limit:160});}
function renderSubagentThread(m){
  if(!m||!m.threadId)return;
  if(m.subagent)mergeSubagents([m.subagent]);
  if(subagentCurrent&&m.threadId!==subagentCurrent)return;
  subagentCurrent=m.threadId;renderSubagents();
  const d=$('#agentDetail');if(!d)return;
  const a=subagents[m.threadId]||m.subagent||{},thread=m.thread||{},msgs=m.messages||[];
  const title=a.label||thread.name||thread.preview||'subagent';
  const bits=[a.state||thread.status,a.model,a.reasoningEffort,thread.cwd||a.cwd].filter(Boolean).join(' · ');
  let h='<div class="aghead"><div class="agt">'+esc(title)+'</div><div class="agsub">'+esc(bits||m.threadId)+'</div></div>';
  if(!m.ok&&m.error&&!msgs.length){d.innerHTML=h+'<div class="empty">'+esc(m.error)+'</div>';return;}
  if(m.error)h+='<div class="errline">'+esc(m.error)+'</div>';
  if(!msgs.length){d.innerHTML=h+'<div class="empty">no visible subagent messages yet</div>';return;}
  h+='<div class="agmsgs">'+msgs.map(agentMsgHTML).join('')+'</div>';
  d.innerHTML=h;typesetMath(d);d.scrollTop=d.scrollHeight;
}
function agentMsgHTML(x){
  const role=(x.role||'tool').toLowerCase(),kind=x.kind?(' · '+x.kind):'',status=x.status?(' · '+x.status):'';
  const label=(role==='assistant'?'assistant':role)+kind+status;
  const body=(role==='tool')
    ? '<pre><code>'+esc(x.txt||'')+'</code></pre>'
    : md(x.txt||'');
  return '<div class="agmsg '+escAttr(role)+'"><div class="agr">'+esc(label)+'</div><div class="agtxt">'+body+'</div></div>';}
function addSubagentMarker(ev){
  const a=ev.subagent||{};if(a.threadId)mergeSubagents([a]);
  const s=atBottom(),d=document.createElement('div');d.className='agmark';
  d.innerHTML='<span>Agents</span><span>'+esc(a.label||'subagent')+'</span><span class="mut">'+
    esc(ev.activity||a.state||'updated')+' — open</span>';
  d.onclick=()=>openSubagents(a.threadId||'');
  stream.appendChild(d);if(s)scroll();}
/* turn-complete footer line — ✻ {past verb} for {N}s · {YYYY-MM-DD HH:MM:SS} UTC (server-stamped) */
function utcStamp(sec){const d=new Date(sec*1000),p=n=>String(n).padStart(2,'0');
  return d.getUTCFullYear()+'-'+p(d.getUTCMonth()+1)+'-'+p(d.getUTCDate())+' '+
         p(d.getUTCHours())+':'+p(d.getUTCMinutes())+':'+p(d.getUTCSeconds())+' UTC';}
function addDone(word,durMs,atSec){const s=atBottom();const d=document.createElement('div');d.className='doneline';
  d.innerHTML='<span class="dg">✻</span> <span class="dw"></span>';
  let t=word+(durMs>0?(' for '+fmtSecs(durMs)):'');
  if(atSec)t+=' · '+utcStamp(atSec);
  d.querySelector('.dw').textContent=t;
  stream.appendChild(d);if(s)scroll();}
function addErr(t){const d=document.createElement('div');d.className='errline';d.textContent=t;stream.appendChild(d);if(atBottom())scroll();}
function addTool(ev){const s=atBottom();const c=document.createElement('div');c.className='tool';
  const cn=counts(ev);const cnt=cn?('<span class="a">+'+cn.a+'</span> <span class="d">−'+cn.d+'</span>'):'';
  c.innerHTML='<div class="th"><span class="ico">'+(ICON[ev.tool]||'🔧')+'</span><span class="tn">'+esc(ev.tool)+'</span>'+
    '<span class="tp">'+esc(primaryArg(ev.input))+'</span><span class="cnt">'+cnt+'</span><span class="eye"><svg class="e-shut" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 11c3 4 7 6 10 6s7-2 10-6"/><line x1="5.5" y1="15.5" x2="4.3" y2="18"/><line x1="12" y1="17.5" x2="12" y2="20"/><line x1="18.5" y1="15.5" x2="19.7" y2="18"/></svg><svg class="e-open" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></span></div>'+
    '<div class="tb"><div class="res"></div></div>';
  c._ev=ev;   /* lazy: build the (maybe large) body only on first expand */
  c.querySelector('.th').onclick=()=>{const open=c.classList.toggle('open');
    if(open&&!c._bodyDone){c._bodyDone=1;c.querySelector('.res').insertAdjacentHTML('beforebegin',toolBody(c._ev));}};
  stream.appendChild(c);if(ev.toolId)tools[ev.toolId]=c;if(s)scroll();}
function resultBits(ev){const bits=[];if(ev.status)bits.push(ev.status);
  if(ev.exitCode!==undefined&&ev.exitCode!==null)bits.push('exit '+ev.exitCode);
  if(ev.durationMs!=null)bits.push(fmtSecs(ev.durationMs));return bits;}
function resultMeta(ev){const bits=resultBits(ev);return bits.length?'<span class="resmeta">'+esc(bits.join(' · '))+'</span>':'';}
function clipOut(t){t=t||'';return t.length>2200?t.slice(0,2200)+'\n…':t;}
function setToolOutput(c,label,text,isError,ev){const r=c&&c.querySelector('.res');if(!r)return;
  r.innerHTML='<div class="reslabel">'+label+' ⤵'+(ev?resultMeta(ev):'')+'</div><pre><code>'+
    esc(clipOut((text||'').trim()))+'</code></pre>';if(isError)c.classList.add('err');}
function updateToolHeader(c,ev){if(!c||!ev)return;
  const old=c._ev||{},merged=Object.assign({},old,ev);c._ev=merged;
  const ico=c.querySelector('.ico');if(ico&&merged.tool)ico.textContent=ICON[merged.tool]||'🔧';
  const tn=c.querySelector('.tn');if(tn&&merged.tool)tn.textContent=merged.tool;
  const tp=c.querySelector('.tp,.ef');if(tp)tp.textContent=primaryArg(merged.input)||merged.tool||'';
  const cnt=c.querySelector('.cnt'),cn=counts(merged);if(cnt)cnt.innerHTML=cn?('<span class="a">+'+cn.a+'</span> <span class="d">−'+cn.d+'</span>'):'';}
function updateTool(ev){let c=tools[ev.toolId];if(!c){
    if(EDIT_TOOLS.has(ev.tool)){addEditCard(ev);addMarker(ev);}else addTool(ev);return;}
  updateToolHeader(c,ev);
  if(c.classList.contains('ecard')){
    const shouldBuild=c._bodyDone||(drawerOpen()&&!gitTab());
    if(shouldBuild){c._bodyDone=1;c.querySelector('.ed').innerHTML=toolBody(c._ev);}
  }}
function addResult(ev){const c=tools[ev.toolId];if(!c)return;c.classList.add('done');if(ev.isError)c.classList.add('err');
  const b=((ev.content!=null?ev.content:c._output)||'').trim();
  if(b||ev.isError||c._output)setToolOutput(c,ev.isError?'error':'output',b,ev.isError,ev);
  else if(resultBits(ev).length){const r=c.querySelector('.res');
    if(r)r.innerHTML='<div class="reslabel">done'+resultMeta(ev)+'</div>';}}
function appendToolDelta(ev){let c=tools[ev.toolId];if(!c){addTool({kind:'tool_use',tool:'Bash',input:{command:ev.toolId||'process output'},toolId:ev.toolId});c=tools[ev.toolId];}
  if(!c)return;c._output=(c._output||'')+(ev.delta||'');
  if(c._output.length>12000)c._output=c._output.slice(c._output.length-12000);
  setToolOutput(c,ev.stream==='stderr'?'stderr':'output',c._output,false,null);
  if(ev.capReached)addToolProgress({toolId:ev.toolId,text:'output cap reached'});}
function addToolProgress(ev){const c=tools[ev.toolId];if(!c||!ev.text)return;
  let p=c.querySelector('.progress');
  if(!p){p=document.createElement('div');p.className='progress';
    const host=c.querySelector('.tb')||c.querySelector('.ed')||c;const res=host.querySelector('.res');
    host.insertBefore(p,res||host.firstChild);}
  p.textContent+=(p.textContent?'\n':'')+ev.text;}
function addTurnDiff(ev){if(!ev.diff)return;const id=ev.turnId||'turn-current';let c=turnDiffCards[id];
  if(!c){if(editCount===0)$('#edits').innerHTML='';
    c=document.createElement('div');c.className='ecard turndiff';
    c.innerHTML='<div class="eh"><span>✏️</span><span class="ef">Turn diff</span></div><div class="ed"></div><div class="res"></div>';
    $('#edits').appendChild(c);turnDiffCards[id]=c;editCount++;updateEditBadge();}
  c.querySelector('.ed').innerHTML=diffHtml(ev.diff);
  if(!replaying){const ed=$('#edits');ed.scrollTop=ed.scrollHeight;}}
function applySettings(ev){if(ev.display_model||ev.model){activeModel=(ev.model&&ev.model!=='default')?ev.model:(ev.display_model||activeModel);setResolvedModel(ev.display_model||ev.model);}
  if(ev.effort)setEffortPill(ev.effort);if(ev.cwd){cwd=ev.cwd;bindProject(cwd);}}
function applyThreadStatus(ev){const st=(''+(ev.status||'')).toLowerCase();
  if(st==='active')setBusy(true);else if(st==='idle'||st==='notloaded')setBusy(false);}
function addModelReroute(ev){if(ev.to){activeModel=ev.to;setResolvedModel(ev.to);}
  addNotice('model rerouted'+(ev.from?(' from '+ev.from):'')+(ev.to?(' to '+ev.to):'')+(ev.reason?(' · '+ev.reason):''));}

function statset(t){const el=$('#thinking');if(!el)return;
  const w=el.querySelector('.word');if(w)w.textContent=t;
  if(!running){const m=el.querySelector('.meta');if(m)m.textContent='';}}
/* CLI-style ↑input / ↓output token counts for the pill (reuses the project fmtTok) */
function tokStr(){return '↑'+fmtTok(tokUp)+' ↓'+fmtTok(tokOut);}
function bindProject(p){if(!p)return;const sel=$('#project');let ok=false;
  for(const o of sel.options){if(o.value===p){ok=true;break;}}
  if(!ok){const o=document.createElement('option');o.value=p;o.textContent='● '+p.split('/').slice(-2).join('/');sel.insertBefore(o,sel.firstChild);}
  sel.value=p;}
function setBusy(b,wordSeed,elapsedMs){running=b;$('#dot').className='dot '+(b?'busy':(ready?'on':''));
  setSessionTabBusy(sid,b);
  $('#thinking').classList.toggle('busy',b);
  if(!b&&ready)statset('ready');      /* busy: startThinking owns the word + timer */
  ta.disabled=!ready;
  sendBtn.disabled=!ready;            /* send stays available while busy → queues */
  $('#stop').style.display=b?'':'none';   /* interrupt button only while busy */
  sendBtn.style.display='';               /* send always visible */
  if(b)startThinking(wordSeed,elapsedMs);else stopThinking();}

/* in-chat "thinking" indicator — animated glyph + cycling word + elapsed timer */
/* spinner verbs — the full Claude Code CLI set (187 present participles, incl. the
   "Clauding" easter egg). A verb is picked per agentic step (see setWord). */
const THINK_WORDS=['Accomplishing','Actioning','Actualizing','Architecting','Baking','Beaming','Beboppin\'','Befuddling','Billowing','Blanching','Bloviating','Boogieing','Boondoggling','Booping','Bootstrapping','Brewing','Bunning','Burrowing','Calculating','Canoodling','Caramelizing','Cascading','Catapulting','Cerebrating','Channeling','Channelling','Choreographing','Churning','Clauding','Coalescing','Cogitating','Combobulating','Composing','Computing','Concocting','Considering','Contemplating','Cooking','Crafting','Creating','Crunching','Crystallizing','Cultivating','Deciphering','Deliberating','Determining','Dilly-dallying','Discombobulating','Doing','Doodling','Drizzling','Ebbing','Effecting','Elucidating','Embellishing','Enchanting','Envisioning','Evaporating','Fermenting','Fiddle-faddling','Finagling','Flambéing','Flibbertigibbeting','Flowing','Flummoxing','Fluttering','Forging','Forming','Frolicking','Frosting','Gallivanting','Galloping','Garnishing','Generating','Gesticulating','Germinating','Gitifying','Grooving','Gusting','Harmonizing','Hashing','Hatching','Herding','Honking','Hullaballooing','Hyperspacing','Ideating','Imagining','Improvising','Incubating','Inferring','Infusing','Ionizing','Jitterbugging','Julienning','Kneading','Leavening','Levitating','Lollygagging','Manifesting','Marinating','Meandering','Metamorphosing','Misting','Moonwalking','Moseying','Mulling','Mustering','Musing','Nebulizing','Nesting','Newspapering','Noodling','Nucleating','Orbiting','Orchestrating','Osmosing','Perambulating','Percolating','Perusing','Philosophising','Photosynthesizing','Pollinating','Pondering','Pontificating','Pouncing','Precipitating','Prestidigitating','Processing','Proofing','Propagating','Puttering','Puzzling','Quantumizing','Razzle-dazzling','Razzmatazzing','Recombobulating','Reticulating','Roosting','Ruminating','Sautéing','Scampering','Schlepping','Scurrying','Seasoning','Shenaniganing','Shimmying','Simmering','Skedaddling','Sketching','Slithering','Smooshing','Sock-hopping','Spelunking','Spinning','Sprouting','Stewing','Sublimating','Swirling','Swooping','Symbioting','Synthesizing','Tempering','Thinking','Thundering','Tinkering','Tomfoolering','Topsy-turvying','Transfiguring','Transmuting','Twisting','Undulating','Unfurling','Unravelling','Vibing','Waddling','Wandering','Warping','Whatchamacalliting','Whirlpooling','Whirring','Whisking','Wibbling','Working','Wrangling','Zesting','Zigzagging'];
const THINK_GLYPHS=['✶','✷','✸','✹','✺','✹','✸','✷'];
const DREAM_GLYPHS=['Zzz','zZz','zzZ','ZzZ'];   /* compacting: a slow breathing Zzz wave */
/* compacting meta: crush-style flowing gradient over scrambling hex (cf. charmbracelet/crush
   internal/ui/anim/anim.go) — runes à la crush (hex+symbols, minus <>&"' for innerHTML safety);
   each cell re-scrambles every frame while a warm<->cool color wave scrolls across the row */
const MATRIX_CHARS='0123456789abcdefABCDEF~!@#$%^*()+=_-?/|';
const MTX_PERIOD=12, MTX_FLOW=0.06;            /* ~1.3 colour sweeps across the row, scrolling ~1cyc/1.1s */
let gradA=[224,192,128], gradB=[79,193,255];   /* [--tool,--acc] RGB, refreshed from the live theme per compaction */
let mtxLite=false;                              /* light theme: darken the wave + drop the glow so the chars stay legible */
let mtxTheme=null;                              /* last theme the palette was built for → re-read on live theme-swap */
function cssRGB(name){const v=getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const m=/^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(v);
  return m?[parseInt(m[1],16),parseInt(m[2],16),parseInt(m[3],16)]:[224,192,128];}
function mtxLum(c){return (0.299*c[0]+0.587*c[1]+0.114*c[2])/255;}   /* perceived luminance 0..1 */
function mtxAdjust(c,light){    /* light bg only: scale toward black to a max luminance (hue kept); dark path untouched */
  if(!light)return c;
  const L=mtxLum(c),MAXL=0.34;
  if(L<=MAXL)return c;
  const k=MAXL/L;return [Math.round(c[0]*k),Math.round(c[1]*k),Math.round(c[2]*k)];}
function mtxRefresh(){    /* re-read palette from the live theme; cheap no-op until the theme attr actually changes (hot-swap) */
  const th=document.documentElement.getAttribute('data-theme')||'dark';
  if(th===mtxTheme)return;
  mtxTheme=th;
  mtxLite=mtxLum(cssRGB('--bg'))>0.5;
  gradA=mtxAdjust(cssRGB('--tool'),mtxLite);
  gradB=mtxAdjust(cssRGB('--acc'),mtxLite);}
function matrixHTML(n,phase){let s='';for(let i=0;i<n;i++){
    const ch=MATRIX_CHARS[Math.floor(Math.random()*MATRIX_CHARS.length)];
    const t=0.5-0.5*Math.cos(2*Math.PI*(i/MTX_PERIOD+phase*MTX_FLOW)),
          r=Math.round(gradA[0]+(gradB[0]-gradA[0])*t),
          g=Math.round(gradA[1]+(gradB[1]-gradA[1])*t),
          b=Math.round(gradA[2]+(gradB[2]-gradA[2])*t);
    s+='<span style="color:rgb('+r+','+g+','+b+')">'+ch+'</span>';}
  return s;}
let thinkTimer=0,thinkStart=0,thinkGi=0,thinkWi=0,lastWordSeed=null;
/* map a server seed → spinner verb (so the word is stable across reattach and
   changes only when the server re-picks the seed, i.e. once per agentic step) */
function setWord(seed){const el=$('#thinking');if(!el)return;
  thinkWi=seed!=null?((seed%THINK_WORDS.length)+THINK_WORDS.length)%THINK_WORDS.length:Math.floor(Math.random()*THINK_WORDS.length);
  const w=el.querySelector('.word');if(w)w.textContent=THINK_WORDS[thinkWi];}
function startThinking(wordSeed,elapsedMs){const el=$('#thinking');if(!el)return;
  const fresh=!thinkTimer;         /* new turn, or reattaching to a running one */
  if(fresh||elapsedMs!=null)thinkStart=Date.now()-(elapsedMs||0);
  el.classList.toggle('compacting',compacting);
  if(compacting){thinkGi=0;el.querySelector('.word').textContent='Compacting';el.querySelector('.glyph').textContent=DREAM_GLYPHS[0];}
  else if(fresh||wordSeed!=null){  /* server-provided seed keeps the word stable across reattach */
    lastWordSeed=wordSeed;setWord(wordSeed);
  }
  if(fresh&&atBottom())scroll();
  clearInterval(thinkTimer);
  let frame=0;
  const tick=compacting?65:130;    /* compacting shimmer wants ~15fps; the normal spinner stays 130ms */
  if(compacting)mtxRefresh();    /* seed palette from the live theme (re-checked each frame for hot theme-swap) */
  thinkTimer=setInterval(()=>{     /* during the turn only the glyph + timer move */
    frame++;
    const s=Math.floor((Date.now()-thinkStart)/1000);
    if(compacting){
      mtxRefresh();             /* live theme-swap: re-read the palette if the user changes theme mid-compaction */
      if(frame%6===0)thinkGi=(thinkGi+1)%DREAM_GLYPHS.length;   /* slow breathing Zzz (~390ms) */
      el.querySelector('.glyph').textContent=DREAM_GLYPHS[thinkGi];
      el.querySelector('.meta').innerHTML=s+'s · <span class="mtx'+(mtxLite?' lite':'')+'">'+matrixHTML(16,frame)+'</span>';
    }else{
      thinkGi=(thinkGi+1)%THINK_GLYPHS.length;
      el.querySelector('.glyph').textContent=THINK_GLYPHS[thinkGi];
      const tok=tokShow?(tokStr()+' · '):'';
      el.querySelector('.meta').textContent=tok+s+'s';
    }
  },tick);}
function stopThinking(){clearInterval(thinkTimer);thinkTimer=0;const el=$('#thinking');if(el)el.classList.remove('compacting');}
function fmtSecs(ms){const s=Math.max(0,Math.round(ms/1000));if(s<60)return s+'s';const m=Math.floor(s/60);return m+'m '+(s%60)+'s';}
function doInterrupt(){if(!running)return;wsSend({type:'interrupt'});addNotice('⏹ interrupt sent');}
function clearUI(){stream.innerHTML='';$('#edits').innerHTML='<div class="empty">no file changes yet</div>';
  $('#gitc').innerHTML='<div class="empty">—</div>';tools={};editCount=0;updateEditBadge();renderCtx(null);ready=false;
  if(asstRenderT){clearTimeout(asstRenderT);asstRenderT=0;}
  textItems={};thinkItems={};planItems={};turnDiffCards={};
  const pd=$('#planDock');if(pd){pd.hidden=true;pd.innerHTML='';}
  mergeSubagents([],true);
  queued={};renderQueue();stopThinking();}

/* file edits → out of chat, into the Changes drawer */
function updateEditBadge(){$('#editN').textContent=editCount;}
function addEditCard(ev){if(editCount===0)$('#edits').innerHTML='';
  const c=document.createElement('div');c.className='ecard';const cn=counts(ev);
  const cnt=cn?('<span class="cnt"><span class="a">+'+cn.a+'</span> <span class="d">−'+cn.d+'</span></span>'):'';
  c.innerHTML='<div class="eh"><span>'+(ICON[ev.tool]||'✏️')+'</span><span class="ef">'+esc(primaryArg(ev.input)||ev.tool)+'</span>'+cnt+'</div>'+
    '<div class="ed"></div><div class="res"></div>';
  c._ev=ev;   /* lazy: build the diff only when the Changes drawer is actually shown */
  if(drawerOpen()&&!gitTab()){c._bodyDone=1;c.querySelector('.ed').innerHTML=toolBody(ev);}
  $('#edits').appendChild(c);if(ev.toolId)tools[ev.toolId]=c;editCount++;updateEditBadge();
  if(!replaying){const ed=$('#edits');ed.scrollTop=ed.scrollHeight;}}
function buildPendingEdits(){$('#edits').querySelectorAll('.ecard').forEach(c=>{
  if(c._ev&&!c._bodyDone){c._bodyDone=1;c.querySelector('.ed').innerHTML=toolBody(c._ev);}});}
function addMarker(ev){const s=atBottom();const cn=counts(ev);const m=document.createElement('div');m.className='emark';
  m.innerHTML=(ICON[ev.tool]||'✏️')+'<span>'+esc(primaryArg(ev.input)||ev.tool)+'</span>'+
    (cn?'<span><span class="a">+'+cn.a+'</span> <span class="d">−'+cn.d+'</span></span>':'')+'<span class="mut">— see Changes</span>';
  m.onclick=()=>{openDrawer('edits');focusEdit(ev.toolId);};
  stream.appendChild(m);if(s)scroll();}

function addApproval(ev){const c=document.createElement('div');c.className='approval';c.dataset.aid=ev.aid;
  c.innerHTML='<div class="ah">🔐 Approve <b>'+esc(ev.tool)+'</b> <span class="tp">'+esc(primaryArg(ev.input)||'')+'</span></div>'+
    '<div class="abody">'+toolBody(ev)+'</div>'+
    '<div class="abtns"><button class="appr">✓ Approve</button>'+
    (ev.always?'<button class="apprall" title="approve and don\'t ask again this session">✓✓ Always</button>':'')+
    '<button class="deny">✕ Deny</button></div>';
  c.querySelector('.appr').onclick=()=>decide(ev.aid,true,false);
  const aa=c.querySelector('.apprall');if(aa)aa.onclick=()=>decide(ev.aid,true,true);
  c.querySelector('.deny').onclick=()=>decide(ev.aid,false,false);
  stream.appendChild(c);scroll();}
function decide(aid,allow,always){wsSend({type:'approve',aid:aid,allow:allow,always:!!always});resolveApprovalCard(aid,allow,always);}
function resolveApprovalCard(aid,allow,always){const c=stream.querySelector('.approval[data-aid="'+aid+'"]');
  if(c&&!c.classList.contains('done')){c.classList.add('done');
    const bt=c.querySelector('.abtns');if(bt)bt.innerHTML='<span class="'+(allow?'ok':'no')+'">'+(allow?('✓ Approved'+(always?" · won't ask again this session":'')):'✕ Denied')+'</span>';}}
function qval(bl){const other=bl.querySelector('.qother').value.trim();
  if(other)return other;
  return [...bl.querySelectorAll('.qopt.sel')].map(x=>x.dataset.label).join(', ');}
function addQuestion(ev){const c=document.createElement('div');c.className='question';c.dataset.aid=ev.aid;
  const qs=ev.questions||[];let h='<div class="qh">❓ <b>Question'+(qs.length>1?'s':'')+'</b></div>';
  qs.forEach((q,qi)=>{h+='<div class="qblk" data-qi="'+qi+'"'+(q.multiSelect?' data-multi="1"':'')+'>'+
    '<div class="qtext">'+(q.header?'<span class="chip">'+esc(q.header)+'</span>':'')+esc(q.question||('Question '+(qi+1)))+'</div>'+
    '<div class="qopts">';
    (q.options||[]).forEach(o=>{h+='<button class="qopt" data-label="'+esc(o.label)+'">'+esc(o.label)+
      (o.description?'<span class="od">'+esc(o.description)+'</span>':'')+'</button>';});
    h+='</div><input class="qother" placeholder="Other… custom answer'+(q.multiSelect?' (comma-separated)':'')+'"></div>';});
  h+='<div class="qbtns"><button class="qsub" disabled>Submit</button></div>';
  c.innerHTML=h;const sub=c.querySelector('.qsub');
  function refresh(){let ok=qs.length>0;c.querySelectorAll('.qblk').forEach(bl=>{if(!qval(bl))ok=false;});sub.disabled=!ok;}
  c.querySelectorAll('.qblk').forEach(bl=>{const multi=bl.dataset.multi==='1';
    bl.querySelectorAll('.qopt').forEach(b=>{b.onclick=()=>{
      if(multi)b.classList.toggle('sel');
      else bl.querySelectorAll('.qopt').forEach(x=>x.classList.toggle('sel',x===b));
      refresh();};});
    bl.querySelector('.qother').oninput=refresh;});
  sub.onclick=()=>{const picks=qs.map((q,qi)=>qval(c.querySelector('.qblk[data-qi="'+qi+'"]')));
    wsSend({type:'answer',aid:ev.aid,answers:picks});resolveQuestionCard(ev.aid,picks);};
  stream.appendChild(c);scroll();}
function resolveQuestionCard(aid,ans){const c=stream.querySelector('.question[data-aid="'+aid+'"]');
  if(!c||c.classList.contains('done'))return;c.classList.add('done');
  let vals=null;if(Array.isArray(ans))vals=ans.filter(Boolean);
  else if(ans&&typeof ans==='object')vals=Object.values(ans);
  const bt=c.querySelector('.qbtns');const has=vals&&vals.length;
  if(bt)bt.insertAdjacentHTML('afterend','<div class="qdone">'+(has?'✓ '+vals.map(esc).join(' · '):'✕ dismissed')+'</div>');}
function fmtCompacted(ev){let s='🗜 context compacted';
  if(ev.pre!=null||ev.post!=null)s+=' · '+fmtTok(ev.pre)+' → '+fmtTok(ev.post)+' tokens';
  if(ev.trigger)s+=' · '+(ev.trigger==='auto'?'auto':'manual');
  if(ev.ms)s+=' · '+Math.round(ev.ms/1000)+'s';
  return s;}
function route(ev){const seq=Number(ev&&ev._seq||0),tab=sessionTabById(sid);
  if(seq&&tab){tab.lastSeq=Math.max(Number(tab.lastSeq||0),seq);tab.serverSeq=Math.max(Number(tab.serverSeq||0),seq);}
  /* if activity resumes while we think we're idle (e.g. the CLI ran an injected
     queued message as its own turn), step back into the busy state */
  if(!running&&['assistant_text','assistant_delta','assistant_update','thinking','thinking_delta',
      'thinking_update','plan','plan_delta','plan_text','tool_use','tool_delta','tool_progress',
      'tool_update'].includes(ev.kind))setBusy(true);
  if(ev.kind==='user_text')addUser(ev.text,ev.images);
  else if(ev.kind==='ready'){ready=true;cwd=ev.cwd||cwd;curCC=ev.session_id||curCC;
    if(!replaying)activeModel=(ev.model&&ev.model!=='default'?ev.model:(ev.display_model||activeModel));
    const modelName=ev.display_model||ev.model||'';if(modelName)setResolvedModel(modelName);
    addNotice('● session ready · '+modelName+(ev.effort?' · '+ev.effort+' effort':'')+' · '+(ev.cwd||''));}
  else if(ev.kind==='assistant_text'){if(ev.itemId)upsertAsst({itemId:ev.itemId,text:ev.text});else addAsst(ev.text);}
  else if(ev.kind==='assistant_delta'||ev.kind==='assistant_update')upsertAsst(ev);
  else if(ev.kind==='thinking'){if(ev.itemId)upsertThink({itemId:ev.itemId,text:ev.text});else addThink(ev.text);}
  else if(ev.kind==='thinking_delta'||ev.kind==='thinking_update')upsertThink(ev);
  else if(ev.kind==='plan'||ev.kind==='plan_delta'||ev.kind==='plan_text')upsertPlan(ev);
  else if(ev.kind==='turn_diff')addTurnDiff(ev);
  else if(ev.kind==='tool_use'){if(EDIT_TOOLS.has(ev.tool)){addEditCard(ev);addMarker(ev);}else addTool(ev);}
  else if(ev.kind==='tool_update')updateTool(ev);
  else if(ev.kind==='tool_delta')appendToolDelta(ev);
  else if(ev.kind==='tool_progress')addToolProgress(ev);
  else if(ev.kind==='tool_result')addResult(ev);
  else if(ev.kind==='approval')addApproval(ev);
  else if(ev.kind==='approval_resolved')resolveApprovalCard(ev.aid,ev.allow,ev.always);
  else if(ev.kind==='question')addQuestion(ev);
  else if(ev.kind==='question_resolved')resolveQuestionCard(ev.aid,ev.answers);
  else if(ev.kind==='turn_start'){tokShow=false;setBusy(true,ev.word,0);}
  else if(ev.kind==='compacting'){compacting=true;setBusy(true,ev.word,0);}
  else if(ev.kind==='compacted'){compacting=false;addNotice(fmtCompacted(ev));if(ev.trigger!=='auto')setBusy(false);}
  else if(ev.kind==='turn_done'){flushAsstRenders(true);compacting=false;setBusy(false);if(ev.done_word)addDone(ev.done_word,ev.dur_ms||0,ev.done_at);if(drawerOpen()&&gitTab())refreshGit();}
  else if(ev.kind==='queued')addQueued(ev);
  else if(ev.kind==='dequeued'||ev.kind==='unqueued')removeQueued(ev.qid);
  else if(ev.kind==='notice')addNotice(ev.text);
  else if(ev.kind==='status')addStatus(ev);
  else if(ev.kind==='recap')addRecap(ev.text);
  else if(ev.kind==='settings')applySettings(ev);
  else if(ev.kind==='thread_status')applyThreadStatus(ev);
  else if(ev.kind==='model_rerouted')addModelReroute(ev);
  else if(ev.kind==='subagents')mergeSubagents(ev.subagents||[]);
  else if(ev.kind==='subagent_activity')addSubagentMarker(ev);
  else if(ev.kind==='safety_buffering')addNotice('model safety buffering'+(ev.fasterModel?(' · faster fallback '+ev.fasterModel):''));
  else if(ev.kind==='goal')addGoal(ev);
  else if(ev.kind==='thread_lifecycle')addNotice((ev.method||'thread lifecycle').replace('thread/','thread '));
}

/* persistent server-side session: attach / reattach / switch */
function markEnded(msg){ready=false;ta.disabled=true;sendBtn.disabled=true;$('#dot').className='dot';statset('ended');
  localStorage.removeItem(SKEY);if(msg)addNotice(msg);}
function onMsg(e){const m=JSON.parse(e.data);
  if(m.type==='started'){pendingStart=false;sid=m.id;cwd=m.cwd;bindProject(m.cwd);localStorage.setItem(SKEY,sid);loadDraft(sid);
    activeModel=(m.model&&m.model!=='default'?m.model:(m.display_model||'default'));ready=true;setBusy(false);ta.focus();setCurname(m.name||'session');setEffortPill(m.effort);renderCtx(null);statset('ready');
    const startedTab=ensureSessionTab(m,true);if(startedTab){startedTab.lastSeq=Number(m.event_seq||0);startedTab.serverSeq=startedTab.lastSeq;persistSessionTabs();}restoredViewId=m.id;
    mergeSubagents(m.subagents||[],true);
    addNotice('new session « '+(m.name||'')+' »'+(m.effort?' · '+m.effort+' effort':'')+' in '+m.cwd+' — type your first message to begin');reqList();loadTree();}
  else if(m.type==='attached'){const incremental=!!m.events_delta&&restoredViewId===m.id,stick=incremental&&atBottom();
    if(!incremental){clearUI();restoredViewId='';}pendingStart=false;sid=m.id;curCC=m.cc||null;localStorage.setItem(SKEY,sid);cwd=m.cwd;bindProject(m.cwd);
    activeModel=(m.model&&m.model!=='default'?m.model:(m.display_model||'default'));ready=!m.ended;setCurname((m.title||m.name||'session')+(m.ended?' · ended':''));setEffortPill(m.effort);renderCtx(m.ctx);renderUsage(m.usage);statset(m.ended?'ended':'ready');
    ensureSessionTab(m,true);
    replaying=true;m.events.forEach(route);replaying=false;flushAsstRenders(!m.busy);
    mergeSubagents(m.subagents||[],incremental);
    compacting=!!m.compacting;setBusy(!!m.busy,m.word,(m.turn_age||0)*1000);loadDraft(sid);
    if(m.ended){markEnded('— this session has ended (history shown · you can resume it from disk) —');}
    else{ta.disabled=false;sendBtn.disabled=false;if(!incremental)addNotice('— '+(m.resumed?'resumed':'reattached to')+' « '+(m.name||'')+' » ('+m.events.length+' events)'+(m.effort?' with '+m.effort+' effort':'')+' —');}
    const attachedTab=sessionTabById(m.id);if(attachedTab){attachedTab.lastSeq=Number(m.event_seq||attachedTab.lastSeq||0);attachedTab.serverSeq=attachedTab.lastSeq;persistSessionTabs();}
    restoredViewId=m.id;if(incremental){if(stick){scroll();requestAnimationFrame(scroll);}}else restoreSessionTabView(m.id);reqList();loadTree();}
  else if(m.type==='detached'){if(!sid){ready=false;ta.disabled=true;sendBtn.disabled=true;statset('idle');}}
  else if(m.type==='no_session'){localStorage.removeItem(SKEY);sid=null;ready=false;activeModel='default';setBusy(false);setCurname('');renderCtx(null);statset('idle');
    if(m.id){sessionViewCache.delete(m.id);const i=sessionTabState.findIndex(t=>t.id===m.id);if(i>=0){sessionTabState.splice(i,1);persistSessionTabs();renderSessionTabs();}}
    mergeSubagents([],true);
    addNotice('that session is no longer running — pick it under “Resume from disk”, or ＋ New.');reqList();loadTree();}
  else if(m.type==='events')m.events.forEach(route);
  else if(m.type==='stderr')addErr(m.text);
  else if(m.type==='error'){pendingStart=false;addErr('⚠ '+m.error);}
  else if(m.type==='exit'){if(!pendingStart){const t=sessionTabById(sid);if(t){t.busy=false;t.ended=true;persistSessionTabs();renderSessionTabs();}
      markEnded('session process exited (code '+m.code+')');setCurname('');mergeSubagents([],true);}reqList();loadTree();}
  else if(m.type==='ended'){dropDraft(m.id);if(m.id)closeSessionTab(m.id);else if(sid){sid=null;activeModel='default';setCurname('');markEnded('session ended');mergeSubagents([],true);}reqList();loadTree();}
  else if(m.type==='resumable_deleted'){
    if(pmanBatch){pmanBatch.done++;
      if(m.ok)pmanBatch.ok++;else pmanBatch.err.push(m.error||'?');
      if(pmanBatch.done>=pmanBatch.n){
        addNotice('🗑 '+pmanBatch.ok+'/'+pmanBatch.n+' session'+(pmanBatch.n>1?'s':'')+' moved to trash'+
          (pmanBatch.err.length?(' · '+pmanBatch.err.length+' failed: '+pmanBatch.err[0]):''));
        pmanBatch=null;reqList();loadTree();}}
    else{addNotice(m.ok?'🗑 session moved to trash':('delete failed: '+(m.error||'?')));loadTree();}}
  else if(m.type==='renamed'){if(m.ok){if(m.cc&&m.cc===curCC&&m.name)setCurname(m.name);renameSessionTabs(m.cc,m.name);addNotice('✎ renamed');reqList();loadTree();}else addNotice('rename failed');}
  else if(m.type==='sessions')renderLive(m.sessions);
  else if(m.type==='context')renderCtx(m.ctx);
  else if(m.type==='usage')renderUsage(m.usage);
  else if(m.type==='subagent_thread')renderSubagentThread(m);
  else if(m.type==='tokens'){tokUp=m.up||0;tokOut=m.out||0;tokShow=true;
    if(m.word!=null&&m.word!==lastWordSeed){lastWordSeed=m.word;if(running&&!compacting)setWord(m.word);}}
  else if(m.type==='projects'){projData=m.projects||[];renderSidebar();}
}
function openWs(cb){const proto=location.protocol==='https:'?'wss:':'ws:';
  ws=new WebSocket(proto+'//'+location.host+'/ws/chat');
  ws.onopen=()=>{clearTimeout(reconnectT);$('#dot').className='dot '+(ready?'on':'');reqList();
    if(cb)cb();else{const saved=localStorage.getItem(SKEY);if(saved&&!pendingStart){const req={type:'attach',id:saved},tab=sessionTabById(saved);
      if(restoredViewId===saved&&tab)req.after_seq=Number(tab.lastSeq||0);else statset('reattaching…');ws.send(JSON.stringify(req));}}};
  ws.onclose=()=>{$('#dot').className='dot';statset('disconnected');ta.disabled=true;sendBtn.disabled=true;
    clearTimeout(reconnectT);reconnectT=setTimeout(()=>openWs(),1800);};
  ws.onmessage=onMsg;}
function wsSend(o){if(ws&&ws.readyState===1)ws.send(JSON.stringify(o));}
function reqList(){wsSend({type:'list'});}
function reltime(ts){const s=(Date.now()/1000)-ts;if(s<60)return Math.round(s)+'s';
  if(s<3600)return Math.round(s/60)+'m';if(s<86400)return Math.round(s/3600)+'h';return Math.round(s/86400)+'d';}
function setCurname(t){$('#curname').textContent=t||'— no session —';}
function sessionTabById(id){return sessionTabState.find(t=>t.id===id)||null;}
function takeChildren(el){const f=document.createDocumentFragment();if(el)while(el.firstChild)f.appendChild(el.firstChild);return f;}
function restoreChildren(el,frag){if(!el)return;el.innerHTML='';if(frag)el.appendChild(frag);}
function stashSessionView(id){if(!id)return false;const t=sessionTabById(id);if(!t)return false;
  flushAsstRenders(false);rememberActiveTabView();
  sessionViewCache.set(id,{stream:takeChildren(stream),plan:takeChildren($('#planDock')),planHidden:$('#planDock').hidden,
    edits:takeChildren($('#edits')),git:takeChildren($('#gitc')),tools:tools,editCount:editCount,textItems:textItems,
    thinkItems:thinkItems,planItems:planItems,turnDiffCards:turnDiffCards,subagents:subagents,
    subagentCurrent:subagentCurrent,queued:queued,ctx:currentCtx,cwd:cwd,cc:curCC,model:activeModel,
    effort:curEffort,ready:ready,running:running,compacting:compacting,word:lastWordSeed,
    elapsed:running?Math.max(0,Date.now()-thinkStart):0,tokUp:tokUp,tokOut:tokOut,tokShow:tokShow,
    title:$('#curname').textContent||t.title||'session'});
  persistSessionTabs();return true;}
function restoreSessionView(id){const v=sessionViewCache.get(id);if(!v)return false;
  restoreChildren(stream,v.stream);restoreChildren($('#planDock'),v.plan);$('#planDock').hidden=!!v.planHidden;
  restoreChildren($('#edits'),v.edits);restoreChildren($('#gitc'),v.git);
  tools=v.tools||{};editCount=v.editCount||0;textItems=v.textItems||{};thinkItems=v.thinkItems||{};
  planItems=v.planItems||{};turnDiffCards=v.turnDiffCards||{};subagents=v.subagents||{};
  subagentCurrent=v.subagentCurrent||'';queued=v.queued||{};cwd=v.cwd||'';curCC=v.cc||null;
  activeModel=v.model||'default';tokUp=v.tokUp||0;tokOut=v.tokOut||0;tokShow=!!v.tokShow;
  ready=!!v.ready;compacting=!!v.compacting;bindProject(cwd);setCurname(v.title);renderCtx(v.ctx);setEffortPill(v.effort);
  updateEditBadge();renderQueue();renderSubagentBadge();renderSubagents();running=false;setBusy(!!v.running,v.word,v.elapsed);
  loadDraft(id);restoreSessionTabView(id);restoredViewId=id;return true;}
function persistSessionTabs(){try{localStorage.setItem(TABSKEY,JSON.stringify(sessionTabState));}catch(e){}}
function renderSessionTabs(){const host=$('#sessionTabs');if(!host)return;
  host.hidden=!sessionTabState.length||(!tabsValidated&&!sid);host.innerHTML='';if(host.hidden)return;
  sessionTabState.forEach((t,i)=>{const active=t.id===sid,wrap=document.createElement('div');
    wrap.className='stab'+(active?' active':'')+(t.unread?' unread':'');wrap.dataset.id=t.id;wrap.setAttribute('role','none');
    const main=document.createElement('button');main.type='button';main.className='stab-main';main.setAttribute('role','tab');
    main.setAttribute('aria-selected',active?'true':'false');main.tabIndex=active||(!sid&&i===0)?0:-1;
    main.title=t.title||'session';
    const dot=document.createElement('span');dot.className='stab-dot'+(t.busy?' busy':'')+(t.ended?' ended':'');
    const text=document.createElement('span');text.className='stab-text';
    const label=document.createElement('span');label.className='stab-label';label.textContent=t.title||'session';text.appendChild(label);
    const unread=document.createElement('span');unread.className='stab-unread';unread.setAttribute('aria-label','new activity');
    main.append(dot,text,unread);main.onclick=()=>switchSession(t.id);
    const close=document.createElement('button');close.type='button';close.className='stab-close';close.textContent='×';
    close.title='Close tab';close.setAttribute('aria-label','Close '+(t.title||'session')+' tab');
    close.onclick=ev=>{ev.stopPropagation();closeSessionTab(t.id);};wrap.append(main,close);host.appendChild(wrap);});
  const current=host.querySelector('.stab.active');if(current)current.scrollIntoView({block:'nearest',inline:'nearest'});}
function ensureSessionTab(s,active){if(!s||!s.id)return null;let t=sessionTabById(s.id);
  if(!t){t={id:s.id,cc:s.cc||null,title:s.title||s.name||'session',cwd:s.cwd||'',
    busy:!!s.busy,ended:!!s.ended,activity:Number(s.activity||0),serverSeq:Number(s.event_seq||0),lastSeq:0,
    unread:false,visited:false,scrollTop:0,atBottom:true};sessionTabState.push(t);}
  const prev=Number(t.activity||0),next=Number(s.activity||prev||0);
  const serverSeq=Number(s.event_seq||t.serverSeq||0);if(!active&&s.id!==sid&&serverSeq>Number(t.lastSeq||0))t.unread=true;
  t.cc=s.cc||t.cc||null;t.title=s.title||s.name||t.title||'session';t.cwd=s.cwd||t.cwd||'';
  if(s.busy!=null)t.busy=!!s.busy;if(s.ended!=null)t.ended=!!s.ended;t.activity=next;t.serverSeq=serverSeq;
  if(active){t.unread=false;t.seenActivity=next;}
  tabsValidated=true;persistSessionTabs();renderSessionTabs();return t;}
function syncSessionTabs(list){const by=new Map((list||[]).map(s=>[s.id,s]));
  sessionTabState.forEach(t=>{if(!by.has(t.id))sessionViewCache.delete(t.id);});sessionTabState=sessionTabState.filter(t=>by.has(t.id));
  sessionTabState.forEach(t=>{const s=by.get(t.id),prev=Number(t.activity||0),next=Number(s.activity||prev||0),serverSeq=Number(s.event_seq||t.serverSeq||0);
    if(t.id!==sid&&serverSeq>Number(t.lastSeq||0))t.unread=true;t.cc=s.cc||t.cc;t.title=s.title||s.name||t.title;t.cwd=s.cwd||t.cwd;
    t.busy=!!s.busy;t.ended=!!s.ended;t.activity=next;t.serverSeq=serverSeq;if(t.id===sid){t.unread=false;t.seenActivity=next;}});
  tabsValidated=true;persistSessionTabs();renderSessionTabs();}
function rememberActiveTabView(){const t=sessionTabById(sid),chat=$('#chat');if(!t||!chat)return;
  t.scrollTop=chat.scrollTop;t.atBottom=chat.scrollHeight-chat.scrollTop-chat.clientHeight<140;t.visited=true;persistSessionTabs();}
function restoreSessionTabView(id){const t=sessionTabById(id),chat=$('#chat');
  if(t&&t.visited&&!t.atBottom){const y=Math.max(0,Number(t.scrollTop||0));chat.scrollTop=Math.min(y,Math.max(0,chat.scrollHeight-chat.clientHeight));
    requestAnimationFrame(()=>{chat.scrollTop=Math.min(y,Math.max(0,chat.scrollHeight-chat.clientHeight));});}
  else{scroll();requestAnimationFrame(scroll);}}
function setSessionTabBusy(id,busy){const t=sessionTabById(id);if(!t||t.busy===!!busy)return;t.busy=!!busy;persistSessionTabs();renderSessionTabs();}
function renameSessionTabs(cc,title){if(!cc||!title)return;let changed=false;sessionTabState.forEach(t=>{if(t.cc===cc){t.title=title;
    const view=sessionViewCache.get(t.id);if(view)view.title=title;changed=true;}});
  if(changed){persistSessionTabs();renderSessionTabs();}}
function closeSessionTab(id){const idx=sessionTabState.findIndex(t=>t.id===id);if(idx<0)return;const active=id===sid;
  if(active){saveDraft();rememberActiveTabView();}sessionViewCache.delete(id);sessionTabState.splice(idx,1);persistSessionTabs();renderSessionTabs();if(!active)return;
  const next=sessionTabState[Math.min(idx,sessionTabState.length-1)];if(next){switchSession(next.id);return;}
  clearUI();sid=null;curCC=null;cwd='';activeModel='default';running=false;compacting=false;localStorage.removeItem(SKEY);
  setCurname('');statset('idle');ta.disabled=true;sendBtn.disabled=true;wsSend({type:'detach'});renderSessionTabs();}
function openLiveSession(s){if(!s||!s.id)return;ensureSessionTab(s,false);switchSession(s.id);}
function sessionTabKeydown(ev){if(!['ArrowLeft','ArrowRight','Home','End'].includes(ev.key))return;
  const tabs=[...$('#sessionTabs').querySelectorAll('.stab-main')];if(!tabs.length)return;let i=tabs.indexOf(ev.target);if(i<0)return;
  if(ev.key==='Home')i=0;else if(ev.key==='End')i=tabs.length-1;else i=(i+(ev.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;
  ev.preventDefault();tabs[i].focus();tabs[i].click();}
$('#sessionTabs').addEventListener('keydown',sessionTabKeydown);
function fmtTok(n){if(n==null)return '?';
  if(n>=1e6)return (n/1e6).toFixed(1).replace(/\.0$/,'')+'M';
  if(n>=1000)return (n/1000).toFixed(n>=1e4?0:1)+'k';
  return ''+n;}
/* shared segmented meter: 5 cells (20% each), whole bar coloured by the total % */
function meterLvl(p){return p>80?'r':p>=60?'o':p>=40?'y':'g';}
function cellBar(pct){const p=Math.max(0,Math.min(100,pct)),lit=Math.min(5,Math.ceil(p/20));
  let s='<span class="cells lv-'+meterLvl(p)+'">';
  for(let i=1;i<=5;i++)s+='<span class="cell'+(i<=lit?' on':'')+'"></span>';
  return s+'</span>';}
function renderCtx(c){currentCtx=c||null;const el=$('#ctx');
  if(!c||c.percentage==null){el.style.display='none';return;}
  const pct=Math.round(c.percentage);
  el.className='ctx';el.style.display='inline-flex';
  el.innerHTML='<span class="ulabel">Context</span>'+cellBar(pct)+'<span>'+pct+'%</span>';
  let title='context '+(c.totalTokens||'?')+' / '+(c.maxTokens||'?')+' tokens ('+pct+'%)'+(c.model?' · '+c.model:'');
  el.title=title;
  setResolvedModel(c.model, c.maxTokens);}
function fmtDur(ms){if(ms==null||ms<=0)return '0m';const m=Math.floor(ms/60000),h=Math.floor(m/60);
  return h>0?(h+'h '+(m%60)+'m'):(m+'m');}
/* rolling usage: 5h + weekly windows as "5h ▮▮ % | 7d ▮ %" inside one pill */
function renderUsage(u){const el=$('#usage');const f=u&&u.five_hour,w=u&&u.seven_day;
  if((!f||f.utilization==null)&&(!w||w.utilization==null)){el.style.display='none';return;}
  function seg(o,lbl){if(!o||o.utilization==null)return '';
    const pct=Math.round(o.utilization);
    return '<span class="useg"><span class="ulabel">'+lbl+'</span>'+cellBar(pct)+'<span>'+pct+'%</span></span>';}
  el.className='usage';el.style.display='inline-flex';
  el.innerHTML='<span class="ulabel">Usage</span>'+seg(f,'5h')+seg(w,'7d');
  let t='';
  [['five_hour','5-hour'],['seven_day','weekly']].forEach(([k,lbl])=>{const o=u&&u[k];
    if(o&&o.utilization!=null){const rem=o.resets_at?fmtDur(new Date(o.resets_at)-Date.now()):'';
      t+=(t?'\n':'')+lbl+': '+Math.round(o.utilization)+'% used'+(rem?(' · resets in '+rem):'');}});
  el.title=t;}
function loadUsage(){fetch('api/usage').then(r=>r.json()).then(j=>renderUsage(j.usage)).catch(()=>{});}
/* The installed Codex app-server is the model source of truth. Both model
   pickers consume this shared catalog, so newly released/account-enabled models
   appear without editing the console. `default` remains a universal offline
   fallback; saved/current retired models are retained as synthetic options. */
function rebuildModelOptions(){const d=(DEFAULT_MODEL_ID&&MODELS.find(m=>m.id===DEFAULT_MODEL_ID))||
    (!DEFAULT_MODEL_ID&&MODELS.find(m=>m.isDefault));
  const defaultName=(d&&d.name)||DEFAULT_MODEL_ID;
  MODEL_OPTIONS=[['default','model: default'+(defaultName?(' · '+defaultName):'')],
    ...MODELS.map(m=>[m.id,m.name||m.id])];}
function modelOptionsFor(current){const opts=MODEL_OPTIONS.map(x=>x.slice());
  if(current&&!opts.some(x=>x[0]===current))opts.push([current,current+' (not in catalog)']);
  return opts;}
function rebuildModelPicker(){const sel=$('#model');if(!sel)return;
  const want=localStorage.getItem('al_model')||sel.value||'default';
  rebuildModelOptions();const known=MODEL_OPTIONS.some(x=>x[0]===want),catalogReady=MODELS.length>0;
  const opts=modelOptionsFor(catalogReady&&!known?'':want);sel.innerHTML='';
  opts.forEach(([v,t])=>{const o=document.createElement('option');o.value=v;o.textContent=t;
    const meta=MODELS.find(m=>m.id===v);if(meta&&meta.description)o.title=meta.description;sel.appendChild(o);});
  if(catalogReady&&!known){const stale=document.createElement('option');stale.value=want;
    stale.textContent=want+' (saved; unavailable)';stale.disabled=true;sel.appendChild(stale);
    sel.value='default';localStorage.setItem('al_model','default');}
  else if(opts.some(x=>x[0]===want))sel.value=want;}
function retryModelLoad(){if(MODELS.length)return;clearTimeout(modelRetryT);
  modelRetryT=setTimeout(loadModels,31000);}
function loadModels(force){return fetch('api/models'+(force?'?fresh=1':''),{cache:'no-store'}).then(r=>r.json()).then(j=>{
  if(j.models&&j.models.length){clearTimeout(modelRetryT);DEFAULT_MODEL_ID=j.defaultModel||'';MODELS=j.models;rebuildModelPicker();
    if(!sid||!ready){curEffort=effortForModel(curEffort,$('#model').value);
      localStorage.setItem('al_effort',curEffort);setEffortPill(curEffort);}}
  else retryModelLoad();}).catch(retryModelLoad);}
/* Per-model effort capabilities come from model/list. The fallback is used only
   before that catalog arrives (or for a retired/custom model absent from it). */
function modelMeta(value){if(value&&value!=='default')return MODELS.find(m=>m.id===value)||null;
  if(DEFAULT_MODEL_ID)return MODELS.find(m=>m.id===DEFAULT_MODEL_ID)||null;
  return MODELS.find(m=>m.isDefault)||null;}
function effortOptionsFor(modelValue){const m=modelMeta(modelValue);
  const values=m&&Array.isArray(m.reasoningEfforts)?m.reasoningEfforts.filter(Boolean):[];
  return values.length?values:FALLBACK_EFFORTS;}
function effortForModel(e,modelValue){const opts=effortOptionsFor(modelValue);if(opts.includes(e))return e;
  const m=modelMeta(modelValue),d=m&&m.defaultReasoningEffort;
  if(d&&opts.includes(d))return d;if(opts.includes('xhigh'))return 'xhigh';return opts[0]||'xhigh';}
function currentEffortModel(){return sid&&ready?activeModel:($('#model').value||'default');}
/* reasoning effort is per-turn. A change applies to the next turn and is also
   remembered as the starting preference for future sessions. */
function setEffortPill(e){if(e)curEffort=e;const el=$('#effort');if(el)el.textContent='🧠 '+curEffort;}
function setEffort(e){if(!effortOptionsFor(currentEffortModel()).includes(e)||e===curEffort)return;
  curEffort=e;localStorage.setItem('al_effort',e);setEffortPill(e);
  if(sid&&ready&&ws&&ws.readyState===1)wsSend({type:'set_effort',effort:e});}   /* applies next turn */
/* show what the live session's model actually resolves to in the picker's default
   option, e.g. "model: opus 4.8 [1M]" — family+version from the model id, the
   [ctx] window from maxTokens. Fed by the ready event and the context usage. */
let _rmodel='', _rmax=0;
function modelLabel(real,maxTok){
  if(!real)return '';
  const s=(''+real).toLowerCase();
  const fam=s.includes('opus')?'opus':s.includes('sonnet')?'sonnet':s.includes('haiku')?'haiku':'';
  if(!fam)return ''+real;
  const m=s.match(new RegExp(fam+'-(\\d+)-(\\d+)'))||s.match(new RegExp('(\\d+)-(\\d+)-'+fam));
  let lbl=fam+(m?(' '+m[1]+'.'+m[2]):'');
  if(maxTok)lbl+='['+fmtTok(maxTok)+']';
  return lbl;}
/* the sidebar #model picker now holds the NEW-session default, so it no longer
   reflects the live session's resolved model (that moved to ⚙ Configure). Kept
   as a no-op shim so existing callers (ready / context) stay valid. */
function setResolvedModel(real,maxTok){if(real)_rmodel=real;if(maxTok)_rmax=maxTok;}

/* sidebar: live sessions (in-RAM) + resume-from-disk (past transcripts) */
/* shared per-card action menu (⋯): a fixed popover anchored to the clicked
   kebab, so it is never clipped by the sidebar's overflow */
function closeCardMenu(){const m=$('#cardMenu');m.classList.remove('on');m.innerHTML='';m._anchor=null;}
/* clicking the same ⋯ again closes the menu (toggle); a different one re-anchors */
function toggleCardMenu(anchor,items){const m=$('#cardMenu');
  if(m.classList.contains('on')&&m._anchor===anchor){closeCardMenu();return;}
  openCardMenu(anchor,items);}
function openCardMenu(anchor,items){const m=$('#cardMenu');m.innerHTML='';m._anchor=anchor;
  items.forEach(it=>{const d=document.createElement('div');d.className='mi'+(it.danger?' danger':'');
    d.textContent=it.label;d.onclick=ev=>{ev.stopPropagation();closeCardMenu();it.fn();};m.appendChild(d);});
  m.classList.add('on');
  const r=anchor.getBoundingClientRect(),mw=m.offsetWidth,mh=m.offsetHeight;
  let left=r.right-mw,top=r.bottom+4;
  if(left<6)left=6;
  if(top+mh>window.innerHeight-6)top=Math.max(6,r.top-mh-4);
  m.style.left=left+'px';m.style.top=top+'px';}
/* ⚙ Configure: change a live session's model / permission in place (hot-swap by
   id — works for any live session) + model-aware effort. The attached session's
   🧠 pill stays in lockstep; background cards update only their target session.
   Reuses the #cardMenu popover shell (positioning + outside-click close). */
function openConfigure(s,anchor){const m=$('#cardMenu');m.innerHTML='';m._anchor=anchor;
  const mkSel=(opts,cur)=>{const o=document.createElement('select');o.className='cfgsel';
    opts.forEach(([v,t])=>{const x=document.createElement('option');x.value=v;x.textContent=t;if(v===cur)x.selected=true;o.appendChild(x);});return o;};
  const refill=(o,opts,cur)=>{o.innerHTML='';opts.forEach(v=>{const x=document.createElement('option');
    x.value=v;x.textContent=v;if(v===cur)x.selected=true;o.appendChild(x);});};
  const row=(label,sel)=>{const w=document.createElement('div');w.className='cfgrow';
    const l=document.createElement('span');l.textContent=label;w.appendChild(l);w.appendChild(sel);m.appendChild(w);};
  const currentModel=s.model||'default';
  const initialEffortModel=currentModel!=='default'?currentModel:(s.display_model||currentModel);
  const currentEffort=effortForModel(s.effort||'',initialEffortModel);
  const modelSel=mkSel(modelOptionsFor(currentModel),currentModel);
  const approvalSel=mkSel([['full-access','🔓 Full access'],['on-request','🔐 Approve'],['auto','⚡ Auto (sandbox)'],['read-only','👁 Read-only']],s.mode||'full-access');
  const effortSel=mkSel(effortOptionsFor(initialEffortModel).map(e=>[e,e]),currentEffort);
  modelSel.onchange=()=>{const model=modelSel.value,effort=effortForModel(effortSel.value,model);
    refill(effortSel,effortOptionsFor(model),effort);
    wsSend({type:'configure',id:s.id,model:model,effort:effort});
    if(s.id===sid){activeModel=model;curEffort=effort;localStorage.setItem('al_effort',effort);setEffortPill(effort);}
    setTimeout(reqList,200);};
  approvalSel.onchange=()=>{wsSend({type:'configure',id:s.id,mode:approvalSel.value});setTimeout(reqList,200);};
  effortSel.onchange=()=>{const effort=effortSel.value;wsSend({type:'configure',id:s.id,effort:effort});
    if(s.id===sid){curEffort=effort;localStorage.setItem('al_effort',effort);setEffortPill(effort);}
    setTimeout(reqList,200);closeCardMenu();};
  row('Model',modelSel);row('Approval',approvalSel);row('Effort',effortSel);
  m.classList.add('on');
  const r=anchor.getBoundingClientRect(),mw=m.offsetWidth,mh=m.offsetHeight;
  let left,top;
  if(anchor.isConnected&&(r.width||r.height)){   /* fresh kebab → anchor to it */
    left=r.right-mw;top=r.bottom+4;
    if(top+mh>window.innerHeight-6)top=r.top-mh-4;
  }else{   /* anchor was detached by a live-list re-render (its rect is all-zeros,
              which would fling the popover to the top-left). Keep the menu where
              it already sat (closeCardMenu leaves style.left/top intact). */
    left=parseFloat(m.style.left)||(window.innerWidth-mw)/2;
    top=parseFloat(m.style.top)||(window.innerHeight-mh)/2;
  }
  left=Math.min(Math.max(6,left),Math.max(6,window.innerWidth-mw-6));
  top=Math.min(Math.max(6,top),Math.max(6,window.innerHeight-mh-6));
  m.style.left=left+'px';m.style.top=top+'px';}

/* collapsible sidebar sections (Favorites / Recent) */
const SECKEY='al_seccol';
function toggleSec(id){const el=$('#'+id);if(!el)return;el.classList.toggle('collapsed');
  let c={};try{c=JSON.parse(localStorage.getItem(SECKEY)||'{}');}catch(e){}
  c[id]=el.classList.contains('collapsed');localStorage.setItem(SECKEY,JSON.stringify(c));}
function applySecCollapse(){let c={};try{c=JSON.parse(localStorage.getItem(SECKEY)||'{}');}catch(e){}
  ['secFav','secRecent'].forEach(id=>{const el=$('#'+id);if(el)el.classList.toggle('collapsed',!!c[id]);});}

function renderLive(list){liveSessions=list||[];syncSessionTabs(liveSessions);renderSidebar();}

/* ── project-grouped sidebar ──────────────────────────────────────────────
   A sidebar row is a FOLDER, and the sessions whose cwd is exactly that folder
   nest under it (a subfolder is its own project). Live therefore answers "which
   projects am I working in" rather than "which threads are open": a project is
   Live when at least one of its sessions is running. Favorites and Recent are
   likewise folder-level. Server data arrives as `projects`; live sessions arrive
   separately over the socket and are merged in here, because a brand-new session
   has no rollout on disk yet. */

function saveExp(){try{localStorage.setItem('al_pexp',JSON.stringify([...pExp]));}catch(e){}}

function shortPath(p){   /* mirrors the server: parent/current, $HOME as ~ */
  if(!p)return '';
  if(HOMEDIR&&p===HOMEDIR)return '~';
  const rel=(HOMEDIR&&p.indexOf(HOMEDIR+'/')===0)?p.slice(HOMEDIR.length+1):p.replace(/^\/+/,'');
  const parts=rel.split('/').filter(Boolean);
  return parts.slice(-2).join('/')||p;}

function buildProjects(){
  const liveBy={};
  (liveSessions||[]).forEach(s=>{const k=s.root||s.cwd||'';if(k)(liveBy[k]=liveBy[k]||[]).push(s);});
  const by={};
  (projData||[]).forEach(p=>{by[p.path]=Object.assign({},p,{sessions:(p.sessions||[]).slice()});});
  /* a folder the server hasn't indexed yet still needs a row */
  Object.keys(liveBy).forEach(k=>{if(!by[k])by[k]={path:k,
    name:(k.split('/').filter(Boolean).slice(-1)[0]||k),sub:shortPath(k),
    fav:false,pinned:false,mtime:Date.now()/1000,sessions:[]};});
  const now=Date.now()/1000;
  return Object.keys(by).map(k=>{const p=by[k];
    p.liveS=liveBy[k]||[];
    const lcc=new Set(p.liveS.map(x=>x.cc).filter(Boolean));
    p.past=(p.sessions||[]).filter(x=>!lcc.has(x.cc));
    p.isLive=p.liveS.some(x=>!x.ended);
    p.anyBusy=p.liveS.some(x=>x.busy);
    p.total=p.liveS.length+p.past.length;
    p.sortKey=p.liveS.length?now:(p.mtime||0);
    return p;}).sort((a,b)=>(b.sortKey||0)-(a.sortKey||0));}

/* Live is sorted by NAME, not recency. Every live project shares the same
   activity sortKey, so a recency order has nothing to break ties with and the
   rows reshuffle on each refresh — the section you watch while working is the
   one that must hold still. Favorites and Recent stay newest-first: there the
   ordering carries real information. */
const byName=(a,b)=>(a.name||'').localeCompare(b.name||'',undefined,
                                               {numeric:true,sensitivity:'base'});
function renderSidebar(){
  const all=buildProjects();
  const live=all.filter(p=>p.isLive).sort(byName);
  const fav=all.filter(p=>!p.isLive&&p.fav);
  const rest=all.filter(p=>!p.isLive&&!p.fav).slice(0,40);
  $('#liveN').textContent=live.length;
  $('#favN').textContent=fav.length;
  fillSec('#liveList',live,'no active project — pick a folder, ＋ New session');
  fillSec('#favList',fav,'star a project to pin it here');
  fillSec('#recentList',rest,'no projects yet');}

function fillSec(sel,list,empty){const box=$(sel);if(!box)return;
  if(!list.length){box.innerHTML='<div class="sb-empty">'+esc(empty)+'</div>';return;}
  box.innerHTML='';list.forEach(p=>box.appendChild(projGroup(p)));}

function projGroup(p){
  const g=document.createElement('div');
  g.className='pgroup'+(pExp.has(p.path)?' open':'');
  const r=document.createElement('div');r.className='prow';
  r.innerHTML='<span class="caret"></span>'+
    '<div class="pmeta"><div class="pname">'+esc(p.name)+(p.fav?' <span class="pstar">★</span>':'')+'</div>'+
    '<div class="psub">'+esc(p.sub||shortPath(p.path))+'</div></div>'+
    '<span class="pn">'+p.total+'</span>'+
    '<span class="skebab pkebab" title="project actions" aria-label="project actions"><i></i></span>';
  r.onclick=ev=>{if(ev.target.closest('.skebab'))return;
    if(pExp.has(p.path))pExp.delete(p.path);else pExp.add(p.path);
    saveExp();g.classList.toggle('open');};
  r.querySelector('.skebab').onclick=ev=>{ev.stopPropagation();
    const items=[{label:'＋ New session here',fn:()=>newSessionIn(p.path)},
      {label:'☑ Manage sessions…',fn:()=>pmanOpen(p)},
      /* browse the project directory in web-file-manager (same ?open= scheme as
         file links — its openPath() loads a directory as a folder view) */
      {label:'📂 Open folder',fn:()=>window.open(webfmOpenUrl(p.path),'_blank')},
      {label:'✎ Rename project',fn:()=>renameProject(p)},
      {label:p.fav?'★ Unfavorite':'☆ Favorite',fn:()=>wsSend({type:'proj_fav',path:p.path,fav:!p.fav})}];
    if(p.pinned)items.push({label:'✕ Remove from sidebar',danger:true,fn:()=>{
      if(confirm('Remove “'+p.name+'” from the sidebar?\n\nOnly the sidebar entry goes away. '+
                 'No session and no file is deleted.'))wsSend({type:'proj_unpin',path:p.path});}});
    toggleCardMenu(ev.currentTarget,items);};
  g.appendChild(r);
  const list=document.createElement('div');list.className='plist';
  p.liveS.forEach(x=>list.appendChild(liveSessRow(x)));
  p.past.forEach(x=>list.appendChild(pastSessRow(x)));
  if(!list.children.length)list.innerHTML='<div class="sb-empty">no sessions yet</div>';
  g.appendChild(list);
  return g;}

/* a running session: green dot, click to switch to it */
function liveSessRow(s){const r=document.createElement('div');
  r.className='srow'+(s.id===sid?' active':'')+(s.ended?' ended':'');
  const dot=s.busy?'busy':(s.ended?'':'on');
  r.innerHTML='<span class="sdot '+dot+'"></span><div class="smeta">'+
    '<div class="sname">'+esc(s.title||s.name||'new session')+'</div></div>'+
    '<span class="skebab" title="more">⋮</span>';
  r.querySelector('.smeta').onclick=()=>openLiveSession(s);
  r.querySelector('.sdot').onclick=()=>openLiveSession(s);
  r.querySelector('.skebab').onclick=ev=>{ev.stopPropagation();const kebab=ev.currentTarget;
    const items=[{label:'⚙ Configure',fn:()=>openConfigure(s,kebab)},
      {label:'✎ Rename',fn:()=>renameSession(s.cc,s.title||s.name)}];
    if(s.cc)items.push({label:'⤓ Export transcript',fn:()=>exportSession(s.cc)});
    items.push({label:'✕ End session',danger:true,fn:()=>endSessionById(s.id,s.name)});
    toggleCardMenu(kebab,items);};
  return r;}

/* an on-disk session: grey dot, click to resume */
function pastSessRow(s){const r=document.createElement('div');r.className='srow';
  r.innerHTML='<span class="sdot"></span><div class="smeta">'+
    '<div class="sname">'+esc(s.title||'session')+'</div>'+
    '<div class="ssub">↺ '+(s.mtime?esc(reltime(s.mtime)):'resume')+'</div></div>'+
    '<span class="skebab" title="more">⋮</span>';
  r.querySelector('.smeta').onclick=()=>resumeSession(s);
  r.querySelector('.sdot').onclick=()=>resumeSession(s);
  r.querySelector('.skebab').onclick=ev=>{ev.stopPropagation();toggleCardMenu(ev.currentTarget,[
    {label:'✎ Rename',fn:()=>renameSession(s.cc,s.title)},
    {label:'⤓ Export transcript',fn:()=>exportSession(s.cc)},
    {label:'🗑 Delete (to trash)',danger:true,fn:()=>delResumable(s)}]);};
  return r;}

/* ── Manage sessions ──────────────────────────────────────────────────────
   Batch cleanup for one project. Sub-agent threads can pile up dozens deep, and
   deleting them one ⋮ menu at a time is the wrong tool. Deletion goes through the
   same del_resumable path as the single-session ⋮ (gio trash, recoverable), and a
   running session is terminated first by the server. */
let pmanProj=null, pmanSel=new Set(), pmanBatch=null;

function pmanRows(){
  if(!pmanProj)return [];
  return (pmanProj.liveS||[]).map(s=>({cc:s.cc,title:s.title||s.name||'new session',
                                       live:true,busy:s.busy,ended:s.ended,mtime:0}))
    .concat((pmanProj.past||[]).map(s=>({cc:s.cc,title:s.title||'session',
                                         live:false,mtime:s.mtime})));}

function pmanOpen(p){
  pmanProj=p;pmanSel=new Set();
  $('#pmansub').textContent=p.name+'  ·  '+(p.sub||'');
  $('#pman').removeAttribute('hidden');
  pmanRender();}

function pmanClose(){$('#pman').setAttribute('hidden','');pmanProj=null;pmanSel=new Set();}
function pmanOpened(){return !$('#pman').hasAttribute('hidden');}

function pmanRender(){
  const rows=pmanRows(),box=$('#pmanlist');
  if(!rows.length){box.innerHTML='<div class="sb-empty">no sessions in this project</div>';}
  else{box.innerHTML='';rows.forEach(x=>{
    const el=document.createElement('div');el.className='pmrow';
    const dis=x.cc?'':' disabled title="this session has no transcript yet"';
    el.innerHTML='<input type="checkbox"'+dis+(x.cc&&pmanSel.has(x.cc)?' checked':'')+'>'+
      '<div class="pmmeta"><div class="pmt">'+esc(x.title)+'</div>'+
      '<div class="pmd">'+(x.live?(x.busy?'working…':(x.ended?'ended':'running')):
                                  ('↺ '+(x.mtime?esc(reltime(x.mtime)):'on disk')))+'</div></div>'+
      (x.live?'<span class="pmlive">live</span>':'');
    const cb=el.querySelector('input');
    const flip=()=>{if(!x.cc)return;
      if(pmanSel.has(x.cc))pmanSel.delete(x.cc);else pmanSel.add(x.cc);
      cb.checked=pmanSel.has(x.cc);pmanBar();};
    el.onclick=ev=>{if(ev.target!==cb)flip();else{cb.checked=!cb.checked;flip();}};
    box.appendChild(el);});}
  pmanBar();}

function pmanBar(){
  const rows=pmanRows().filter(x=>x.cc),n=pmanSel.size;
  $('#pmancount').textContent=n+' selected';
  $('#pmandel').disabled=!n;
  const all=$('#pmanall');
  all.checked=n>0&&n===rows.length;
  all.indeterminate=n>0&&n<rows.length;}

function pmanToggleAll(){
  const rows=pmanRows().filter(x=>x.cc);
  if(pmanSel.size===rows.length)pmanSel=new Set();
  else pmanSel=new Set(rows.map(x=>x.cc));
  pmanRender();}

function pmanDelete(){
  const ccs=[...pmanSel];if(!ccs.length)return;
  const liveN=pmanRows().filter(x=>x.live&&pmanSel.has(x.cc)).length;
  if(!confirm('Delete '+ccs.length+' session'+(ccs.length>1?'s':'')+' from '+
      (pmanProj?pmanProj.name:'this project')+'?'+
      (liveN?('\n\n'+liveN+' of them '+(liveN>1?'are':'is')+' still running and will be ended first.'):'')+
      '\n\nTranscripts move to the trash (recoverable), they are not erased.'))return;
  pmanBatch={n:ccs.length,done:0,ok:0,err:[]};
  ccs.forEach(cc=>wsSend({type:'del_resumable',cc:cc}));
  pmanClose();}

$('#pmanx').onclick=pmanClose;
$('#pmanclose').onclick=pmanClose;
$('#pmandel').onclick=pmanDelete;
$('#pmanall').onclick=pmanToggleAll;
$('#pman').addEventListener('mousedown',e=>{if(e.target.id==='pman')pmanClose();});

function renameProject(p){
  const nm=prompt('Rename project (leave empty to reset to the folder name):',p.renamed?p.name:'');
  if(nm===null)return;
  wsSend({type:'proj_rename',path:p.path,name:nm});}

/* start a session in a specific folder: route through the custom-path box so the
   picker visibly shows where the new session will run */
function newSessionIn(path){
  $('#project').value='__custom__';
  $('#cwdwrap').classList.add('show');
  $('#cwd').value=path;
  $('#newbtn').click();}

function renameSession(cc,cur){
  if(!cc){alert('This session is still starting — try again in a moment.');return;}
  const nm=prompt('Rename session (leave empty to reset to the auto name):',cur||'');
  if(nm===null)return;
  wsSend({type:'rename',cc:cc,name:nm});}
function delResumable(s){
  if(!confirm('Delete this session from disk?\n\n'+(s.title||s.name||s.cc||'session')+
    '\n\nIt is moved to the trash (recoverable), not permanently deleted.'))return;
  wsSend({type:'del_resumable',cc:s.cc});
  /* optimistic: drop it from favorites + cached lists so it vanishes at once */
  projData=(projData||[]).map(p=>Object.assign({},p,
    {sessions:(p.sessions||[]).filter(x=>x.cc!==s.cc)}));
  renderSidebar();}

function exportSession(cc){
  if(!cc){alert('This session has no transcript yet.');return;}
  const a=document.createElement('a');a.href='api/export?cc='+encodeURIComponent(cc);
  a.download=cc+'.jsonl';document.body.appendChild(a);a.click();a.remove();}

let srchAbort=null, srchT=0, srchSeq=0, thState=null;
function srchOpen(){return !$('#srch').hasAttribute('hidden');}
function openSearch(){const w=$('#srch');w.removeAttribute('hidden');showResults();
  const q=$('#srchq');q.focus();q.select();}
function closeSearch(){$('#srch').setAttribute('hidden','');
  if(srchAbort){srchAbort.abort();srchAbort=null;}}
function searchFolder(){
  const s=(liveSessions||[]).find(x=>x.id===sid);
  return (s&&s.cwd)||currentFolder()||'';}
function srchScopeArgs(){const sc=$('#srchscope').value;
  if(sc==='session')return curCC?'&scope=session&cc='+encodeURIComponent(curCC):'&scope=all';
  if(sc==='project'){const f=searchFolder();
    return f?'&scope=project&cwd='+encodeURIComponent(f):'&scope=all';}
  return '&scope=all';}
function runSearch(){
  const q=$('#srchq').value.trim(),res=$('#srchres'),meta=$('#srchmeta');
  showResults();
  if(srchAbort){srchAbort.abort();srchAbort=null;}
  const need=/[㐀-鿿぀-ヿ가-힯]/.test(q)?1:2;
  if(q.length<need){res.innerHTML='';
    meta.textContent='Type at least '+need+' character'+(need===1?'':'s')+'.';return;}
  const seq=++srchSeq;
  meta.textContent='searching...';
  srchAbort=new AbortController();
  const t0=Date.now();
  fetch('api/search?q='+encodeURIComponent(q)+srchScopeArgs(),{signal:srchAbort.signal})
    .then(r=>r.json()).then(j=>{
      if(seq!==srchSeq)return;
      const list=j.results||[];
      meta.textContent=list.length
        ? list.length+(j.more?'+':'')+' match'+(list.length===1?'':'es')+' · '+((Date.now()-t0)/1000).toFixed(1)+'s'
            +(j.more?' · showing the 200 most recent':'')
        : (j.note||j.error||'no matches');
      res.innerHTML='';
      list.forEach(h=>{
        const d=document.createElement('div');d.className='sres';
        const proj=(h.cwd||'').split('/').slice(-2).join('/');
        d.innerHTML='<div class="sh1"><b>'+esc(h.title||'session')+'</b>'+
          '<span class="role">'+esc(h.role)+'</span><span>'+esc(proj)+'</span>'+
          '<span>'+esc((h.ts||'').slice(0,16).replace('T',' '))+'</span></div>'+
          '<div class="sh2">'+esc(h.pre)+'<mark>'+esc(h.hit)+'</mark>'+esc(h.post)+'</div>';
        d.onclick=()=>openThread(h);
        res.appendChild(d);});
    }).catch(e=>{if(e.name!=='AbortError'&&seq===srchSeq)meta.textContent='search failed: '+e;});}
function showResults(){$('#srchthread').setAttribute('hidden','');
  $('#srchres').removeAttribute('hidden');}
function highlightInto(el,txt,q){
  el.textContent=txt;
  if(!q)return;
  const i=txt.toLowerCase().indexOf(q.toLowerCase());
  if(i<0)return;
  el.textContent='';
  el.appendChild(document.createTextNode(txt.slice(0,i)));
  const m=document.createElement('mark');m.textContent=txt.slice(i,i+q.length);
  el.appendChild(m);
  el.appendChild(document.createTextNode(txt.slice(i+q.length)));}
function renderThread(j,q,targetMid){
  const body=$('#thbody');body.innerHTML='';
  $('.thtitle').textContent=(j.title||'session')+' · '+(j.cwd||'').split('/').slice(-2).join('/')+
    ' · '+(j.total||0)+' messages';
  if(!j.atStart){const b=document.createElement('button');b.className='thmore';
    b.textContent='↑ load 60 earlier';
    b.onclick=()=>loadThread(thState.mid,thState.before+60,thState.after,q,targetMid);
    body.appendChild(b);}
  else body.insertAdjacentHTML('beforeend','<div class="thend">— start of conversation —</div>');
  let tgt=null;
  (j.messages||[]).forEach(m=>{
    const d=document.createElement('div');
    d.className='thmsg '+m.role+(m.mid===targetMid?' target':'');
    d.innerHTML='<div class="thr">'+esc(m.role)+' · '+esc((m.ts||'').slice(0,16).replace('T',' '))+'</div>';
    const t=document.createElement('div');t.className='tht';
    highlightInto(t,m.txt||'',m.mid===targetMid?q:'');
    d.appendChild(t);body.appendChild(d);
    if(m.mid===targetMid)tgt=d;});
  if(!j.atEnd){const b=document.createElement('button');b.className='thmore';
    b.textContent='↓ load 60 later';
    b.onclick=()=>loadThread(thState.mid,thState.before,thState.after+60,q,targetMid);
    body.appendChild(b);}
  else body.insertAdjacentHTML('beforeend','<div class="thend">— end of conversation —</div>');
  if(tgt)tgt.scrollIntoView({block:'center'});}
function loadThread(mid,before,after,q,targetMid){
  thState={mid:mid,before:before,after:after,q:q};
  $('#thbody').innerHTML='<div class="thend">loading...</div>';
  fetch('api/thread?mid='+encodeURIComponent(mid)+'&before='+before+'&after='+after)
    .then(r=>r.json()).then(j=>{
      if(j.error){$('#thbody').innerHTML='<div class="thend">'+esc(j.error)+'</div>';return;}
      thState.cc=j.cc;thState.cwd=j.cwd;renderThread(j,q,targetMid);})
    .catch(e=>{$('#thbody').innerHTML='<div class="thend">failed: '+esc(String(e))+'</div>';});}
function openThread(h){
  $('#srchres').setAttribute('hidden','');
  $('#srchthread').removeAttribute('hidden');
  loadThread(h.mid,40,40,$('#srchq').value.trim(),h.mid);}
function wireSearch(){
  const q=$('#srchq');if(!q)return;
  $('#thback').onclick=showResults;
  $('#thopen').onclick=()=>{const s=thState;if(!s||!s.cc)return;
    closeSearch();resumeSession({cc:s.cc,cwd:s.cwd});};
  q.addEventListener('input',()=>{clearTimeout(srchT);srchT=setTimeout(runSearch,260);});
  q.addEventListener('keydown',e=>{if(e.key==='Enter'){clearTimeout(srchT);runSearch();}});
  $('#srchscope').onchange=runSearch;
  $('#srchx').onclick=closeSearch;
  const ob=$('#srchopen');if(ob)ob.onclick=()=>{openSearch();if(window.innerWidth<=860)closeSidebar();};
  $('#srch').onclick=e=>{if(e.target===$('#srch'))closeSearch();};
  document.addEventListener('keydown',e=>{
    if((e.ctrlKey||e.metaKey)&&(e.key==='k'||e.key==='K')){e.preventDefault();
      srchOpen()?closeSearch():openSearch();return;}
    if(e.key==='Escape'&&srchOpen()){e.preventDefault();e.stopPropagation();closeSearch();}
  },true);}
function impMsg(t,bad){const el=$('#impmsg');if(!el)return;
  el.textContent=t||'';el.classList.toggle('bad',!!bad);
  if(t&&!bad)setTimeout(()=>{if(el.textContent===t)el.textContent='';},6000);}
function importTarget(){const p=$('#project').value;
  return (($('#cwd').value||'').trim())||(p==='__custom__'?'':(p||''));}
function wireImport(){
  const btn=$('#impbtn'),inp=$('#impfile');if(!btn||!inp)return;
  btn.onclick=()=>{if(!importTarget()){impMsg('pick a project folder above first',true);return;}
    inp.click();};
  inp.onchange=()=>{const f=inp.files&&inp.files[0];if(!f)return;
    const dir=importTarget();
    if(!dir){impMsg('pick a project folder above first',true);inp.value='';return;}
    const fd=new FormData();fd.append('file',f);fd.append('cwd',dir);
    impMsg('importing '+f.name+' ...');
    fetch('api/import',{method:'POST',body:fd}).then(r=>r.json().then(j=>({ok:r.ok,j})))
      .then(({ok,j})=>{
        if(ok&&j.ok){impMsg('imported · resume it from its project below');loadTree();}
        else impMsg(((j&&j.error)||'import failed'),true);})
      .catch(e=>impMsg(String(e),true))
      .finally(()=>{inp.value='';});};}

/* pull the folder-grouped sidebar. The socket also pushes it on connect and after
   any change, so this is the cold-start and manual-rescan path. */
function loadTree(){
  fetch('api/tree').then(r=>r.json())
    .then(j=>{projData=j.projects||[];renderSidebar();})
    .catch(()=>{});}

function currentFolder(){const p=$('#project').value;
  return p==='__custom__'?$('#cwd').value.trim():(p||'');}

/* directory autocomplete for the custom path box (server /api/dircomplete) */
let acItems=[],acSel=-1,acTimer=0;
function acClose(){const b=$('#cwdac');b.classList.remove('on');b.innerHTML='';acItems=[];acSel=-1;}
function acRender(j){const b=$('#cwdac');acItems=(j&&j.dirs)||[];acSel=-1;
  if(!acItems.length){acClose();return;}
  let html=acItems.map((p,i)=>{const base=(p.split('/').filter(Boolean).slice(-1)[0])||p;
    return '<div class="acitem" data-i="'+i+'"><div class="acname">'+esc(base)+'</div><div class="acpath">'+esc(p)+'</div></div>';}).join('');
  if(j&&j.more)html+='<div class="acmore">… more — keep typing to narrow</div>';
  b.innerHTML=html;b.classList.add('on');
  b.querySelectorAll('.acitem').forEach(el=>el.onmousedown=ev=>{ev.preventDefault();acPick(+el.dataset.i);});}
function acPick(i){if(i<0||i>=acItems.length)return;
  $('#cwd').value=acItems[i]+'/';   /* auto-append slash → keep drilling with the mouse, no typing */
  $('#cwd').focus();loadTree();acQuery();}
function acMove(d){const els=$('#cwdac').querySelectorAll('.acitem');if(!els.length)return;
  acSel=(acSel+d+els.length)%els.length;els.forEach((el,i)=>el.classList.toggle('sel',i===acSel));els[acSel].scrollIntoView({block:'nearest'});}
function acQuery(){clearTimeout(acTimer);const q=$('#cwd').value;
  acTimer=setTimeout(()=>fetch('api/dircomplete?q='+encodeURIComponent(q)).then(r=>r.json()).then(acRender).catch(acClose),130);}

function switchSession(id){if(!id)return;if(id===sid){const current=sessionTabById(id);if(current){current.unread=false;persistSessionTabs();renderSessionTabs();}
    if(window.innerWidth<=860)closeSidebar();return;}
  const live=liveSessions.find(s=>s.id===id);if(live)ensureSessionTab(live,false);stashSessionView(sid);saveDraft();clearUI();restoredViewId='';
  sid=id;const target=sessionTabById(id);if(target)target.unread=false;localStorage.setItem(SKEY,id);persistSessionTabs();renderSessionTabs();
  const cached=restoreSessionView(id);if(!cached)statset('switching…');
  const go=()=>{const req={type:'attach',id:id};if(cached)req.after_seq=Number(target&&target.lastSeq||0);wsSend(req);};
  if(ws&&ws.readyState===1)go();else openWs(go);
  if(window.innerWidth<=860)closeSidebar();}
function resumeSession(s){if(!s||!s.cc)return;stashSessionView(sid);saveDraft();clearUI();restoredViewId='';pendingStart=true;sid=null;renderSessionTabs();statset('resuming…');
  const go=()=>wsSend({type:'resume',cc:s.cc,cwd:s.cwd,model:$('#model').value,mode:$('#mode').value});
  if(ws&&ws.readyState===1)go();else openWs(go);
  if(window.innerWidth<=860)closeSidebar();}
function newSession(){const proj=$('#project').value;const dir=proj==='__custom__'?$('#cwd').value.trim():proj;
  if(!dir){addErr('pick a project directory first');return;}
  /* keep any current session alive in the background — just spin up another */
  const model=$('#model').value,effort=effortForModel(curEffort,model);
  curEffort=effort;localStorage.setItem('al_effort',effort);setEffortPill(effort);
  stashSessionView(sid);saveDraft();clearUI();restoredViewId='';pendingStart=true;sid=null;renderSessionTabs();
  const start=()=>wsSend({type:'start',cwd:dir,model:model,mode:$('#mode').value,effort:effort});
  if(ws&&ws.readyState===1)start();else openWs(start);statset('starting…');
  if(window.innerWidth<=860)closeSidebar();}
function endSessionById(id,name){if(!id)return;
  if(!confirm('End session '+(name?'« '+name+' »':'')+'?\nIts codex process stops; you can still resume it from disk later.'))return;
  wsSend({type:'end',id:id});
  if(id===sid){setCurname('');markEnded('session ended');}
  reqList();loadTree();}
/* image attachments: paste (Ctrl/Cmd+V) an image into the composer */
let pendingImages=[];
const MAX_IMG=8, MAX_IMG_BYTES=5*1024*1024, OK_IMG=['image/png','image/jpeg','image/gif','image/webp'];
function renderAttach(){const a=$('#attach');a.classList.toggle('on',pendingImages.length>0);
  a.innerHTML=pendingImages.map((im,i)=>'<div class="att"><img src="'+im.url+'"><button class="rm" data-i="'+i+'" title="remove">✕</button></div>').join('');
  a.querySelectorAll('.rm').forEach(b=>b.onclick=()=>{pendingImages.splice(+b.dataset.i,1);renderAttach();});}
function addImageFile(file){
  if(pendingImages.length>=MAX_IMG){addNotice('⚠ up to '+MAX_IMG+' images at once');return;}
  if(OK_IMG.indexOf(file.type)<0){addNotice('⚠ unsupported image type: '+(file.type||'?'));return;}
  if(file.size>MAX_IMG_BYTES){addNotice('⚠ image too large ('+Math.round(file.size/1048576)+'MB, max 5MB)');return;}
  const r=new FileReader();
  r.onload=()=>{const url=''+r.result;pendingImages.push({media_type:file.type,data:url.split(',')[1]||'',url:url});renderAttach();};
  r.readAsDataURL(file);}
function handlePaste(e){const cd=e.clipboardData;if(!cd)return;
  if((cd.getData('text/plain')||'').length>0)return;
  const items=cd.items||[];let got=false;
  for(const it of items){if(it.kind==='file'&&it.type.indexOf('image/')===0){const f=it.getAsFile();if(f){addImageFile(f);got=true;}}}
  if(got)e.preventDefault();}
/* per-session composer drafts — each session keeps its own unsent text (+ images),
   so switching sessions swaps the draft with the chat. Text persists across reloads
   via localStorage; attached images are kept in memory only. */
const DKEY='al_drafts';
let drafts={}, _dpT=0;
try{const sv=JSON.parse(localStorage.getItem(DKEY)||'{}');for(const k in sv)drafts[k]={text:sv[k]||'',images:[]};}catch(e){}
function persistDrafts(){const t={};for(const k in drafts){const v=drafts[k];if(v&&v.text&&v.text.trim())t[k]=v.text;}try{localStorage.setItem(DKEY,JSON.stringify(t));}catch(e){}}
function schedulePersist(){clearTimeout(_dpT);_dpT=setTimeout(persistDrafts,500);}
function saveDraft(){if(sid)drafts[sid]={text:ta.value,images:pendingImages.slice()};persistDrafts();}
function loadDraft(id){const d=drafts[id]||{text:'',images:[]};ta.value=d.text||'';
  ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight,160)+'px';
  pendingImages=(d.images||[]).slice();renderAttach();}
function dropDraft(id){if(id&&drafts[id]){delete drafts[id];persistDrafts();}}
function sendMsg(){const t=ta.value.trim();
  if((!t&&!pendingImages.length)||!ready||!sid||!ws||ws.readyState!==1)return;
  wsSend({type:'user',text:t,images:pendingImages.map(im=>({media_type:im.media_type,data:im.data}))});
  ta.value='';ta.style.height='auto';pendingImages=[];renderAttach();dropDraft(sid);}
  /* busy state (and the thinking word/timer) is driven by the server's
     turn_start, so it stays correct across reattach — no optimistic flip here */

/* queued messages: chips above the composer while the agent is busy. Click a
   chip (or press ↑ on an empty box) to withdraw it back into the editor. */
let queued={};
function renderQueue(){const q=$('#queue');if(!q)return;const ids=Object.keys(queued);
  q.classList.toggle('on',ids.length>0);
  q.innerHTML=ids.map(id=>'<div class="qmsg" data-q="'+id+'" title="click to edit · ✕ to discard">'+
    '<span class="qicon">⏳</span><span class="qtext">'+esc(queued[id].text||'')+
    (queued[id].images?(' 🖼×'+queued[id].images):'')+'</span><span class="qx" title="discard">✕</span></div>').join('');
  q.querySelectorAll('.qmsg').forEach(el=>{const id=el.dataset.q;
    el.querySelector('.qx').onclick=ev=>{ev.stopPropagation();discardQueued(id);};
    el.onclick=()=>editQueued(id);});}
function addQueued(ev){queued[ev.qid]={text:ev.text||'',images:ev.images||0};renderQueue();}
function removeQueued(qid){if(queued[qid]){delete queued[qid];renderQueue();}}
function discardQueued(id){if(ws&&ws.readyState===1)wsSend({type:'unqueue',qid:id});removeQueued(id);}
function editQueued(id){const it=queued[id];if(!it)return;
  const draft=ta.value;
  ta.value=draft.trim()?(it.text+'\n'+draft):it.text;   /* keep any in-progress draft */
  ta.dispatchEvent(new Event('input'));ta.focus();
  if(it.images)addNotice('⚠ image(s) on the withdrawn message were dropped — re-paste if needed');
  discardQueued(id);}

/* sidebar open/close (mobile drawer; desktop collapse) */
function openSidebar(){$('#sidebar').classList.add('open');$('#sb-backdrop').classList.add('show');}
function closeSidebar(){$('#sidebar').classList.remove('open');$('#sb-backdrop').classList.remove('show');}
function toggleSidebar(){const sb=$('#sidebar');
  if(window.innerWidth<=860){sb.classList.contains('open')?closeSidebar():openSidebar();}
  else sb.classList.toggle('collapsed');}

/* changes drawer (Edits | Git) */
function drawerOpen(){return $('#drawer').classList.contains('open');}
function gitTab(){return $('#tabGit').classList.contains('on');}
function showTab(w){const e=w==='edits';$('#tabEdits').classList.toggle('on',e);$('#tabGit').classList.toggle('on',!e);
  $('#edits').style.display=e?'':'none';$('#gitc').style.display=e?'none':'';if(e){buildPendingEdits();showAllEdits();}else refreshGit();}
/* clicking "see Changes" focuses the drawer on just that one file's edit;
   the Edits tab header (or "show all") brings the full list back */
function showAllEdits(){const ed=$('#edits');const fb=ed.querySelector('.efocus');if(fb)fb.remove();
  ed.classList.remove('focusone');
  ed.querySelectorAll('.ecard').forEach(c=>c.style.display='');}
function focusEdit(toolId){const ed=$('#edits'),target=toolId&&tools[toolId];
  if(!target){showAllEdits();return;}
  ed.querySelectorAll('.ecard').forEach(c=>{c.style.display=(c===target)?'':'none';});
  ed.classList.add('focusone');
  let fb=ed.querySelector('.efocus');
  if(!fb){fb=document.createElement('div');fb.className='efocus';ed.insertBefore(fb,ed.firstChild);}
  fb.innerHTML='showing one file · <span class="showall">show all changes</span>';
  fb.querySelector('.showall').onclick=showAllEdits;
  target.classList.add('flash');target.scrollIntoView({block:'start'});setTimeout(()=>target.classList.remove('flash'),1200);}
function openDrawer(w){$('#drawer').classList.add('open');showTab(w||'edits');}
async function refreshGit(){if(!cwd){$('#gitc').innerHTML='<div class="empty">no session</div>';return;}
  try{const r=await fetch('api/diff?cwd='+encodeURIComponent(cwd));const j=await r.json();
    if(!j.ok){$('#gitc').innerHTML='<div class="empty">'+esc(j.error||'n/a')+'</div>';return;}
    let h='';if(j.files&&j.files.length)h+=j.files.map(f=>'<div class="gfile"><span class="st">'+esc(f.status)+'</span>'+esc(f.path)+'</div>').join('')+'<hr style="border-color:#333;margin:6px 0">';
    h+=j.diff&&j.diff.trim()?diffHtml(j.diff):'<div class="empty">clean ✓</div>';$('#gitc').innerHTML=h;
  }catch(e){}}

/* bindings */
ta.addEventListener('input',()=>{ta.style.height='auto';ta.style.height=Math.min(ta.scrollHeight,160)+'px';
  if(sid){drafts[sid]={text:ta.value,images:pendingImages.slice()};schedulePersist();}});
ta.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMsg();}
  else if(e.key==='ArrowUp'&&!ta.value&&Object.keys(queued).length){
    e.preventDefault();const ids=Object.keys(queued);editQueued(ids[ids.length-1]);}});
window.addEventListener('paste',handlePaste);
sendBtn.onclick=sendMsg;
$('#stop').onclick=doInterrupt;
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&running&&!srchOpen()&&!agentPanelOpen()&&!$('#cwdac').classList.contains('on')){e.preventDefault();doInterrupt();}});
$('#newbtn').onclick=newSession;
/* color theme: apply + persist (the <head> script already set it pre-paint) */
function applyTheme(t){if(t&&t!=='dark')document.documentElement.setAttribute('data-theme',t);
  else document.documentElement.removeAttribute('data-theme');}
(function(){const t=localStorage.getItem('al_theme')||'dark';const sel=$('#theme');
  if(sel){sel.value=t;sel.onchange=()=>{const v=sel.value;localStorage.setItem('al_theme',v);applyTheme(v);};}
  applyTheme(t);})();
$('#navtoggle').onclick=toggleSidebar;
$('#sb-backdrop').onclick=closeSidebar;
$('#agentbtn').onclick=()=>agentPanelOpen()?closeSubagents():openSubagents();
$('#agentClose').onclick=closeSubagents;
/* effort pill: model/list decides which depths this model supports. */
$('#effort').onclick=ev=>{ev.stopPropagation();toggleCardMenu(ev.currentTarget,
  effortOptionsFor(currentEffortModel()).map(e=>({label:(e===curEffort?'● ':'○ ')+e,fn:()=>setEffort(e)})));};
setEffortPill(curEffort);
/* desktop: drag the sidebar's right edge to resize (clamped + persisted) */
const SBW_KEY='al_sbw',SBW_MIN=200,SBW_MAX=560;
function setSidebarW(w,save){w=Math.max(SBW_MIN,Math.min(SBW_MAX,Math.round(w)));
  document.documentElement.style.setProperty('--sbw',w+'px');
  if(save)localStorage.setItem(SBW_KEY,w);}
(function(){const saved=parseInt(localStorage.getItem(SBW_KEY)||'',10);if(saved)setSidebarW(saved,false);
  const h=$('#sbresize');if(!h)return;let on=false;
  h.addEventListener('mousedown',e=>{on=true;h.classList.add('drag');document.body.style.userSelect='none';e.preventDefault();});
  window.addEventListener('mousemove',e=>{if(!on)return;setSidebarW(e.clientX-$('#sidebar').getBoundingClientRect().left,false);});
  window.addEventListener('mouseup',()=>{if(!on)return;on=false;h.classList.remove('drag');document.body.style.userSelect='';
    setSidebarW($('#sidebar').getBoundingClientRect().width,true);});
  h.addEventListener('dblclick',()=>setSidebarW(270,true));   /* double-click: reset to default */
})();
/* desktop: drag the Changes drawer's left edge to resize (clamped + persisted) */
const DRW_KEY='al_drw',DRW_MIN=360,DRW_MAX=1100;
function setDrawerW(w,save){const cap=Math.round(window.innerWidth*0.96);
  w=Math.max(DRW_MIN,Math.min(Math.min(DRW_MAX,cap),Math.round(w)));
  document.documentElement.style.setProperty('--drw',w+'px');
  if(save)localStorage.setItem(DRW_KEY,w);}
(function(){const saved=parseInt(localStorage.getItem(DRW_KEY)||'',10);if(saved)setDrawerW(saved,false);
  const h=$('#drresize');if(!h)return;let on=false;
  h.addEventListener('mousedown',e=>{on=true;h.classList.add('drag');document.body.style.userSelect='none';e.preventDefault();});
  window.addEventListener('mousemove',e=>{if(!on)return;setDrawerW(window.innerWidth-e.clientX,false);});
  window.addEventListener('mouseup',()=>{if(!on)return;on=false;h.classList.remove('drag');document.body.style.userSelect='';
    setDrawerW($('#drawer').getBoundingClientRect().width,true);});
  h.addEventListener('dblclick',()=>setDrawerW(560,true));   /* double-click: reset to default */
})();
$('#resumeRef').onclick=e=>{e.stopPropagation();wsSend({type:'proj_refresh'});loadTree();};
/* collapsible sections: clicking the header toggles; restore saved state */
['secFav','secRecent'].forEach(id=>{
  const h=$('#'+id+' .sb-h');if(h)h.onclick=()=>toggleSec(id);});
applySecCollapse();
/* dismiss the ⋯ card menu on outside-click, Escape, scroll or resize */
document.addEventListener('click',e=>{if(!e.target.closest('#cardMenu')&&!e.target.closest('.skebab'))closeCardMenu();});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeCardMenu();
  if(fimpOpened())fimpClose();if(pmanOpened())pmanClose();if(agentPanelOpen())closeSubagents();}});
window.addEventListener('resize',closeCardMenu);
window.addEventListener('scroll',closeCardMenu,true);
/* model/mode pickers set the NEW-session default only (persisted). A running
   session is reconfigured per-session via the ⚙ Configure menu, so editing the
   new-session default no longer disturbs the current session. */
$('#model').onchange=()=>{const model=$('#model').value;localStorage.setItem('al_model',model);
  if(!sid||!ready){curEffort=effortForModel(curEffort,model);
    localStorage.setItem('al_effort',curEffort);setEffortPill(curEffort);}};
$('#mode').onchange=()=>localStorage.setItem('al_mode',$('#mode').value);
rebuildModelPicker();   /* restore a saved model even before the live catalog arrives */
{const smd=localStorage.getItem('al_mode');if(smd){const o=$('#mode');for(const x of o.options)if(x.value===smd){o.value=smd;break;}}}
$('#tabEdits').onclick=()=>showTab('edits');
$('#tabGit').onclick=()=>showTab('git');
$('#dclose').onclick=()=>$('#drawer').classList.remove('open');
/* dismiss the Changes drawer on outside-click (chat, sidebar, composer…) or Escape,
   so you don't have to aim for the ✕. The .emark "see Changes" markers are the
   openers → excluded, else the opening click would immediately re-close it. */
document.addEventListener('click',e=>{
  if(drawerOpen()&&!e.target.closest('#drawer')&&!e.target.closest('.emark'))$('#drawer').classList.remove('open');});
document.addEventListener('click',e=>{
  if(agentPanelOpen()&&!e.target.closest('#agentPanel')&&!e.target.closest('#agentbtn')&&!e.target.closest('.agmark'))closeSubagents();});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&drawerOpen())$('#drawer').classList.remove('open');});
$('#grefresh').onclick=refreshGit;
$('#project').onchange=()=>{const c=$('#project').value==='__custom__';
  $('#cwdwrap').classList.toggle('show',c);
  if(c){if(!$('#cwd').value)$('#cwd').value=HOMEDIR?(HOMEDIR+'/'):'';$('#cwd').focus();acQuery();}else acClose();
  loadTree();};
$('#cwd').addEventListener('input',acQuery);
$('#cwd').addEventListener('focus',acQuery);
$('#cwd').addEventListener('change',loadTree);
$('#cwd').addEventListener('blur',()=>setTimeout(acClose,160));
$('#cwd').addEventListener('keydown',e=>{const b=$('#cwdac');if(!b.classList.contains('on'))return;
  if(e.key==='ArrowDown'){e.preventDefault();acMove(1);}
  else if(e.key==='ArrowUp'){e.preventDefault();acMove(-1);}
  else if(e.key==='Enter'&&acSel>=0){e.preventDefault();acPick(acSel);}
  else if(e.key==='Escape')acClose();});

/* ── Import folder ────────────────────────────────────────────────────────
   Register a directory as a sidebar project. Folders normally reach the sidebar
   by owning a session, so a fresh directory would otherwise be invisible until
   its first session existed. Same /api/dircomplete backend as the path box. */
let fimpItems=[],fimpSel=-1,fimpT=0;
function fimpMsg(t,bad){const e=$('#fimpmsg');if(!e)return;e.textContent=t||'';e.classList.toggle('bad',!!bad);}
function fimpOpen(){const w=$('#fimp');w.removeAttribute('hidden');fimpMsg('');
  const i=$('#fimpq');i.value=HOMEDIR?HOMEDIR+'/':'';i.focus();fimpQuery();}
function fimpClose(){$('#fimp').setAttribute('hidden','');fimpItems=[];fimpSel=-1;}
function fimpOpened(){return !$('#fimp').hasAttribute('hidden');}
function fimpMark(){const b=$('#fimpac');
  b.querySelectorAll('.acitem').forEach((el,i)=>el.classList.toggle('sel',i===fimpSel));
  const cur=b.querySelector('.acitem.sel');if(cur)cur.scrollIntoView({block:'nearest'});}
function fimpRender(j){const b=$('#fimpac');fimpItems=(j&&j.dirs)||[];fimpSel=-1;
  if(!fimpItems.length){b.innerHTML='<div class="acmore">no matching folder</div>';return;}
  b.innerHTML=fimpItems.map((p,i)=>'<div class="acitem" data-i="'+i+'"><div class="acname">'+
    esc(p.split('/').filter(Boolean).slice(-1)[0]||p)+'</div><div class="acpath">'+esc(p)+'</div></div>').join('')
    +((j&&j.more)?'<div class="acmore">… keep typing to narrow</div>':'');
  b.querySelectorAll('.acitem').forEach(el=>el.onclick=()=>{
    $('#fimpq').value=fimpItems[+el.dataset.i]+'/';$('#fimpq').focus();fimpQuery();});}
function fimpQuery(){clearTimeout(fimpT);
  fimpT=setTimeout(()=>fetch('api/dircomplete?q='+encodeURIComponent($('#fimpq').value))
    .then(r=>r.json()).then(fimpRender).catch(()=>{}),130);}
function fimpAdd(){
  let p=$('#fimpq').value.trim();
  if(p.length>1)p=p.replace(/\/+$/,'');
  if(!p){fimpMsg('type a folder path',true);return;}
  fimpMsg('adding…');
  fetch('api/pinfolder',{method:'POST',headers:{'Content-Type':'application/json'},
                         body:JSON.stringify({path:p})})
    .then(r=>r.json()).then(j=>{
      if(j&&j.ok){fimpClose();addNotice('📁 project « '+(j.name||p)+' » added to the sidebar');loadTree();}
      else fimpMsg((j&&j.error)||'could not add that folder',true);})
    .catch(e=>fimpMsg(String(e),true));}
$('#fimpbtn').onclick=fimpOpen;
$('#fimpx').onclick=fimpClose;
$('#fimpcancel').onclick=fimpClose;
$('#fimpok').onclick=fimpAdd;
$('#fimp').addEventListener('mousedown',e=>{if(e.target.id==='fimp')fimpClose();});
$('#fimpq').addEventListener('input',fimpQuery);
$('#fimpq').addEventListener('keydown',e=>{
  if(e.key==='ArrowDown'){e.preventDefault();if(fimpItems.length){fimpSel=Math.min(fimpSel+1,fimpItems.length-1);fimpMark();}}
  else if(e.key==='ArrowUp'){e.preventDefault();if(fimpItems.length){fimpSel=Math.max(fimpSel-1,0);fimpMark();}}
  else if(e.key==='Enter'){e.preventDefault();
    if(fimpSel>=0){$('#fimpq').value=fimpItems[fimpSel]+'/';fimpSel=-1;fimpQuery();}
    else fimpAdd();}
  else if(e.key==='Escape'){e.preventDefault();fimpClose();}});

/* project picker: custom path plus recent session dirs */
(async function(){try{const r=await fetch('api/projects');const j=await r.json();HOMEDIR=j.home||'';const sel=$('#project');
  /* Custom path… first (easy to reach); recent session dirs stay as a small
     convenience list without scanning the filesystem for repositories. */
  const cust=document.createElement('option');cust.value='__custom__';cust.textContent='✎  Custom path…';sel.appendChild(cust);
  const mk=(label,items)=>{if(!items.length)return;const g=document.createElement('optgroup');g.label=label;
    items.forEach(p=>{const o=document.createElement('option');o.value=p.path;o.textContent=p.path.split('/').slice(-2).join('/');o.title=p.path;g.appendChild(o);});sel.appendChild(g);};
  const recent=(j.projects||[]).filter(p=>p.recent);
  mk('Recent',recent);
  const first=recent[0];
  sel.value=first?first.path:'__custom__';
  $('#cwdwrap').classList.toggle('show',sel.value==='__custom__');
  loadTree();
}catch(e){}})();

setInterval(()=>reqList(),8000);
setInterval(loadTree,30000);
loadTree();
loadUsage();
setInterval(loadUsage,60000);
loadModels();
setInterval(loadModels,300000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)loadModels();});
wireImport();
wireSearch();
$('#mrefresh').onclick=()=>{const b=$('#mrefresh');if(b.classList.contains('busy'))return;
  b.classList.add('busy');loadModels(true).finally(()=>b.classList.remove('busy'));};
openWs();
</script>
</body>
</html>"""


def _recap_tick():
    """Periodic sweep: any session idle past the threshold gets a one-line recap,
    so it's already waiting (in the log) when you return — and fires for a session
    you're sitting on idle too. At most one recap per idle window (recap_for dedup)."""
    if not RECAP_ENABLED:
        return
    now = time.time()
    for s in list(CHAT_SESSIONS.values()):
        if (not s.busy and not s.ended and not s.compacting and not s.recap_busy
                and s.recap_for != s.last_activity
                and (now - s.last_activity) >= RECAP_IDLE_SEC):
            tornado.ioloop.IOLoop.current().spawn_callback(s._make_recap)


def main():
    moved = migrate_session_favs_to_projects()
    app = tornado.web.Application([
        (r"/", ConsoleHandler),
        (r"/console", ConsoleHandler),
        (r"/api/projects", ProjectsHandler),
        (r"/api/resumable", ResumableHandler),
        (r"/api/tree", TreeHandler),
        (r"/api/pinfolder", PinFolderHandler),
        (r"/api/dircomplete", DirCompleteHandler),
        (r"/api/diff", DiffHandler),
        (r"/api/usage", UsageHandler),
        (r"/api/models", ModelsHandler),
        (r"/api/export", ExportHandler),
        (r"/api/import", ImportHandler),
        (r"/api/search", SearchHandler),
        (r"/api/thread", ThreadHandler),
        (r"/ws/chat", ChatSocket),
        (r"/static/(.*)", tornado.web.StaticFileHandler,
         {"path": os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")}),
    ])
    loopback = BIND in ("127.0.0.1", "localhost", "::1")
    app.listen(PORT, address=BIND, max_buffer_size=IMPORT_MAX, max_body_size=IMPORT_MAX)
    tornado.ioloop.IOLoop.current().run_in_executor(None, reindex)
    if RECAP_ENABLED:   # idle-session recap sweep (every 30s)
        tornado.ioloop.PeriodicCallback(_recap_tick, 30000).start()
    print("Codex Console on http://%s:%d" % (BIND, PORT))
    print("  console: http://%s:%d/" % (BIND, PORT))
    print("  codex bin: %s" % CODEX_BIN)
    print("  codex transcripts:  %s" % CODEX_ROOT)
    print("  auth: %s" % ("enabled" if AUTH else "disabled"))
    if moved:
        print("  migrated %d starred session(s) onto their project folders" % moved)
    if not HAVE_CODEX:
        print("  ⚠️  codex CLI not found — install it or set CODEX_CONSOLE_CODEX=/path/to/codex")
    if not loopback and not AUTH:
        print("  ⚠️  EXPOSED on %s WITHOUT auth — this serves your agent"
              " transcripts and code diffs. Set CODEX_CONSOLE_AUTH=user:pass." % BIND)

    def _shutdown(signum, frame):
        for s in list(CHAT_SESSIONS.values()):
            try:
                s.terminate()
            except Exception:
                pass
        os._exit(0)
    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    tornado.ioloop.IOLoop.current().start()


if __name__ == "__main__":
    main()
