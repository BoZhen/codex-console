# Python SDK Evaluation

Evaluated `openai-codex 0.147.0` against Codex CLI `0.153.4`.

The published wheel requires Python 3.10+, Pydantic 2.12+, and pins
`openai-codex-cli-bin==0.147.0`. Explicit `CodexConfig(codex_bin=...)` allows
using the installed CLI. An isolated `AsyncCodex` handshake and `models()`
request succeeded against CLI 0.153.4.

The lower-level asynchronous client provides typed requests and notification
reading. However, `CodexClient._reader_loop` invokes the approval handler
synchronously before reading further messages. Waiting for a browser response
inside that handler would pause notification and RPC response processing.
The high-level API also does not expose typed helpers for the background
terminal and paginated thread-item methods used by this console.

Keep the existing app-server transport for now. Revisit migration when deferred
browser approval responses and concurrent event delivery can be handled through
public SDK APIs, with coverage for detach/reattach, questions, interruption, and
new protocol fields. This evaluation did not execute a model turn or change the
console's installed dependencies.

References:
- [Official SDK documentation](https://learn.chatgpt.com/docs/codex-sdk)
- [Published package](https://pypi.org/project/openai-codex/0.147.0/)
