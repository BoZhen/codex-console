# Codex Console

An interactive, browser-driven GUI for **OpenAI's Codex CLI** that separates the
two things a raw terminal tangles together:

- **Discussion** — what you asked / what the agent said (prose only)
- **Code & file changes** — every tool call (shell commands, patches) rendered as
  **collapsed cards** (`▸ shell  cat note.txt`) you expand on demand, plus the
  live `git diff` of the working directory (ground truth)

It drives Codex through the **`codex app-server`** JSON-RPC protocol (the same
backend the editor integrations use), keeps one persistent **codex thread** alive
per chat, feeds your messages as turns, and renders the typed event stream back —
a Codex-style chat where code and conversation stay visually separated.

## Features

- **Chat / code split** — prose in the stream; tool calls (shell / apply_patch /
  MCP) as collapsible change cards. Switch to the live **Git diff** tab for the
  whole working tree.
- **Multi-session sidebar** — Live / Favorites / Recent / In-folder sections.
  Each card has a `⋮` menu: rename, favorite, delete, end.
- **Resumable sessions** — reopen a project and pick up a previous Codex thread
  (rollouts under `~/.codex/sessions` restore history and the right `cwd`).
- **Interactive approvals** — when the approval policy is `🔐 Approve`, each
  shell command / file change surfaces a per-action prompt (**Approve** /
  **Approve & don't ask again this session** → `acceptForSession` / **Deny**).
  Tool user-input questions render as in-browser cards.
- **Message queue / steering** — type while the agent is busy; the message is
  injected into the running turn at the next tool boundary (`turn/steer`).
- **Reasoning effort** — a `🧠` pill (minimal / low / medium / high / xhigh).
  Codex effort is per-turn, so a change applies on the next turn (no relaunch).
- **Approval & sandbox presets** — a single picker maps to codex's
  `approvalPolicy` × `sandbox`: 🔐 Approve, ⚡ Auto (sandbox), 👁 Read-only,
  🔓 Full access.
- **Live meters** — context-window usage (from `thread/tokenUsage/updated`) and
  rolling 5-hour / weekly limits (from `account/rateLimits/updated`) in the
  header; a floating status pill with live ↑/↓ token counts.
- **Image paste**, **LaTeX (KaTeX)**, **13 color themes**, **`/compact`**.
- **Local file links** — absolute or `~/...` file paths in assistant messages
  become links to a sibling web-file-manager instance for preview/download.

## Run

```bash
# any python with tornado works; defaults to localhost-only (safe):
python codex_console.py

# reach it from your phone/iPad on the LAN, WITH auth:
CODEX_CONSOLE_BIND=0.0.0.0 CODEX_CONSOLE_AUTH=user:change-this-password python codex_console.py
```

Open `http://<host>:7704`, pick a project dir, and start chatting.

| Env | Default | Meaning |
|---|---|---|
| `CODEX_CONSOLE_PORT` | `7704` | listen port |
| `CODEX_CONSOLE_BIND` | `127.0.0.1` | bind address; set `0.0.0.0` for LAN access |
| `CODEX_CONSOLE_AUTH` | *(disabled)* | optional HTTP Basic Auth `user:pass` |
| `CODEX_CONSOLE_CODEX` | *(auto)* | path to the codex binary (else auto-resolved) |
| `CODEX_CONSOLE_WEBFM_URL` | same host, port `7701` | web-file-manager base URL for local file links |
| `CODEX_CONSOLE_RECAP` | `0` | enable idle away-summary recap cards |
| `CODEX_CONSOLE_RECAP_IDLE_SEC` | `300` | idle seconds before a recap is generated |
| `CODEX_CONSOLE_RECAP_MODEL` | `gpt-5.3-codex-spark` | model used for recap generation |
| `CODEX_CONSOLE_RECAP_TIMEOUT_SEC` | `45` | timeout for the one-shot recap generator |

### The codex binary

Codex Console spawns `codex app-server`. The npm package installs `codex` as a
**node wrapper** (`~/.local/bin/codex` → `codex.js`), which needs node + its
platform package resolvable from the spawn context — that can break when a
background process (with a minimal `PATH`) launches it. So the console
**auto-resolves the native ELF binary** under
`…/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/*/bin/codex`
(matching your installed version) and drives it directly. Override with
`CODEX_CONSOLE_CODEX=/path/to/native/codex` if needed.

> [!WARNING]
> This **drives Codex** in the directory you pick — it can read/write files and
> run commands there — and it exposes your **chat history and source diffs**. Do
> not expose it on an untrusted network without `CODEX_CONSOLE_AUTH` and a
> trusted boundary (VPN / SSH tunnel / reverse proxy + TLS).

## Notes

- Intentionally a single `codex_console.py` with inline HTML/CSS/JS — **no build
  step**.
- [KaTeX](https://katex.org/) is vendored under `static/katex/` (MIT) for offline
  math rendering.
- Per-session prefs/names live under `~/.codex/console-*.json`.

## License

[MIT](LICENSE)
