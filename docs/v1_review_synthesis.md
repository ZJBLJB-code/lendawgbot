# V1 Review Synthesis (Tech Lead)

Round 1 review by the four sub-agent personas. **50 findings total.**
Counts: PM 12 · Designer 11 · Engineer 13 · QA 14.

This document is the punch list of record for v2. Item numbers are stable.

## V2 punch list — must ship

| # | Severity | Title | Sources |
|---|---|---|---|
| 1 | P0 | Restructure hero: streak callout above number, plain-English context below | PM, Designer |
| 2 | P0 | Header badge "Live" only; move "Phase A · 1 contract" to footer | PM, Designer, QA |
| 3 | P0 | Translate jargon: regime, sub-signal, exit reason, instrument | PM |
| 4 | P0 | Wordmark all-white; reserve green for money only | Designer |
| 5 | P0 | Fix three competing green hero numbers (today / total / cage) | Designer |
| 6 | P0 | Replace Tailwind Play CDN with hand-written CSS | Engineer, QA |
| 7 | P0 | Add HTML escape helper; use everywhere innerHTML interpolates strings | Engineer, QA |
| 8 | P0 | Wrap render sections in try/catch; render error fallback per section | Engineer, QA |
| 9 | P0 | Empty-trades day: "Bot stood down" copy + tape empty state | QA |
| 10 | P0 | Day 0 mode: detect mode, render dedicated lab view (not zeroed live) | QA |
| 11 | P0 | Drop hero card glow effect | Designer |
| 12 | P1 | Cage match scoring: centralize `wasRight`; REDUCE_SIZE = right | PM, Engineer |
| 13 | P1 | Cage verdict text gives the holdout dignity, not "ganging up" | PM |
| 14 | P1 | Persona names with nicknames; lead with full name not initials | PM |
| 15 | P1 | Curate quote selection to layperson-friendly persona lines | PM |
| 16 | P1 | Total earnings context: "On $10K · 1.8% · day N" | PM |
| 17 | P1 | Streak hook surfaces prominently | PM |
| 18 | P1 | Tabular alignment in The Tape: pad day, fixed-width columns | Designer |
| 19 | P1 | Mobile tape: stack vertically below 480px | QA, Designer |
| 20 | P1 | Header wrap fix: nowrap, shorten badge | QA, Designer |
| 21 | P1 | Persona scoreboard MVP as badge, not run-on text | QA |
| 22 | P1 | ▲/▼ on hero and cumulative for color-blind redundancy | QA |
| 23 | P1 | ▲/▼ in ticker strip values | Designer |
| 24 | P1 | Persona scoreboard subtle bar fill for rhythm | Designer |
| 25 | P1 | Chart.js graceful degradation if CDN fails | Engineer |
| 26 | P1 | Cage match: inline direction/sub_signal in payload (no lookup) | Engineer |
| 27 | P1 | Verdict badges typographic + colored left rule, not 3 saturated colors | Designer |
| 28 | P2 | Drop dead CSS (.skeleton, .num-animate, .js-focus-visible) | Engineer |
| 29 | P2 | Use relative time ("Updated 2m ago") | PM, QA |
| 30 | P2 | Add aria-labels to hero numerics | QA |
| 31 | P2 | Pulse-dot aria-label="Live" | Engineer |
| 32 | P2 | Sort recent_trades by entry_time | Engineer |
| 33 | P2 | Add dist/ to .gitignore | Engineer |

## Deferred to v3+

- **Share moment / OG-image card** (PM P2) — high value, design-heavy. Backlog.
- **Live "bot is watching" indicator with countdown** (PM P1) — needs realtime hook. Backlog.
- **Avatars per persona** (PM P1) — design + asset work. Backlog.
- **Bot's-mood self-deprecation surface** (PM P2) — needs reflection data piping. Backlog.
- **Persona-card click-to-expand record** (Designer P2) — nice to have.

## Round 1 verdict

**HOLD.** Five P0s in v1 — three are emotional/hierarchy (hero, jargon, color), two are
correctness (Tailwind CDN, escape/innerHTML). Cannot ship to Tom in this state.
v2 will close all P0s and most P1s.

Top wins from round 1:
- Persona pattern produced specific, non-overlapping findings (no four-way bikeshed).
- The "ticker meets Robinhood" brief held — Designer's critique is "tighten the
  restraint," not "redo the look."
- Engineer + QA independently flagged the same XSS surface and Day 0 break,
  confirming both are real.

Top risks remaining for v2:
- Tailwind replacement is the biggest mechanical change; it can introduce
  regressions if the class list isn't fully covered.
- Day 0 view is genuinely a second design — risk of v2 feeling unfinished there.
- Mobile tape redesign is non-trivial; may need a third round to land.
