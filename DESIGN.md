# Design

## Source of truth
- Status: Active
- Last refreshed: 2026-09-09
- Primary product surfaces: session sidebar, session tabs, chat stream, composer, changes drawer
- Evidence reviewed: `README.md`, inline HTML/CSS/JS in `codex_console.py`, `faster_whisper_worker.py`, current multi-session behavior

## Brand
- Personality: quiet, technical, work-focused
- Trust signals: explicit session state, visible model/context, clear distinction between closing a view and ending a process
- Avoid: marketing layouts, decorative cards, oversized controls, ambiguous destructive actions

## Product goals
- Goals: make several Codex sessions easy to keep running and switch between; preserve project organization; keep active work visible
- Non-goals: restore every Codex process after service restart; replace the project/session sidebar; emulate browser chrome literally
- Success signals: a live session opens once, subsequent switches do not resume it, and closing its tab does not end it

## Personas and jobs
- Primary personas: developers and researchers running several project-scoped Codex threads
- User jobs: monitor parallel work, switch contexts quickly, retain drafts, dictate editable prompts on mobile or desktop, and deliberately end only the sessions they no longer need
- Key contexts of use: desktop as primary; narrow tablet/mobile for occasional monitoring

## Information architecture
- Primary navigation: left sidebar for all projects and live/history sessions; top tab strip for the current working set
- Core routes/screens: one console screen with session-scoped chat and changes state
- Content hierarchy: tabs and session status, chat/plan, composer, secondary changes/subagent panels

## Design principles
- Preserve process state: navigation must not imply termination
- Separate discovery from working set: LIVE lists everything running; tabs list what is open in this browser
- Make state legible: active, busy, unread, disconnected, and ended states must differ without relying only on color
- Tradeoffs: prefer a compact tab strip and lightweight reattach over duplicating the full console in nested views

## Visual language
- Color: reuse existing theme variables and status colors
- Typography: reuse UI and monospace fonts; compact labels with no viewport-scaled type
- Spacing/layout rhythm: 4-8px control spacing; preserve the current 820px chat measure
- Shape/radius/elevation: existing 5-8px radii; tabs are navigation, not cards
- Motion: restrained state transitions; honor reduced-motion preferences
- Imagery/iconography: existing symbols and compact status dots; no decorative imagery

## Components
- Existing components to reuse: LIVE session rows, busy dots, close/menu controls, session title, context/status header
- New/changed components: horizontal session tab strip labeled only by session name; tab activation and close-view actions; compact older-content marker with a history-search action; collapsible sticky Plan dock; collapsible consecutive tool-call groups; semantic Agent tool cards that summarize collaboration actions without exposing empty protocol fields; composer attachment button with image thumbnails and compact text/code file previews; fixed-size microphone control with recording, live-revision, finalization, and cancellation states
- Variants and states: active, background-ready, background-busy, unread, stale/ended; Plan expanded or collapsed to active work only; tool groups collapsed or expanded with running and failed counts
- Token/component ownership: inline CSS and JS in `codex_console.py`; no new dependency or design-system layer

## Accessibility
- Target standard: practical WCAG 2.1 AA behavior for navigation and controls
- Keyboard/focus behavior: tabs use tab semantics, arrow-key navigation, visible focus, and a named close button; Alt+M toggles voice recording; stopping from the microphone button returns focus to the originating composer so Enter can send the reviewed transcript
- Contrast/readability: reuse tested theme colors and add text/symbol state alongside color
- Screen-reader semantics: `role="tablist"`, `role="tab"`, `aria-selected`, descriptive close labels
- Reduced motion and sensory considerations: no required animation for state comprehension

## Responsive behavior
- Supported breakpoints/devices: existing desktop layout and `max-width: 860px` mobile layout
- Layout adaptations: tabs scroll horizontally and truncate session names; they have no project subtitle and never wrap or resize the chat/composer
- Touch/hover differences: close, attachment, and microphone controls remain large enough for touch; tooltips are supplemental; mobile users choose images or files through the system picker while desktop paste remains available

## Interaction states
- New session: a modal confirms the working directory, model, effort and permissions. Recent folders and a home-scoped server-side folder browser are available without repository scanning. Opening, cancelling or failing validation preserves the active session and draft; switch views only after a correlated successful start response. Escape closes the modal without interrupting a running turn.
- Folder selection: the sidebar has no duplicate directory picker. New sessions choose their directory in the modal; importing a transcript first chooses its destination through the same folder browser. Search scope follows the active session, not an unconfirmed dialog selection.
- Folder creation: an inline name form creates a single child of the currently browsed folder, then opens it for selection. Never overwrite an existing entry or initialize a repository. Validate names and home containment on the server, accept only JSON and reject cross-origin browser writes, and lock navigation during creation to prevent duplicate or misdirected requests.
- Creation settings: model, catalog refresh, effort and permission controls live in the New session modal, not the sidebar. Persist model/permission defaults only after successful creation; cancelling leaves active-session settings untouched. Refresh the open modal's catalog without discarding an available selection.
- Tool groups: the outer summary uses an accent-tinted header and left border; child calls are numbered, indented rows along a vertical guide. Expanded child headers and inset results have separate surfaces. Preserve independent group/call expansion and existing error colors across themes.
- Subagents: one expandable activity card per child thread; refresh only open cards and preserve detail scroll position. Terminal child turns become completed or errored instead of remaining active.
- Background terminals: a compact modal lists the attached session's commands, with refresh and per-command stop confirmation. Ignore responses belonging to a different session; unavailable interfaces show an explicit error.
- Session controls: model and service-tier selectors sit beside the effort control; options follow the installed CLI catalog and apply to the next turn. New and resumed sessions explicitly select Standard independently of CLI configuration; Fast is opt-in when advertised.
- Context: use Codex's per-thread runtime accounting and effective window when available, with its auto-compaction threshold in the tooltip. Read local diagnostics without changing logging configuration; never substitute cumulative billing tokens. Mark request-usage fallback with `~`, invalidate samples on compaction/model changes, and ignore late samples for ended or replaced sessions.
- Questions: explicit server requests use their matching RPC response. Assistant messages with explicit async delivery use a separate pending card and send one correlated turn/steer or turn/start answer, without an extra queued message or live user bubble. Other assistant-message question metadata remains non-actionable.
- Question lifecycle: turn completion expires turn-scoped RPC cards but preserves asynchronous message-backed questions. Interruption and termination dismiss both. Dismiss skips questions without stopping the turn. Submit and Dismiss lock while sending; disconnected attempts show inline feedback.
- Question input: single-choice options and custom answers are mutually exclusive. Custom text is offered only with `isOther` or no options, grows to 124px, submits on Enter, and inserts a newline on Shift+Enter; IME composition is not submitted.
- Reading: upward scrolling leaves tail-follow mode immediately; returning to the bottom resumes it. A floating down-arrow appears when reading older messages and jumps to the latest message, restoring tail-follow. Each tab preserves its reading position and follow state; revealing a tab scrolls only the tab strip.
- Updates: a page-version mismatch exposes a reload action; preserve text drafts and defer reload while recording or holding draft attachments.
- Loading: newly resumed session tab shows its existing switching/resuming status; pressing the microphone begins model warmup concurrently with permission and initial audio capture
- Empty: hide the strip when no tab is open
- Error: failed attach removes stale live tabs and leaves the user in an explicit no-session state; microphone and final transcription failures retain the last usable draft; repeated live-preview failures fall back to final-only transcription without stopping the recording
- Resume errors never create a replacement thread. An active-writer conflict asks the user to close the session in the other Codex process and retry; failed startup processes are reaped, and resumed thread IDs must match the requested session.
- Success: activating a live tab updates chat, project binding, draft, model, and context; long sessions retain a bounded recent DOM window with explicit access to indexed history; a collapsed Plan shows only current active work and updates in place; a fully completed Plan remains visible briefly and then clears itself; adjacent calls of the same tool collapse into one summary while preserving each original call and output on expansion; subagent wait/start/input/close actions use concise labels and reserve detailed child output for the Subagents panel; pasted images and system-picked image/text/code attachments share validation, preview, draft, and send behavior; voice recording writes revisable hypotheses into the originating session draft, preserves a confirmed prefix between updates, runs a full final correction before unlocking send, restores the original selection on cancel, and never submits automatically
- Disabled: ended/stale tabs cannot send messages
- Offline/slow network: tabs remain visible while the socket reconnects; no session is ended by disconnect

## Content voice
- Tone: concise and operational
- Terminology: tab means an open view; session means the running Codex process/thread; end means terminate
- Microcopy rules: use "Close tab" for view-only removal and "End session" for process termination

## Implementation constraints
- Framework/styling system: primary Python server with inline vanilla HTML/CSS/JS plus an optional isolated transcription worker
- Design-token constraints: reuse existing CSS custom properties
- Performance constraints: restore an opened tab from its in-memory DOM cache before networking; use sequenced incremental `attach`, not `resume` or full replay, to catch up background activity; bound each chat DOM by rendered-item and text-size budgets while never pruning unresolved interactive cards or tool groups containing running calls; keep at most one live transcription request in flight and coalesce newer recorder chunks behind it
- Compatibility constraints: existing sidebar, drafts, approvals, model/context state, and service restart behavior must remain intact; microphone capture requires a secure browser context while transcription remains an optional local backend
- Test/screenshot expectations: source-level regression tests, JavaScript syntax check, unit suite, and desktop/mobile visual smoke check when practical

## Open questions
- None currently.
