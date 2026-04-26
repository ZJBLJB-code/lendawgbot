# TomCash Sub-Agent Personas

Five world-class specialists. Each has a discrete charter. They are dispatched
in parallel after every meaningful change to the dashboard. Their job is to
make TomCash relentlessly better — not to bikeshed.

## When to dispatch
- After v1 of the HTML lands
- After any visual or interaction change
- Before any production deploy (push to TomCash.com)
- Any time a human says "review this"

## How to dispatch
Each persona file is a self-contained prompt. Pass it to a general-purpose
Agent along with: (a) the current `dist/index.html`, (b) the current
`data/sample.json`, (c) the change since last review.

Run all five in parallel. Collect findings. The Tech Lead synthesizes the
findings into a prioritized punch-list. The Engineer implements.

## The roster

1. **product_manager.md** — Tom's voice. Does this delight him? Cut what doesn't.
2. **designer.md** — The aesthetic conscience. Ticker-meets-Robinhood, no compromise.
3. **engineer.md** — Builds. Clean code, no bloat, performance budget.
4. **qa.md** — Breaks things. Tests interactions, mobile, edge cases, accessibility.
5. **tech_lead.md** — Synthesizes the four others. Owns the bar. Final word on ship/no-ship.

## Iteration contract
- Each persona returns findings in a fixed schema (severity, category, fix)
- No persona may say "looks good" without three concrete observations
- Tech Lead's punch-list is the single source of truth for the next iteration
- An iteration is "done" when no Tech Lead item is severity ≥ P1
