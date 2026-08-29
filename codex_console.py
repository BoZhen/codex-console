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
    c = _txt(command).strip()
    if not c:
        return False
    if "&&" in c:
        parts = [p.strip() for p in re.split(r"\s*&&\s*", c)]
        return all(parts) and all(_codex_is_read_cmd(p) for p in parts)
    if _READ_SLICE_PIPE_RE.match(c):
        return True
    return bool((_READ_RE.match(c) or _SED_READ_RE.match(c))
                and not re.search(r"[|;&]|\$\(|`|>|<", c))

def _codex_cmd(command):
    """→ (tool, shown_command). tool is 'Read' for a simple single-file read,
    else 'shell'; shown_command is the unwrapped inner command."""
    c = _txt(command).strip()
    m = _WRAP_RE.match(c)
    inner = (m.group(2) if m else c).strip()
    if _codex_is_read_cmd(inner):
        return "Read", inner
    return "shell", inner


def _codex_exec_event(base, args, call_id):
    args = args if isinstance(args, dict) else {}
    cmd = _txt(args.get("cmd") or args.get("command"))
    tool, shown = _codex_cmd(cmd)
    return {**base, "kind": "tool_use", "tool": tool,
            "input": {"command": _cap(shown), "cwd": args.get("workdir") or args.get("cwd")},
            "toolId": call_id or ""}


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
            self.log = load_transcript_events(self.resume_cc)

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
            "name": "codex-console", "version": "0.1.0", "title": "Codex Console"}})
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
        self.display_model = _display_model(self.model, (res or {}).get("model"))
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

    # ───────── inbound: notifications → normalized events ─────────
    def _on_note(self, method, p):
        if method == "thread/started":
            th = p.get("thread") or {}
            if th.get("id"):
                self.cc_id = self.thread_id = th["id"]
        elif method == "turn/started":
            self.turn_id = (p.get("turn") or {}).get("id")
        elif method == "turn/completed":
            self._finish_turn(error=False)
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
            if d and not self._tok_exact:
                self._tok_chars += len(d)
                self._tok_out = max(self._tok_out, (self._tok_chars + 3) // 4)
                self._emit_tokens()
        elif method in ("item/reasoning/textDelta", "item/reasoning/summaryTextDelta"):
            d = p.get("delta") or ""
            if d and not self._tok_exact:
                self._tok_chars += len(d)
                self._tok_out = max(self._tok_out, (self._tok_chars + 3) // 4)
                self._emit_tokens()
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
        # ignored: hook/*, mcpServer/*, thread/status/changed, turn/diff/updated,
        # turn/plan/updated, account/updated, warning, model/*, fuzzyFileSearch/* …

    def _on_item(self, item, started):
        it = item.get("type")
        iid = item.get("id")
        if it == "userMessage":
            return                          # echoed locally by _echo_user
        if it == "agentMessage":
            if not started:
                txt = item.get("text") or ""
                if txt.strip():
                    self._push([{"kind": "assistant_text", "text": _cap(txt)}])
            return
        if it == "reasoning":
            if not started:
                txt = _txt(item.get("summary") or item.get("content"))
                if txt.strip():
                    self._push([{"kind": "thinking", "text": _cap(txt)}])
            return
        if it == "plan":
            if not started:
                txt = _txt(item.get("text"))
                if txt.strip():
                    self._push([{"kind": "assistant_text", "text": _cap("📋 Plan\n" + txt)}])
            return
        if it == "commandExecution":
            cmd = _txt(item.get("command"))
            if started:
                tool, shown = _codex_cmd(cmd)   # 'Read' for a plain read, else 'shell'; unwrapped
                self._push([{"kind": "tool_use", "tool": tool,
                             "input": {"command": _cap(shown), "cwd": item.get("cwd")},
                             "toolId": iid}])
                self._maybe_steer()
            else:
                out = _txt(item.get("aggregatedOutput"))
                code = item.get("exitCode")
                err = (item.get("status") not in ("completed", "success")
                       or (code not in (0, None)))
                self._push([{"kind": "tool_result", "toolId": iid,
                             "content": _cap(out, RESULT_CAP), "isError": bool(err)}])
            return
        if it == "fileChange":
            # codex apply_patch: each change carries {path, kind(add/update/delete),
            # diff(unified)}. Surface it as an EDIT (file path + real diff) so it
            # shows as a "see Changes" marker + a diff card in the drawer — not a
            # generic exec card.
            files = []
            for ch in (item.get("changes") or []):
                if isinstance(ch, dict):
                    k = ch.get("kind")
                    kind = (k.get("type") if isinstance(k, dict) else k) or "update"
                    files.append({"path": ch.get("path") or "",
                                  "diff": ch.get("diff") or "", "kind": kind})
            if started:
                if len(files) == 1:
                    fp, body, kind = files[0]["path"], files[0]["diff"], files[0]["kind"]
                elif files:
                    fp, kind = "%d files" % len(files), "edit"
                    body = "\n".join("--- %s (%s) ---\n%s" % (f["path"], f["kind"], f["diff"])
                                     for f in files)
                else:
                    fp, body, kind = "apply_patch", "", "edit"
                self._push([{"kind": "tool_use", "tool": "apply_patch",
                             "input": {"file_path": fp, "diff": _cap(body, RESULT_CAP),
                                       "kind": kind, "n": len(files)},
                             "toolId": iid}])
                self._maybe_steer()
            else:
                st = item.get("status")
                if st not in ("completed", "success", "applied", None):
                    self._push([{"kind": "tool_result", "toolId": iid,
                                 "content": "patch %s" % st, "isError": True}])
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
                             "isError": bool(item.get("error"))}])
            return
        if it == "webSearch":
            if started:
                self._push([{"kind": "tool_use", "tool": "web_search",
                             "input": {"query": _txt(item.get("query"))}, "toolId": iid}])
            else:
                self._push([{"kind": "tool_result", "toolId": iid, "content": "", "isError": False}])
            return
        # other item types (imageView, imageGeneration, review modes, …) ignored for v1

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
        total = tu.get("total") or {}
        last = tu.get("last") or {}
        mx = tu.get("modelContextWindow")
        # Context-window occupancy = the LAST request's input tokens (the full
        # prompt the model saw, incl. cached history). NOT total.totalTokens —
        # that's the cumulative sum over the whole thread and runs to thousands of
        # percent on a long or resumed session (the "2508%" bug).
        cur = last.get("inputTokens")
        if cur is None:
            cur = last.get("totalTokens")
        if cur is not None and mx:
            cfg_mx = _configured_context_window()
            shown_mx = cfg_mx if cfg_mx and cfg_mx > mx else mx
            self.ctx = {"totalTokens": cur, "maxTokens": shown_mx,
                        "reportedMaxTokens": mx, "configuredMaxTokens": cfg_mx,
                        "percentage": round(cur * 100.0 / shown_mx, 1),
                        "model": self.display_model or self.model or None}
            self._emit({"type": "context", "ctx": self.ctx})
        li = last.get("inputTokens")
        lc = last.get("cachedInputTokens") or 0
        lo = last.get("outputTokens")
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
            cmd = _txt(p.get("command") or "")
            self._push([{"kind": "approval", "aid": aid, "tool": "shell",
                         "input": {"command": _cap(cmd) if cmd else None,
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
        self.log.extend(evs)
        if len(self.log) > 1500:
            self.log = self.log[-1500:]
        self._emit({"type": "events", "events": evs})

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
            elif k == "tool_use":
                lines.append("[tool: %s]" % (e.get("tool") or "?"))
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

    def _say(self, obj):
        try:
            self.write_message(json.dumps(obj))
        except Exception:
            pass

    def _broadcast_favorites(self):
        favs = list_favorites()
        for ws in list(ChatSocket.clients):
            ws._say({"type": "favorites", "favorites": favs})

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
                       "mode": sess.mode, "effort": sess.effort})
        elif mt == "attach":
            sess = CHAT_SESSIONS.get(msg.get("id"))
            if not sess:
                self._say({"type": "no_session", "id": msg.get("id")})
                return
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
                       "turn_age": sess.turn_age(), "word": sess.turn_word, "effort": sess.effort,
                       "compacting": sess.compacting})
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
                       "turn_age": sess.turn_age(), "word": sess.turn_word, "effort": sess.effort,
                       "compacting": sess.compacting, "resumed": True})
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
        elif mt == "rename":
            # Sidebar ✎: set/clear a custom label for a session (by codex thread id).
            cc = msg.get("cc")
            ok = set_name(cc, msg.get("name") or "")
            self._say({"type": "renamed", "cc": cc,
                       "name": (msg.get("name") or "").strip()[:120], "ok": bool(ok)})
            if ok:
                self._broadcast_favorites()   # a renamed session may be starred → refresh every device's list
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
                {"id": s.id, "cwd": s.cwd, "name": os.path.basename(s.cwd) or s.cwd,
                 "cc": s.cc_id, "model": s.model or "default", "display_model": s.display_model,
                 "mode": s.mode,
                 "effort": s.effort, "title": s.title(),
                 "busy": s.busy, "ended": s.ended}
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
  --acc:#4fc1ff;--usr:#3794ff;--add:#2ea043;--del:#f85149;--tool:#e0c080;--think:#7a7a7a;--onacc:#04121f}
:root[data-theme="light"]{
  --bg:#ffffff;--bg2:#f6f8fa;--bg3:#eaeef2;--line:#d0d7de;--fg:#1f2328;--mut:#656d76;
  --acc:#0969da;--usr:#0969da;--add:#1a7f37;--del:#cf222e;--tool:#9a6700;--think:#8a8a8a;--onacc:#ffffff}
:root[data-theme="dracula"]{
  --bg:#282a36;--bg2:#21222c;--bg3:#343746;--line:#44475a;--fg:#f8f8f2;--mut:#6272a4;
  --acc:#bd93f9;--usr:#8be9fd;--add:#50fa7b;--del:#ff5555;--tool:#f1fa8c;--think:#6272a4;--onacc:#282a36}
:root[data-theme="nord"]{
  --bg:#2e3440;--bg2:#2b303b;--bg3:#3b4252;--line:#434c5e;--fg:#d8dee9;--mut:#7b88a1;
  --acc:#88c0d0;--usr:#81a1c1;--add:#a3be8c;--del:#bf616a;--tool:#ebcb8b;--think:#69758c;--onacc:#2e3440}
:root[data-theme="solarized-light"]{
  --bg:#fdf6e3;--bg2:#eee8d5;--bg3:#e4ddc8;--line:#d6cfb8;--fg:#586e75;--mut:#93a1a1;
  --acc:#268bd2;--usr:#268bd2;--add:#859900;--del:#dc322f;--tool:#b58900;--think:#93a1a1;--onacc:#fdf6e3}
:root[data-theme="tokyo-night"]{
  --bg:#1a1b26;--bg2:#1f2335;--bg3:#292e42;--line:#3b4261;--fg:#c0caf5;--mut:#565f89;
  --acc:#7aa2f7;--usr:#7dcfff;--add:#9ece6a;--del:#f7768e;--tool:#e0af68;--think:#565f89;--onacc:#1a1b26}
:root[data-theme="catppuccin"]{
  --bg:#1e1e2e;--bg2:#181825;--bg3:#313244;--line:#45475a;--fg:#cdd6f4;--mut:#7f849c;
  --acc:#89b4fa;--usr:#89dceb;--add:#a6e3a1;--del:#f38ba8;--tool:#f9e2af;--think:#6c7086;--onacc:#1e1e2e}
:root[data-theme="gruvbox"]{
  --bg:#282828;--bg2:#1d2021;--bg3:#3c3836;--line:#504945;--fg:#ebdbb2;--mut:#a89984;
  --acc:#83a598;--usr:#8ec07c;--add:#b8bb26;--del:#fb4934;--tool:#fabd2f;--think:#928374;--onacc:#282828}
:root[data-theme="catppuccin-latte"]{
  --bg:#eff1f5;--bg2:#e6e9ef;--bg3:#dce0e8;--line:#ccd0da;--fg:#4c4f69;--mut:#8c8fa1;
  --acc:#1e66f5;--usr:#04a5e5;--add:#40a02b;--del:#d20f39;--tool:#df8e1d;--think:#8c8fa1;--onacc:#ffffff}
:root[data-theme="gruvbox-light"]{
  --bg:#fbf1c7;--bg2:#f2e5bc;--bg3:#ebdbb2;--line:#d5c4a1;--fg:#3c3836;--mut:#7c6f64;
  --acc:#458588;--usr:#689d6a;--add:#98971a;--del:#cc241d;--tool:#b57614;--think:#928374;--onacc:#fbf1c7}
:root[data-theme="rose-pine-dawn"]{
  --bg:#faf4ed;--bg2:#fffaf3;--bg3:#f2e9e1;--line:#dfdad9;--fg:#575279;--mut:#9893a5;
  --acc:#907aa9;--usr:#286983;--add:#5b8a3a;--del:#b4637a;--tool:#ea9d34;--think:#9893a5;--onacc:#faf4ed}
:root[data-theme="one-light"]{
  --bg:#fafafa;--bg2:#f0f0f0;--bg3:#e5e5e6;--line:#d4d4d6;--fg:#383a42;--mut:#a0a1a7;
  --acc:#4078f2;--usr:#0184bc;--add:#50a14f;--del:#e45649;--tool:#c18401;--think:#a0a1a7;--onacc:#ffffff}
:root[data-theme="ayu-light"]{
  --bg:#fcfcfc;--bg2:#f3f4f5;--bg3:#e7e8e9;--line:#dcdde0;--fg:#5c6166;--mut:#9ca0a6;
  --acc:#399ee6;--usr:#55b4d4;--add:#86b300;--del:#e65050;--tool:#f2ae49;--think:#9ca0a6;--onacc:#ffffff}
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
  --delfg:color-mix(in srgb, var(--del) 58%, var(--fg))}
/* THEME-END */
html,body{height:100%;background:var(--bg);color:var(--fg);overflow:hidden;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;font-size:14px}
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
#thinking .meta .mtx{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:1px}
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
.msg.asst .b{color:var(--fg)}
.think{color:var(--think);font-style:italic;font-size:13px;border-left:2px solid var(--line);padding:3px 0 3px 10px;margin-bottom:12px;white-space:pre-wrap}
.think.hide{display:none}
.notice{color:var(--mut);font-size:11.5px;margin:6px 0}
.errline{color:var(--del);font-size:12px;font-family:ui-monospace,monospace;margin:4px 0;white-space:pre-wrap}
.localstatus{border:1px solid var(--line);border-radius:8px;margin:8px 0 12px;background:var(--bg2);padding:9px 10px;font-size:12px;line-height:1.45}
.localstatus .sh{display:flex;align-items:center;gap:8px;color:var(--fg);font-weight:650;margin-bottom:7px}
.localstatus .sg{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:5px 14px}
.localstatus .sk{color:var(--mut);margin-right:6px}
.localstatus .sv{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
.localstatus .sf{margin-top:7px;color:var(--mut);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
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
.tool .tn{color:var(--tool);font-weight:600;font-family:ui-monospace,monospace;font-size:12.5px;flex-shrink:0}
.tool .tp{color:var(--mut);font-family:ui-monospace,monospace;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
.tool .cnt{font-size:11.5px;font-family:ui-monospace,monospace;flex-shrink:0}
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
pre{background:var(--codebg);border:1px solid var(--line);border-radius:6px;padding:8px;overflow-x:auto;margin:5px 0;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px;line-height:1.45}
code{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;background:var(--codebg);border:1px solid var(--line);border-radius:3px;padding:0 4px}
pre code{background:none;border:none;padding:0}
.bubble h1,.bubble h2,.bubble h3{font-size:14px;margin:8px 0 4px}
.bubble table{border-collapse:collapse;margin:7px 0;font-size:12px;display:block;overflow-x:auto;max-width:100%}
.bubble th,.bubble td{border:1px solid var(--line);padding:3px 9px;text-align:left}
.bubble thead th{background:var(--bg3);font-weight:600}
.msg.asst ul,.msg.asst ol{margin:4px 0 4px 20px}
.msg.asst a{color:var(--acc)}
.diffline{font-family:ui-monospace,monospace;font-size:12px;white-space:pre-wrap;line-height:1.4}
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
  transform:translateX(100%);transition:transform .2s;z-index:20;display:flex;flex-direction:column}
#drawer.open{transform:none}
#drresize{position:absolute;left:0;top:0;width:6px;height:100%;cursor:col-resize;background:transparent;transition:background .12s;z-index:30}
#drresize:hover,#drresize.drag{background:var(--acc)}
#drawer .dh{padding:8px 12px;background:var(--bg2);border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
#drawer .dh .grow{flex:1}
#drawer .dc{flex:1;overflow:auto;padding:10px}
.gfile{font-family:ui-monospace,monospace;font-size:12px;padding:1px 0}.gfile .st{display:inline-block;width:24px;color:var(--tool);font-weight:700}
.empty{color:var(--mut);padding:18px;text-align:center}
/* edits-out-of-chat */
.dh .tab{cursor:pointer;padding:3px 9px;border-radius:5px;color:var(--mut);font-size:12.5px;user-select:none}
.dh .tab.on{background:var(--bg3);color:var(--fg)}
.dh .tab span{font-size:10px;opacity:.8}
.emark{font-size:12px;color:var(--tool);background:var(--toolbg);border:1px solid var(--toolln);border-radius:6px;
  padding:3px 9px;margin:2px 0 12px;display:inline-flex;gap:7px;cursor:pointer;font-family:ui-monospace,monospace;align-items:center}
.emark:hover{filter:brightness(1.25)}
.emark .a{color:var(--addfg)}.emark .d{color:var(--delfg)}.emark .mut{color:var(--mut)}
.ecard{border:1px solid var(--line);border-radius:8px;margin-bottom:10px;background:var(--bg2);overflow:hidden}
.ecard .eh{padding:7px 9px;display:flex;gap:7px;align-items:center;background:var(--toolbg);border-bottom:1px solid var(--line)}
.ecard .ef{color:var(--tool);font-family:ui-monospace,monospace;font-size:12px;font-weight:600;flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ecard .cnt{font-size:11px;font-family:ui-monospace,monospace}.ecard .cnt .a{color:var(--addfg)}.ecard .cnt .d{color:var(--delfg)}
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
.approval .ah .tp{color:var(--mut);font-family:ui-monospace,monospace;font-weight:400;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
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
.ctx{display:none;align-items:center;gap:6px;font-size:12.5px;font-weight:600;color:var(--fg);white-space:nowrap;font-family:ui-monospace,monospace}
.usage{display:none;align-items:center;gap:6px;font-size:12.5px;font-weight:600;color:var(--fg);white-space:nowrap;font-family:ui-monospace,monospace;
  border-left:1px solid var(--line);padding-left:11px;margin-left:4px}
.ctx .ulabel,.usage .ulabel{opacity:.7}
.usage .useg{display:inline-flex;align-items:center;gap:5px}
.usage .useg + .useg::before{content:"|";opacity:.3;font-weight:400}   /* divider between 5h | 7d */
/* shared segmented meter: 5 cells × 20%, whole bar coloured by the total % (Context + Usage) */
.cells{display:inline-flex;gap:2px;align-items:center}
.cells .cell{width:7px;height:13px;border-radius:2px;background:var(--bg3);border:1px solid var(--line);box-sizing:border-box;transition:background .25s,box-shadow .25s}
.cells.lv-g{color:#2fbf4f}.cells.lv-y{color:#ecc020}.cells.lv-o{color:#ff8c1a}.cells.lv-r{color:#f5483b}
.cells .cell.on{background:currentColor;border-color:currentColor;box-shadow:0 0 4px currentColor}
@media(max-width:680px){.usage{display:none!important}}
#shell{flex:1;display:flex;min-height:0;position:relative}
#mainCol{flex:1;display:flex;flex-direction:column;min-width:0}
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
.acname{font-size:12px;font-family:ui-monospace,monospace;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.acpath{font-size:10px;font-family:ui-monospace,monospace;color:var(--mut);overflow-wrap:anywhere;line-height:1.3}
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
.srow .sdot{width:7px;height:7px;border-radius:50%;background:var(--mut);flex-shrink:0}
.srow .sdot.on{background:var(--add)}
.srow .sdot.busy{background:var(--tool);box-shadow:0 0 5px var(--tool);animation:pulse 1s infinite}
.srow .smeta{flex:1;min-width:0}
.srow .sname{font-size:12.5px;color:var(--fg);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.srow .ssub{font-size:11px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.srow .skebab{flex-shrink:0;font-size:18px;line-height:1;padding:1px 6px;color:var(--fg);cursor:pointer;opacity:.9;border-radius:5px;user-select:none}
.srow:hover .skebab{opacity:1}.srow .skebab:hover{color:var(--acc);background:var(--bg3)}
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
.seclist{max-height:266px;overflow-y:auto}
/* shared per-card action menu (⋯) */
#cardMenu{position:fixed;z-index:60;min-width:152px;background:var(--bg2);border:1px solid var(--line);border-radius:8px;box-shadow:0 8px 26px rgba(0,0,0,.55);padding:4px;display:none}
#cardMenu.on{display:block}
#cardMenu .mi{padding:7px 10px;font-size:12.5px;color:var(--fg);cursor:pointer;border-radius:5px;white-space:nowrap}
#cardMenu .mi:hover{background:var(--bg3)}
#cardMenu .mi.danger{color:var(--delfg)}#cardMenu .mi.danger:hover{background:var(--nobg)}
#cardMenu .cfgrow{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:5px 9px;font-size:12.5px;color:var(--mut)}
#cardMenu .cfgrow .cfgsel{background:var(--bg3);color:var(--fg);border:1px solid var(--line);border-radius:5px;padding:3px 6px;font-size:12px;cursor:pointer}
.fscope{font-size:10px;color:var(--mut);font-family:ui-monospace,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:130px}
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
.thmsg.tool .tht{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;color:var(--dim)}
.thmsg.target{background:var(--bg3)}
.thmsg .tht mark{background:var(--acc);color:var(--bg);border-radius:2px;padding:0 1px}
.thmore{display:block;width:calc(100% - 24px);margin:6px 12px;padding:5px;background:var(--bg3);
  color:var(--dim);border:1px dashed var(--line);border-radius:5px;font-size:11.5px;cursor:pointer}
.thmore:hover{color:var(--acc);border-color:var(--acc)}
.thend{text-align:center;font-size:11px;color:var(--mut);padding:6px}
#sb-backdrop{display:none}
@media(max-width:860px){
  #sidebar{position:fixed;left:0;top:0;bottom:0;z-index:40;transform:translateX(-100%);transition:transform .2s;width:min(310px,86vw);box-shadow:2px 0 14px rgba(0,0,0,.5)}
  #sidebar.open{transform:none}
  #sidebar.collapsed{display:flex}
  #sb-backdrop.show{display:block;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:35}
  #sbresize,#drresize{display:none}
}
.bubble .math.display{display:block;margin:6px 0;overflow-x:auto;overflow-y:hidden;max-width:100%}
.katex-display{margin:.35em 0!important}
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
      <div class="impline"><input type="file" id="impfile" accept=".jsonl,application/x-ndjson" hidden><button id="impbtn" title="adopt a Codex rollout exported from another machine">Import session</button><span id="impmsg"></span></div>
    </div>
    <div class="sb-sec">
      <div class="sb-h">Live <span id="liveN" class="cnt">0</span></div>
      <div id="liveList"><div class="sb-empty">none running</div></div>
    </div>
    <div class="sb-sec" id="secFav">
      <div class="sb-h sb-toggle"><span class="caret"></span>★ Favorites <span id="favN" class="cnt">0</span></div>
      <div id="favList" class="seclist"><div class="sb-empty">star a session to pin it here</div></div>
    </div>
    <div class="sb-sec" id="secRecent">
      <div class="sb-h sb-toggle"><span class="caret"></span>🕘 Recent <span class="grow"></span><span class="sb-ref" id="resumeRef" title="refresh">↻</span></div>
      <div id="recentList" class="seclist"><div class="sb-empty">—</div></div>
    </div>
    <div class="sb-sec" id="secFolder">
      <div class="sb-h sb-toggle"><span class="caret"></span>📁 In folder <span class="grow"></span><span id="folderScope" class="fscope"></span></div>
      <div id="folderList" class="seclist"><div class="sb-empty">—</div></div>
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
    <div id="chat"><div class="wrap" id="stream"></div></div>
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
let liveSessions=[], liveCCs=new Set(), recentData=[], folderData=[], favData=[], HOMEDIR='';
const EDIT_TOOLS=new Set(['Edit','MultiEdit','Write','NotebookEdit','apply_patch']);
const SKEY='al_session';

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
    if(lang==='math'||lang==='latex'||lang==='tex'){   /* fenced math (GitHub/Zulip ```math convention; codex emits this) → display equation */
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
function typesetMath(root){if(!window.katex)return;
  root.querySelectorAll('.math:not([data-done])').forEach(el=>_mathObserver().observe(el));}
function diffHtml(t){return t.split('\n').map(l=>{let c='dl-ctx';
  if(l.startsWith('@@')||l.startsWith('diff ')||l.startsWith('+++')||l.startsWith('---')||l.startsWith('***'))c='dl-hdr';
  else if(l.startsWith('+'))c='dl-add';else if(l.startsWith('-'))c='dl-del';
  return '<div class="diffline '+c+'">'+esc(l||' ')+'</div>';}).join('');}
let replaying=false;   /* true while bulk-replaying a session log on attach — suppresses
                          per-event atBottom/scroll so we don't force 1000s of reflows;
                          a single scroll-to-bottom runs once at the end instead. */
function atBottom(){if(replaying)return false;const c=$('#chat');return c.scrollHeight-c.scrollTop-c.clientHeight<140;}
function scroll(){if(replaying)return;const c=$('#chat');c.scrollTop=c.scrollHeight;}

const ICON={Edit:'✏️',MultiEdit:'✏️',Write:'📝',Bash:'▶',Read:'📖',Glob:'🔍',Grep:'🔍',Task:'🤖',
  WebFetch:'🌐',WebSearch:'🌐',TodoWrite:'☑️',NotebookEdit:'📓',
  apply_patch:'✏️',shell:'▶',web_search:'🌐'};   /* codex tools */
function primaryArg(i){if(!i)return '';if(typeof i==='string')return i.slice(0,80);
  if(i.file_path)return i.file_path.split('/').slice(-2).join('/');
  if(i.command)return (''+i.command).split('\n')[0].slice(0,90);
  if(i.pattern)return i.pattern;if(i.description)return i.description.slice(0,80);
  if(i.url)return i.url;return '';}
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
  if((t==='Bash'||t==='shell'||t==='Read')&&i.command)return '<pre><code>'+esc(i.command)+'</code></pre>';
  if(typeof i==='string')return '<pre><code>'+esc(i)+'</code></pre>';
  return '<pre><code>'+esc(JSON.stringify(i,null,2))+'</code></pre>';}

function addUser(text,nImg){const s=atBottom();const d=document.createElement('div');d.className='msg user';
  d.innerHTML='<div class="b">'+esc(text)+'</div>'+(nImg?'<div class="imgs">🖼 '+nImg+' image'+(nImg>1?'s':'')+' attached</div>':'');
  stream.appendChild(d);scroll();}
function addAsst(text){const s=atBottom();const d=document.createElement('div');d.className='msg asst';
  d.innerHTML='<div class="b bubble">'+md(text)+'</div>';typesetMath(d);stream.appendChild(d);if(s)scroll();}
function addThink(text){const s=atBottom();const d=document.createElement('div');d.className='think'+(showThink?'':' hide');d.dataset.t=1;
  d.textContent=text;stream.appendChild(d);if(s)scroll();}
function addNotice(t){const d=document.createElement('div');d.className='notice';d.textContent=t;stream.appendChild(d);}
function addStatus(ev){const s=atBottom(),st=ev.status||{},se=st.session||{},sv=st.service||{},g=st.git||{};
  const ctx=st.context||{},u=st.usage||{};
  const state=se.ended?'ended':(se.compacting?'compacting':(se.busy?'busy '+fmtSecs((se.turn_age||0)*1000):'ready'));
  let ctxText=ctx.percentage!=null?(ctx.percentage+'% · '+fmtTok(ctx.totalTokens)+' / '+fmtTok(ctx.maxTokens)):'unknown';
  if(ctx.reportedMaxTokens&&ctx.reportedMaxTokens!==ctx.maxTokens)ctxText+=' · reported '+fmtTok(ctx.reportedMaxTokens);
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
function addResult(ev){const c=tools[ev.toolId];if(!c)return;if(ev.isError)c.classList.add('err');
  const b=(ev.content||'').trim();c.querySelector('.res').innerHTML='<div class="reslabel">'+(ev.isError?'error ⤵':'output ⤵')+
    '</div><pre><code>'+esc(b.length>2200?b.slice(0,2200)+'\n…':b)+'</code></pre>';}

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
function route(ev){
  /* if activity resumes while we think we're idle (e.g. the CLI ran an injected
     queued message as its own turn), step back into the busy state */
  if(!running&&(ev.kind==='assistant_text'||ev.kind==='thinking'||ev.kind==='tool_use'))setBusy(true);
  if(ev.kind==='user_text')addUser(ev.text,ev.images);
  else if(ev.kind==='ready'){ready=true;cwd=ev.cwd||cwd;curCC=ev.session_id||curCC;
    if(!replaying)activeModel=(ev.model&&ev.model!=='default'?ev.model:(ev.display_model||activeModel));
    const modelName=ev.display_model||ev.model||'';if(modelName)setResolvedModel(modelName);
    addNotice('● session ready · '+modelName+(ev.effort?' · '+ev.effort+' effort':'')+' · '+(ev.cwd||''));}
  else if(ev.kind==='assistant_text')addAsst(ev.text);
  else if(ev.kind==='thinking')addThink(ev.text);
  else if(ev.kind==='tool_use'){if(EDIT_TOOLS.has(ev.tool)){addEditCard(ev);addMarker(ev);}else addTool(ev);}
  else if(ev.kind==='tool_result')addResult(ev);
  else if(ev.kind==='approval')addApproval(ev);
  else if(ev.kind==='approval_resolved')resolveApprovalCard(ev.aid,ev.allow,ev.always);
  else if(ev.kind==='question')addQuestion(ev);
  else if(ev.kind==='question_resolved')resolveQuestionCard(ev.aid,ev.answers);
  else if(ev.kind==='turn_start'){tokShow=false;setBusy(true,ev.word,0);}
  else if(ev.kind==='compacting'){compacting=true;setBusy(true,ev.word,0);}
  else if(ev.kind==='compacted'){compacting=false;addNotice(fmtCompacted(ev));if(ev.trigger!=='auto')setBusy(false);}
  else if(ev.kind==='turn_done'){compacting=false;setBusy(false);if(ev.done_word)addDone(ev.done_word,ev.dur_ms||0,ev.done_at);if(drawerOpen()&&gitTab())refreshGit();}
  else if(ev.kind==='queued')addQueued(ev);
  else if(ev.kind==='dequeued'||ev.kind==='unqueued')removeQueued(ev.qid);
  else if(ev.kind==='notice')addNotice(ev.text);
  else if(ev.kind==='status')addStatus(ev);
  else if(ev.kind==='recap')addRecap(ev.text);
}

/* persistent server-side session: attach / reattach / switch */
function markEnded(msg){ready=false;ta.disabled=true;sendBtn.disabled=true;$('#dot').className='dot';statset('ended');
  localStorage.removeItem(SKEY);if(msg)addNotice(msg);}
function onMsg(e){const m=JSON.parse(e.data);
  if(m.type==='started'){pendingStart=false;sid=m.id;cwd=m.cwd;bindProject(m.cwd);localStorage.setItem(SKEY,sid);loadDraft(sid);
    activeModel=(m.model&&m.model!=='default'?m.model:(m.display_model||'default'));ready=true;setBusy(false);ta.focus();setCurname(m.name||'session');setEffortPill(m.effort);renderCtx(null);statset('ready');
    addNotice('new session « '+(m.name||'')+' »'+(m.effort?' · '+m.effort+' effort':'')+' in '+m.cwd+' — type your first message to begin');reqList();loadPast();}
  else if(m.type==='attached'){clearUI();pendingStart=false;sid=m.id;curCC=m.cc||null;localStorage.setItem(SKEY,sid);cwd=m.cwd;bindProject(m.cwd);
    activeModel=(m.model&&m.model!=='default'?m.model:(m.display_model||'default'));ready=!m.ended;setCurname((m.title||m.name||'session')+(m.ended?' · ended':''));setEffortPill(m.effort);renderCtx(m.ctx);renderUsage(m.usage);statset(m.ended?'ended':'ready');
    replaying=true;m.events.forEach(route);replaying=false;
    compacting=!!m.compacting;setBusy(!!m.busy,m.word,(m.turn_age||0)*1000);loadDraft(sid);
    if(m.ended){markEnded('— this session has ended (history shown · you can resume it from disk) —');}
    else{ta.disabled=false;sendBtn.disabled=false;addNotice('— '+(m.resumed?'resumed':'reattached to')+' « '+(m.name||'')+' » ('+m.events.length+' events)'+(m.effort?' with '+m.effort+' effort':'')+' —');}
    scroll();requestAnimationFrame(scroll);reqList();loadPast();}
  else if(m.type==='no_session'){localStorage.removeItem(SKEY);sid=null;ready=false;activeModel='default';setBusy(false);setCurname('');renderCtx(null);statset('idle');
    addNotice('that session is no longer running — pick it under “Resume from disk”, or ＋ New.');reqList();loadPast();}
  else if(m.type==='events')m.events.forEach(route);
  else if(m.type==='stderr')addErr(m.text);
  else if(m.type==='error'){pendingStart=false;addErr('⚠ '+m.error);}
  else if(m.type==='exit'){if(!pendingStart){markEnded('session process exited (code '+m.code+')');setCurname('');}reqList();loadPast();}
  else if(m.type==='ended'){dropDraft(m.id);if(m.id&&m.id===sid){sid=null;activeModel='default';setCurname('');markEnded('session ended');}reqList();loadPast();}
  else if(m.type==='resumable_deleted'){addNotice(m.ok?'🗑 session moved to trash':('delete failed: '+(m.error||'?')));loadPast();}
  else if(m.type==='renamed'){if(m.ok){if(m.cc&&m.cc===curCC&&m.name)setCurname(m.name);addNotice('✎ renamed');reqList();loadPast();}else addNotice('rename failed');}
  else if(m.type==='sessions')renderLive(m.sessions);
  else if(m.type==='context')renderCtx(m.ctx);
  else if(m.type==='usage')renderUsage(m.usage);
  else if(m.type==='tokens'){tokUp=m.up||0;tokOut=m.out||0;tokShow=true;
    if(m.word!=null&&m.word!==lastWordSeed){lastWordSeed=m.word;if(running&&!compacting)setWord(m.word);}}
  else if(m.type==='favorites'){favData=m.favorites||[];renderPast();}
}
function openWs(cb){const proto=location.protocol==='https:'?'wss:':'ws:';
  ws=new WebSocket(proto+'//'+location.host+'/ws/chat');
  ws.onopen=()=>{clearTimeout(reconnectT);$('#dot').className='dot '+(ready?'on':'');if(cb)cb();reqList();maybeMigrateFavs();
    const saved=localStorage.getItem(SKEY);if(saved&&!sid&&!pendingStart){statset('reattaching…');ws.send(JSON.stringify({type:'attach',id:saved}));}};
  ws.onclose=()=>{$('#dot').className='dot';statset('disconnected');ta.disabled=true;sendBtn.disabled=true;
    clearTimeout(reconnectT);reconnectT=setTimeout(()=>openWs(),1800);};
  ws.onmessage=onMsg;}
function wsSend(o){if(ws&&ws.readyState===1)ws.send(JSON.stringify(o));}
function reqList(){wsSend({type:'list'});}
function reltime(ts){const s=(Date.now()/1000)-ts;if(s<60)return Math.round(s)+'s';
  if(s<3600)return Math.round(s/60)+'m';if(s<86400)return Math.round(s/3600)+'h';return Math.round(s/86400)+'d';}
function setCurname(t){$('#curname').textContent=t||'— no session —';}
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
function renderCtx(c){const el=$('#ctx');
  if(!c||c.percentage==null){el.style.display='none';return;}
  const pct=Math.round(c.percentage);
  el.className='ctx';el.style.display='inline-flex';
  el.innerHTML='<span class="ulabel">Context</span>'+cellBar(pct)+'<span>'+pct+'%</span>';
  let title='context '+(c.totalTokens||'?')+' / '+(c.maxTokens||'?')+' tokens ('+pct+'%)'+(c.model?' · '+c.model:'');
  if(c.configuredMaxTokens&&c.configuredMaxTokens!==c.reportedMaxTokens)title+=' · configured '+fmtTok(c.configuredMaxTokens);
  if(c.reportedMaxTokens&&c.reportedMaxTokens!==c.maxTokens)title+=' · reported '+fmtTok(c.reportedMaxTokens);
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

/* collapsible sidebar sections (Favorites / Recent / In folder) */
const SECKEY='al_seccol';
function toggleSec(id){const el=$('#'+id);if(!el)return;el.classList.toggle('collapsed');
  let c={};try{c=JSON.parse(localStorage.getItem(SECKEY)||'{}');}catch(e){}
  c[id]=el.classList.contains('collapsed');localStorage.setItem(SECKEY,JSON.stringify(c));}
function applySecCollapse(){let c={};try{c=JSON.parse(localStorage.getItem(SECKEY)||'{}');}catch(e){}
  ['secFav','secRecent','secFolder'].forEach(id=>{const el=$('#'+id);if(el)el.classList.toggle('collapsed',!!c[id]);});}

function renderLive(list){const box=$('#liveList');
  liveSessions=list||[];
  list=liveSessions;
  liveCCs=new Set(liveSessions.map(s=>s.cc).filter(Boolean));
  $('#liveN').textContent=list.length;
  if(!list.length){box.innerHTML='<div class="sb-empty">none running — pick a project, ＋ New</div>';}
  else{box.innerHTML='';list.forEach(s=>{
    const r=document.createElement('div');
    r.className='srow'+(s.id===sid?' active':'')+(s.ended?' ended':'');
    const proj=(s.cwd||'').split('/').slice(-2).join('/');
    const dot=s.busy?'busy':(s.ended?'':'on');
    r.innerHTML='<span class="sdot '+dot+'"></span><div class="smeta">'+
      '<div class="sname">'+esc(s.title||s.name||'new session')+(s.ended?' · ended':'')+'</div>'+
      '<div class="ssub">'+esc(proj)+(s.busy?' · working…':'')+'</div></div>'+
      '<span class="skebab" title="more">⋮</span>';
    r.querySelector('.smeta').onclick=()=>switchSession(s.id);
    r.querySelector('.sdot').onclick=()=>switchSession(s.id);
    r.querySelector('.skebab').onclick=ev=>{ev.stopPropagation();const kebab=ev.currentTarget;const items=[
      {label:'⚙ Configure',fn:()=>openConfigure(s,kebab)},
      {label:'✎ Rename',fn:()=>renameSession(s.cc,s.title||s.name)}];
      if(s.cc){items.push({label:'⤓ Export transcript',fn:()=>exportSession(s.cc)});
        items.push({label:isFav(s.cc)?'★ Unfavorite':'☆ Favorite',fn:()=>toggleFav(s)});}
      items.push({label:'✕ End session',danger:true,fn:()=>endSessionById(s.id,s.name)});
      toggleCardMenu(ev.currentTarget,items);};
    box.appendChild(r);});}
  renderPast();
}

/* favorites: starred sessions, persisted SERVER-SIDE by claude session id so every
   device shares one list. favData mirrors the server; the server pushes it on connect
   and re-broadcasts on every change. */
const FKEY='al_favs';   /* legacy per-device store — read once to migrate, then ignored */
function getFavs(){return favData;}
function isFav(cc){return favData.some(f=>f.cc===cc);}
function toggleFav(s){
  if(isFav(s.cc)){favData=favData.filter(f=>f.cc!==s.cc);
    wsSend({type:'set_favorite',cc:s.cc,fav:false});}
  else{favData=[{cc:s.cc,cwd:s.cwd||'',name:s.name||'',title:s.title||''},...favData];
    wsSend({type:'set_favorite',cc:s.cc,fav:true,cwd:s.cwd||'',name:s.name||'',title:s.title||''});}
  renderPast();}
function maybeMigrateFavs(){   /* one-time: lift this device's old localStorage stars to the server */
  if(localStorage.getItem('al_favs_migrated'))return;
  if(!ws||ws.readyState!==1)return;        /* need the socket open; onopen retries */
  let old=[];try{old=JSON.parse(localStorage.getItem(FKEY)||'[]');}catch(e){}
  old.forEach(f=>{if(f&&f.cc)wsSend({type:'set_favorite',cc:f.cc,fav:true,
    cwd:f.cwd||'',name:f.name||'',title:f.title||''});});
  localStorage.setItem('al_favs_migrated','1');}

/* a past-session row: click to resume; star toggles favorite */
function pastRow(s,fav){const r=document.createElement('div');r.className='srow';
  const proj=(s.cwd||'').split('/').slice(-2).join('/');
  const sub=(fav?'':'↺ ')+esc(proj)+(s.mtime?(' · '+reltime(s.mtime)):'');
  r.innerHTML='<div class="smeta">'+
    '<div class="sname">'+esc(s.title||proj||'session')+'</div><div class="ssub">'+sub+'</div></div>'+
    '<span class="skebab" title="more">⋮</span>';
  r.querySelector('.smeta').onclick=()=>resumeSession(s);
  r.querySelector('.skebab').onclick=ev=>{ev.stopPropagation();toggleCardMenu(ev.currentTarget,[
    {label:'✎ Rename',fn:()=>renameSession(s.cc,s.title)},
    {label:'⤓ Export transcript',fn:()=>exportSession(s.cc)},
    {label:fav?'★ Unfavorite':'☆ Favorite',fn:()=>toggleFav(s)},
    {label:'🗑 Delete (to trash)',danger:true,fn:()=>delResumable(s)}]);};
  return r;}
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
  favData=favData.filter(f=>f.cc!==s.cc);
  recentData=(recentData||[]).filter(x=>x.cc!==s.cc);
  folderData=(folderData||[]).filter(x=>x.cc!==s.cc);
  renderPast();}

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
        if(ok&&j.ok){impMsg('imported · resume it from the list below');loadPast();}
        else impMsg(((j&&j.error)||'import failed'),true);})
      .catch(e=>impMsg(String(e),true))
      .finally(()=>{inp.value='';});};}

function renderPast(){
  const favs=getFavs(),favCC=new Set(favs.map(f=>f.cc));
  const fb=$('#favList');$('#favN').textContent=favs.length;
  if(!favs.length)fb.innerHTML='<div class="sb-empty">star a session to pin it here</div>';
  else{fb.innerHTML='';favs.forEach(f=>fb.appendChild(pastRow(f,true)));}
  const rec=(recentData||[]).filter(s=>!liveCCs.has(s.cc)&&!favCC.has(s.cc)).slice(0,30);
  const rb=$('#recentList');
  if(!rec.length)rb.innerHTML='<div class="sb-empty">no recent sessions</div>';
  else{rb.innerHTML='';rec.forEach(s=>rb.appendChild(pastRow(s,false)));}
  const fol=(folderData||[]).filter(s=>!liveCCs.has(s.cc)&&!favCC.has(s.cc)).slice(0,30);
  const ob=$('#folderList');
  if(!fol.length)ob.innerHTML='<div class="sb-empty">no past sessions in this folder</div>';
  else{ob.innerHTML='';fol.forEach(s=>ob.appendChild(pastRow(s,false)));}
}
function currentFolder(){const p=$('#project').value;
  return p==='__custom__'?$('#cwd').value.trim():(p||'');}
function loadPast(){const folder=currentFolder();
  $('#folderScope').textContent=folder?(folder.split('/').filter(Boolean).slice(-1)[0]||folder):'';
  fetch('api/resumable').then(r=>r.json()).then(j=>{recentData=j.resumable||[];renderPast();}).catch(()=>{});
  if(folder)fetch('api/resumable?cwd='+encodeURIComponent(folder)).then(r=>r.json()).then(j=>{folderData=j.resumable||[];renderPast();}).catch(()=>{});
  else{folderData=[];renderPast();}}

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
  $('#cwd').focus();loadPast();acQuery();}
function acMove(d){const els=$('#cwdac').querySelectorAll('.acitem');if(!els.length)return;
  acSel=(acSel+d+els.length)%els.length;els.forEach((el,i)=>el.classList.toggle('sel',i===acSel));els[acSel].scrollIntoView({block:'nearest'});}
function acQuery(){clearTimeout(acTimer);const q=$('#cwd').value;
  acTimer=setTimeout(()=>fetch('api/dircomplete?q='+encodeURIComponent(q)).then(r=>r.json()).then(acRender).catch(acClose),130);}

function switchSession(id){if(!id||id===sid)return;saveDraft();clearUI();statset('switching…');wsSend({type:'attach',id:id});
  if(window.innerWidth<=860)closeSidebar();}
function resumeSession(s){if(!s||!s.cc)return;saveDraft();clearUI();pendingStart=true;sid=null;statset('resuming…');
  const go=()=>wsSend({type:'resume',cc:s.cc,cwd:s.cwd,model:$('#model').value,mode:$('#mode').value});
  if(ws&&ws.readyState===1)go();else openWs(go);
  if(window.innerWidth<=860)closeSidebar();}
function newSession(){const proj=$('#project').value;const dir=proj==='__custom__'?$('#cwd').value.trim():proj;
  if(!dir){addErr('pick a project directory first');return;}
  /* keep any current session alive in the background — just spin up another */
  const model=$('#model').value,effort=effortForModel(curEffort,model);
  curEffort=effort;localStorage.setItem('al_effort',effort);setEffortPill(effort);
  saveDraft();clearUI();pendingStart=true;sid=null;
  const start=()=>wsSend({type:'start',cwd:dir,model:model,mode:$('#mode').value,effort:effort});
  if(ws&&ws.readyState===1)start();else openWs(start);statset('starting…');
  if(window.innerWidth<=860)closeSidebar();}
function endSessionById(id,name){if(!id)return;
  if(!confirm('End session '+(name?'« '+name+' »':'')+'?\nIts codex process stops; you can still resume it from disk later.'))return;
  wsSend({type:'end',id:id});
  if(id===sid){sid=null;setCurname('');markEnded('session ended');}
  reqList();loadPast();}
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
  if(e.key==='Escape'&&running&&!srchOpen()&&!$('#cwdac').classList.contains('on')){e.preventDefault();doInterrupt();}});
$('#newbtn').onclick=newSession;
/* color theme: apply + persist (the <head> script already set it pre-paint) */
function applyTheme(t){if(t&&t!=='dark')document.documentElement.setAttribute('data-theme',t);
  else document.documentElement.removeAttribute('data-theme');}
(function(){const t=localStorage.getItem('al_theme')||'dark';const sel=$('#theme');
  if(sel){sel.value=t;sel.onchange=()=>{const v=sel.value;localStorage.setItem('al_theme',v);applyTheme(v);};}
  applyTheme(t);})();
$('#navtoggle').onclick=toggleSidebar;
$('#sb-backdrop').onclick=closeSidebar;
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
$('#resumeRef').onclick=e=>{e.stopPropagation();loadPast();};
/* collapsible sections: clicking the header toggles; restore saved state */
['secFav','secRecent','secFolder'].forEach(id=>{
  const h=$('#'+id+' .sb-h');if(h)h.onclick=()=>toggleSec(id);});
applySecCollapse();
/* dismiss the ⋯ card menu on outside-click, Escape, scroll or resize */
document.addEventListener('click',e=>{if(!e.target.closest('#cardMenu')&&!e.target.closest('.skebab'))closeCardMenu();});
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeCardMenu();});
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
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&drawerOpen())$('#drawer').classList.remove('open');});
$('#grefresh').onclick=refreshGit;
$('#project').onchange=()=>{const c=$('#project').value==='__custom__';
  $('#cwdwrap').classList.toggle('show',c);
  if(c){if(!$('#cwd').value)$('#cwd').value=HOMEDIR?(HOMEDIR+'/'):'';$('#cwd').focus();acQuery();}else acClose();
  loadPast();};
$('#cwd').addEventListener('input',acQuery);
$('#cwd').addEventListener('focus',acQuery);
$('#cwd').addEventListener('change',loadPast);
$('#cwd').addEventListener('blur',()=>setTimeout(acClose,160));
$('#cwd').addEventListener('keydown',e=>{const b=$('#cwdac');if(!b.classList.contains('on'))return;
  if(e.key==='ArrowDown'){e.preventDefault();acMove(1);}
  else if(e.key==='ArrowUp'){e.preventDefault();acMove(-1);}
  else if(e.key==='Enter'&&acSel>=0){e.preventDefault();acPick(acSel);}
  else if(e.key==='Escape')acClose();});

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
  loadPast();
}catch(e){}})();

setInterval(()=>reqList(),8000);
setInterval(loadPast,30000);
loadPast();
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
    app = tornado.web.Application([
        (r"/", ConsoleHandler),
        (r"/console", ConsoleHandler),
        (r"/api/projects", ProjectsHandler),
        (r"/api/resumable", ResumableHandler),
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
