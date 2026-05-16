# Len Dawg Bot

[![Pages](https://github.com/ZJBLJB-code/lendawgbot/actions/workflows/pages.yml/badge.svg)](https://github.com/ZJBLJB-code/lendawgbot/actions/workflows/pages.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

> Live: **https://zjbljb-code.github.io/lendawgbot/**

A single-page dashboard that turns Len Dawg Bot's trading day into something Tom (the human, Zach's dad) can read in five seconds. Built like a sports app — block numbers, dry humor, AI personas debating each trade, and a Robinhood-style "tap to reveal" daily ritual.

The dashboard is a **truly static** single-file HTML build. No runtime JavaScript frameworks, no third-party CDNs (fonts are self-hosted, even). It re-renders from a JSON snapshot on every cron tick, then deploys to GitHub Pages.

## Two views

| | URL | What it shows |
|---|---|---|
| **REAL** | `/` (`index.html`) | Whatever Len Dawg Bot is actually doing today. Currently the lab view: 5 edge-proof gate verdicts from a 5-year backtest, sourced from real `verdict_*.json` files. |
| **DEMO** | `/demo.html` | The polished simulated dashboard: 4-day winning streak, +$37.50 today, +$181 cumulative, four AI personas arguing, the works. Use it to see what the live view will look like once the bot starts trading. |

A toggle in the header flips between them.

## Quick start

```bash
git clone https://github.com/ZJBLJB-code/lendawgbot
cd lendawgbot
python3 scripts/build.py            # builds dist/index.html from data/sample.json
open dist/index.html                # default browser
```

No virtualenv, no `pip install`, no Node — Python 3.11 stdlib is enough to build the demo.

## Architecture

The pipeline has **two stages with a frozen contract between them**: a
canonical publisher that reads quant-bot's raw artifacts and writes one
schema-validated, PII-free JSON file, and a build step that consumes that
file and renders the static dashboard. Because the canonical schema in
`tools/publisher_schema.py` is an **allow-list** (unknown fields are dropped
at construction time), PII cannot survive the trip from quant-bot into
`data/dashboard.json` — there is no separate sanitize step. Schema version
is pinned at `2.0`; bumping it is a breaking change.

```
        +-------------------+
        |   quant-bot       |   journal/day0/verdict_*.json
        |   (raw artifacts) |   journal/live/*.jsonl
        |                   |   data/processed/trades.parquet
        +---------+---------+
                  |
                  v
        +-------------------+        tools/publisher.py  (v2)
        |  canonical        |  --->  read raw -> typed Dashboard ->
        |  publisher        |        ALLOW-LIST emit -> atomic write
        +---------+---------+
                  |
                  v
        +-------------------+        data/dashboard.json
        |  canonical JSON   |        schema_version 2.0
        |  (the contract)   |        + _provenance block
        +---------+---------+
                  |
                  v
        +-------------------+        scripts/build.py --from canonical
        |  build step       |        embeds JSON inline into
        |                   |        src/template.html, minifies,
        |                   |        self-hosts fonts
        +---------+---------+
                  |
                  v
        +-------------------+
        |  dist/            |        index.html  -> real data
        |  (deployable)     |        demo.html   -> demo data
        |                   |        _status.json -> health probe
        +---------+---------+
                  |
                  v
        +-------------------+
        |  GitHub Pages     |        every push + every 15 min
        |  (or CF Pages)    |        during market hours
        +-------------------+
```

The legacy `adapters/quant_bot.py` (read-and-reverse-engineer-with-blocklist)
is deprecated and kept only as a transitional safety net. New work belongs
in `tools/publisher.py` + `tools/publisher_schema.py`.

## The "refresh" workflow

When the bot writes a new verdict, refresh the dashboard end-to-end with a single command:

```bash
# Local-only refresh (sync + build, no commit):
bash scripts/refresh.sh

# Full refresh + push (CI rebuilds + redeploys publicly in ~1-2 min):
bash scripts/refresh.sh --deploy

# Just see what changed in the staged journal, no build:
bash scripts/refresh.sh --diff-only
```

The script uses `$QB_ROOT` (defaults to `$HOME/quant-bot`). Override:

```bash
QB_ROOT=/path/to/your/quant-bot bash scripts/refresh.sh --deploy
```

## Auto-publish on every trade (recommended)

The cleanest path: have the bot publish to this repo itself. After every
`position_closed` event and every EOD flatten, the bot runs the vendored
publisher and `git push`es a fresh `data/dashboard.json` via a fine-scoped
PAT. CI rebuilds + redeploys in ~90 seconds. You never have to type
anything.

Setup (one-time, ~10 minutes): see [docs/auto_publish_setup.md](docs/auto_publish_setup.md)
— PAT creation, 1Password stash, bot env-var, branch merge.

The bot-side code lives on the `claude/dashboard-publish-hook-2026-05-15`
branch of the private bot repo. It's gated on `LENDAWGBOT_REPO_DIR` — the
hook is a no-op until the env var is set, so the branch can land safely
and the auto-publish loop is activated by flipping one env var.

## Auto-update on file change (legacy path)

Predecessor of the bot-side publish hook: a `launchd` agent watches the
quant-bot journal directory and runs `refresh.sh --deploy` when the bot
writes a new verdict. Requires the Mac to be awake at trade time.

```bash
LENDAWGBOT_DIR=$HOME/lendawgbot \
QUANT_BOT_DIR=$HOME/quant-bot \
  bash scripts/install_watcher.sh
```

Logs: `/tmp/lendawgbot-watcher.log`. Uninstall: `bash scripts/install_watcher.sh --uninstall`.

## Project structure

```
.
├── src/template.html          # the single-file dashboard (~3.5k lines)
├── scripts/
│   ├── build.py               # template + data → dist/index.html
│   ├── refresh.sh             # one-shot sync + build + deploy
│   ├── sync_quant_bot.sh      # mirror fresh artifacts from $QB_ROOT
│   └── install_watcher.sh     # launchd auto-trigger on file change
├── tools/
│   ├── publisher.py           # canonical publisher (v2): raw → dashboard.json
│   ├── publisher_schema.py    # allow-listed Dashboard schema (v2.0)
│   └── dashboard.schema.json  # emitted JSON Schema artifact
├── adapters/
│   ├── quant_bot.py           # DEPRECATED — kept as transitional fallback
│   └── translations.py        # presentation copy (regimes, signals, etc.)
├── data/
│   ├── dashboard.json         # canonical published payload (v2.0)
│   ├── sample.json            # demo (LIVE mode showcase)
│   └── day0_sample.json       # demo (DAY0 lab mode)
├── personas/                  # reusable persona charters for design reviews
├── docs/                      # design + audit history
├── schema.py                  # Dashboard / Verdict / EquityPoint dataclasses
└── .github/workflows/pages.yml
```

## Design language

- **Typography:** Inter (body) · Fraunces (hero P&L, opsz axis) · JetBrains Mono (numerics) — all latin-subset woff2, self-hosted, base64-cached.
- **Color tokens:** soft positive `#29D391`, coral negative `#FF7A6E`, Apple-blue link `#6BA0FF`. WCAG AA contrast pass.
- **Motion:** 4 durations + 3 cubic-bezier easings, 680ms page-entrance choreography, hero count-up with prefers-reduced-motion bypass.
- **Gamification:** daily curtain reveal, streak ring, confetti on green-day-with-streak, locker-room gate lights, markets-opened ticker. All Tom-grade restraint — once-per-day, opt-out for reduced-motion, mute toggle for audio.
- **Hardening:** strict CSP (zero third-party hosts), HSTS 2yr, Permissions-Policy, frame-ancestors deny. Per-section `safe()` error boundaries, `esc()` HTML escape, `num()` numeric coercion against XSS. Build always emits `_status.json` for external monitors.

## Health monitoring

`/_status.json` is the single source of truth for build health:

```json
{
  "ok": true,
  "schema_version": 1,
  "last_build_at": "2026-04-26T20:31:14Z",
  "last_build_source": "quant-bot",
  "publisher": "github-pages",
  "mode": "DAY0",
  "data_age_hours": 0.5,
  "warnings": [],
  "errors": []
}
```

External monitors (UptimeRobot, Better Stack, healthchecks.io) can poll that one URL and assert `"ok": true`. Tab-side, the dashboard also polls it every 60s during market hours and soft-reloads when `last_build_at` advances — so an open tab is never more than 60s stale.

## Deploy paths

GitHub Pages (this repo) is the default. For Cloudflare Pages with custom domain support, see [docs/cloudflare-pages.md](docs/cloudflare-pages.md) — keep `wrangler.toml` and add the two CF secrets to the repo.

## License

MIT. See [LICENSE](LICENSE).

## Credits

Built by Zach for Tom. Designed with help from a full cast of specialist sub-agents (designer, engineer, QA, PM, motion designer, accessibility auditor, performance engineer, info architect, and a Tom's-eye UX specialist), all running in parallel via Claude Agent SDK.

Not financial advice. The bot trades a /MES futures contract on a small-dollar paper account; all numbers in the demo are fabricated for design purposes.
