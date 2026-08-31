# Design

## Source of truth
- Status: Active
- Last refreshed: 2026-08-31
- Primary product surfaces: session sidebar, session tabs, chat stream, composer, changes drawer
- Evidence reviewed: `README.md`, inline HTML/CSS/JS in `codex_console.py`, current multi-session behavior

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
- User jobs: monitor parallel work, switch contexts quickly, retain drafts, and deliberately end only the sessions they no longer need
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
- New/changed components: horizontal session tab strip labeled only by session name; tab activation and close-view actions
- Variants and states: active, background-ready, background-busy, unread, stale/ended
- Token/component ownership: inline CSS and JS in `codex_console.py`; no new dependency or design-system layer

## Accessibility
- Target standard: practical WCAG 2.1 AA behavior for navigation and controls
- Keyboard/focus behavior: tabs use tab semantics, arrow-key navigation, visible focus, and a named close button
- Contrast/readability: reuse tested theme colors and add text/symbol state alongside color
- Screen-reader semantics: `role="tablist"`, `role="tab"`, `aria-selected`, descriptive close labels
- Reduced motion and sensory considerations: no required animation for state comprehension

## Responsive behavior
- Supported breakpoints/devices: existing desktop layout and `max-width: 860px` mobile layout
- Layout adaptations: tabs scroll horizontally and truncate session names; they have no project subtitle and never wrap or resize the chat/composer
- Touch/hover differences: close control remains large enough for touch; tooltips are supplemental

## Interaction states
- Loading: newly resumed session tab shows its existing switching/resuming status
- Empty: hide the strip when no tab is open
- Error: failed attach removes stale live tabs and leaves the user in an explicit no-session state
- Success: activating a live tab updates chat, project binding, draft, model, and context
- Disabled: ended/stale tabs cannot send messages
- Offline/slow network: tabs remain visible while the socket reconnects; no session is ended by disconnect

## Content voice
- Tone: concise and operational
- Terminology: tab means an open view; session means the running Codex process/thread; end means terminate
- Microcopy rules: use "Close tab" for view-only removal and "End session" for process termination

## Implementation constraints
- Framework/styling system: single Python file with inline vanilla HTML/CSS/JS
- Design-token constraints: reuse existing CSS custom properties
- Performance constraints: restore an opened tab from its in-memory DOM cache before networking; use sequenced incremental `attach`, not `resume` or full replay, to catch up background activity
- Compatibility constraints: existing sidebar, drafts, approvals, model/context state, and service restart behavior must remain intact
- Test/screenshot expectations: source-level regression tests, JavaScript syntax check, unit suite, and desktop/mobile visual smoke check when practical

## Open questions
- None currently.
