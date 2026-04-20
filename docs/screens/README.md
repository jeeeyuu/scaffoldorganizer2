# Screens

Reference mockups of each tab at the default 600×400 pywebview window size.
All SVGs share the same chrome (top-bar, toolbar, tab strip, sticky command
bar) and vary only the panel content, so the files double as a style guide
for future layout changes.

| # | Tab         | File                                   | What it shows |
|---|-------------|----------------------------------------|---------------|
| 1 | Inbox       | [01-inbox.svg](01-inbox.svg)           | Uncommitted captures from Telegram + chat, amber left-borders, P2–P5 chips |
| 2 | Active      | [02-active.svg](02-active.svg)         | `todo` and `doing` merged on one page — the `doing` card sits on top with a green glow and a `● DOING` state label |
| 3 | Long-term   | [03-longterm.svg](03-longterm.svg)     | `horizon=long_term` backlog, protected from AI regeneration |
| 4 | Sessions    | [04-sessions.svg](04-sessions.svg)     | Brain-dump editor on the left (raw + structured Markdown), saved-session list on the right with Load buttons |
| 5 | Work Log    | [05-worklog.svg](05-worklog.svg)       | Editable daily worklog draft, Save / Save & Export actions, plus the archive list |
| 6 | Done        | [06-done.svg](06-done.svg)             | Completed items at 72 % opacity with a line-through title — still in the DB for worklog context |

## Visual conventions

- **Left-border colour** encodes status: amber=inbox, blue=todo, green=doing,
  muted=done, faint=archived.
- **Priority chip** encodes urgency: P1 red, P2 amber, P3 blue, P4 muted,
  P5 outline-only.
- **Doing** cards get an extra green glow and the title jumps to weight 800.
- **Status dots** (top-right): green = healthy, amber = idle / disabled,
  red = error (hover the dot for the raw error string).

These are hand-crafted SVGs, not screenshots — they intentionally use the
same palette and type rules as [`gui/assets/style.css`](../../gui/assets/style.css)
so they stay credible as preview images even as the real UI evolves.
