# V3 Research — Seven-Agent Fan-Out

> **Historical note (v1):** references to "Tom-the-bot" in this document predate the rename to **Len Dawg Bot**. They are preserved here as the v1 record. New copy uses "Len Dawg" / "Len Dawg Bot".

Round 3 spec gathering. Seven specialist agents ran in parallel to think through every dimension before any v3 code is written. This document is the consolidated punch list of record. Implementation lands in v3 PR.

## 1. Schema contract (data engineer)

- **Module**: `tomcash/schema.py` — frozen Python dataclasses, `Verdict` enum (TAKE/SKIP/REDUCE_SIZE), `Dashboard.from_dict`/`to_dict` round-trip.
- **Versioning**: `schema_version` field on `Dashboard`, semver, MAJOR-bump-on-mismatch guards the renderer.
- **Validation**: switch to **Pydantic v2** for production (free JSON Schema export, field-path errors). Dataclasses good enough for v3 PoC.
- **Required minimum**: `schema_version`, `mode`, `generated_at`, `bot.{name,instrument,phase_short}`. Mode-specific: LIVE needs `today.pnl_dollars` + `equity_curve`; DAY0 needs `verdict` + `gates`.
- **JSON Schema**: yes — emit `tomcash/schema.json` from the model, check in.
- **Day 0 envelope**: deferred to v1.1.0 as additive `day0: Day0Block | None` field.

## 2. Adapter / pipeline (platform engineer)

- **Entry point**: `build_dashboard(quant_bot_root: Path, mode="AUTO") -> Dashboard` in `tomcash/adapters/quant_bot.py`.
- **Read order (LIVE)**: `data/processed/trades.parquet` → `journal/live/<today>.jsonl` → `LEARNINGS.md` → `journal/eod/reflection_<today>.json`. Each reader has graceful fallback.
- **Read order (DAY0)**: freshest `journal/day0/verdict_*.json`.
- **Mode detection**: filesystem-only. LIVE requires `≥1 event in {bracket_filled, trade_closed}` AND `_is_fresh(file, 30h)`. Heartbeat-only logs do NOT qualify.
- **Staleness**: `MAX_STALENESS_HOURS = 30`. Stale → render `AWAITING TODAY'S DATA` empty state, never silently re-publish yesterday as today.
- **Translations**: live in `tomcash/adapters/translations.py` (presentation copy owned by TomCash, not quant-bot).
- **Failure modes**: 8 enumerated. Adapter failure → `build.py` falls back to `data/sample.json`. Cron stays green.
- **CLI**: `python scripts/build.py --from quant-bot --root /abs/path [--mode AUTO|LIVE|DAY0]`.
- **Test plan**: 5 cases (mode-detect, heartbeat-ignore, stale → empty state, unknown-instrument fallthrough, build.py-fallback subprocess).

## 3. Motion language (motion designer, ex-Apple/Linear)

**Tokens**:
```css
--ease-out-quint: cubic-bezier(0.22, 1, 0.36, 1);
--ease-out-back:  cubic-bezier(0.34, 1.4, 0.64, 1);
--ease-in-out-quart: cubic-bezier(0.76, 0, 0.24, 1);
--dur-instant: 80ms; --dur-fast: 160ms; --dur-base: 320ms; --dur-slow: 640ms;
```

- **Page entrance**: 680ms total, staggered. Header glass → hero → ticker → earnings/curve (curve draws L→R) → cage cards (60ms stagger) → quote → scoreboard → tape → learnings.
- **Hero count-up**: 640ms out-quint, decimal precision held throughout (no flicker), `prefers-reduced-motion` skips to final value. Vanilla JS, ≤30 lines.
- **Hover micro-interactions**: cage cards lift -2px, tape rows tint bg, scoreboard rows shift bg. All `--dur-fast` `--ease-in-out-quart`.
- **Equity curve**: hairline tracks cursor with 80ms linear (locked feel), glass chip shows date/P&L/running total.
- **Tape expand**: chevron rotate 0→90deg, `max-height: 0→320px`, inner content stagger 50ms.
- **Live pulse**: 2s cycle, scale 1→1.18, opacity 1→0.6→1.
- **Don't animate**: live data updates after first paint, ticker pause-on-hover, footer links, header on scroll, persona avatars (they're portraits, not mascots).

## 4. Iconography (Lucide, 1.5px stroke)

**The minimum 9**:
1. `trending-up` — streak banner, equity legend
2. `trending-down` — streak (cold), negative hero
3. `check` — persona "right" mark, Day 0 gate pass
4. `x` — persona "wrong" mark, Day 0 gate fail
5. `chevron-down` — tape row expand affordance
6. `trophy` — MVP badge
7. `radio` — header live pill
8. `info` — Cage Match tooltip trigger
9. `swords` — Cage Match section title

**Sizing**: 14px (inline-with-text), 18px (section adjuncts), 24px (hero-adjacent only). No 16/20.
**Color**: `stroke="currentColor"`, `fill="none"`. Always quieter than adjacent text — default `var(--text-dim)`.
**Don't iconify**: wordmark, ticker tape items, learnings/tomorrow bullets.

## 5. Accessibility (WCAG audit)

**Contrast failures (must fix)**:
- `--text-tertiary` `#6E7888` on surface: 3.8:1 → bump to **`#7E899A`** (4.7:1)
- `--negative` `#FF6B5E` on surface: 4.2:1 → bump to **`#FF7A6E`** (4.6:1) OR keep large-text-only
- `--link` `#4F8BFF` on surface: 4.4:1 → bump to **`#6BA0FF`** (5.0:1), underline always

**Color-blind redundancy**: ▲/▼ on hero (already wired), +/− signs preserved, diagonal SVG `<pattern>` on negative equity fill.

**Focus**: 2px solid `--focus-ring`, offset 2px (3px on cards, -2px on tape rows).

**ARIA top 10**: skip-link to `#main`, `<section aria-labelledby>` on hero with sr-only `<h2>`, `aria-live="polite"` on `#last-refresh` and `#live-state`, `<details><summary>` for tape rows (free keyboarding), `aria-label="Up $37.50 today"` on hero (override the ▲ pseudo-element via separate aria-hidden span).

**prefers-reduced-motion**: kill ticker scroll, kill pulse animation, squash all transitions to 0.001ms, freeze Chart.js animations. Keep instant state changes.

**prefers-color-scheme**: dark only for v3. Light mode deferred.

**Touch targets**: bump `.tape-row .toggle` to 44×44, increase row padding to 14px on mobile.

**Top 5 wins (1h)**: token hex fixes → hero aria-label → focus ring → tape ARIA → reduced-motion block.

## 6. Information architecture (ex-Robinhood, Apple Card)

**Tier 1 (above fold, ≤3)**: Hero, Total earnings + curve, Tom's Brag Card *(new)*.
**Tier 2**: Cage Match, Scoreboard, Today vs Bot's Average *(new)*, What's next.
**Tier 3**: The Tape, AI Learnings, Pull-quote, Ticker tape, Footer.

**Proposed v3 order**:
1. Header
2. Hero (Today P&L)
3. **Tom's Brag Card** *(new)* — one-tap shareable line: "4-day streak, +$181, day 10 live"
4. Total earnings + equity curve
5. **Today vs Bot's Average** *(new)* — three chips: today / 10-day avg / best-vs-worst percentile
6. AI Cage Match
7. Persona Scoreboard
8. Pull-quote
9. What's next
10. The Tape (depth)
11. AI Learnings (depth)
12. Ticker tape — demoted to thin strip above footer
13. Footer

**Cut**: standalone ticker-tape band (fold its 2 useful stats — best/worst day — into Today vs Bot's Average).

**Footer rewrite (4 lines, verbatim)**:
```
Made for Dad.
Day 10 live · 1 contract at a time · real money, small stakes.
Tom-the-bot is an experiment by Zach. Not financial advice.
Updated Apr 25, 8:19 AM PT.
```

**Anchors / TOC**: no. Tom is a passive reader, not a navigator.

## 7. Tom's-eye UX walk (designer for older adults)

**5-second test scores**:
- "Did Tom-the-bot make money today?" → 8/10 (hero is loud)
- "Are we up overall?" → 4/10 (curve is buried 3 cards down)
- "Is the bot on a heater?" → 6/10 (streak banner exists but quiet)

**8 vocabulary fixes**: "MVP" → "Top performer this month", "Vetoed" → "Talked the bot out of", "Calm, choppy market" → "Quiet day for stocks", "Phase A — Live, 1 contract max" → "Day 10 of real trading. Small bets only", etc. Rename "The Tape" → "Today's trades" or "The play-by-play."

**The brag moment** — `Tom's Highlights` card, screenshot-sized 16:9:
> Up **$181** on $10K. **(+1.8%)**
> Bot won **21 of 32** trades. (66%)
> The Skeptic talked him out of $187 in losers.

**Sports-page parallels missing**:
- Pre-game **lineup card** ("which persona is hot today")
- Post-game **Trade of the Day** in plain English
- **Comeback meter** ("days since last red day")

**Confusing**: `T-20260424-00` trade IDs (drop or rename "Trade #1 today"), unlabeled equity y-axis, "THE TAPE" jargon, "Calm, choppy market" contradiction, cage verdict tone reversal.

**Ruthless cut**: the Tape, behind a "See all trades" link.

## 8. Performance + build (ex-Vercel/Cloudflare)

- **Single-file vs CDN**: **inline everything at build**. ~250KB cost buys determinism.
- **Fonts**: self-host woff2, latin-only subset, base64-embedded. `font-display: swap`. Inter + Fraunces + JetBrains Mono = ~68KB subsetted (vs ~210KB unsubsetted).
- **Critical CSS**: don't split. Whole stylesheet ~6KB. Inline 100%.
- **Chart.js**: **delete it**. Replace with 2KB hand-rolled SVG sparkline. Removes last external dep.
- **Build script**: `htmlmin`, `jsonschema` validate, fonts cached at `.cache/fonts/`, atomic write to `dist/index.html.tmp` then rename. On exception, render `src/fallback.html`. Never fail the cron.
- **Budget**: HTML+inline ≤ 220KB gz, LCP ≤ 800ms, TTI ≤ 1100ms, CLS = 0.
- **Cron**: `0 9,12,13 * * 1-5` `TZ=America/Los_Angeles`. Wrapper uses `flock` to avoid mid-write race.
- **Deploy**: **Cloudflare Pages**. Free TLS for tomcash.com, edge cache, GitHub auto-deploy.
- **Recovery**: `dist/index.html` never overwritten on failure. `dist/_status.json` carries `{ok, last_success, error}`.

---

## Decisions for v3 implementation

1. **Schema contract**: Pydantic v2 (defer in PoC), frozen dataclasses now → migrate later.
2. **Translations** in TomCash, not quant-bot.
3. **Inline everything**, no Chart.js, hand-rolled sparkline.
4. **New IA**: Brag Card promoted to position 3, Today-vs-Average added at position 5, Tape demoted to depth.
5. **Color tokens**: bump tertiary/negative/link per a11y audit.
6. **Lucide 9-icon set** at 1.5px stroke, sized 14/18/24.
7. **Motion language**: 4 durations + 3 easings, page entrance 680ms total.
8. **Cron + Cloudflare Pages** at the publish layer.

This is the v3 punch list. Implementation lives in the next PR.
