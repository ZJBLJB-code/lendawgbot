# Auto-publish setup (Track B)

This doc covers the operator steps to enable the bot's self-publish hook.
After completion, the dashboard at https://zjbljb-code.github.io/lendawgbot/
will auto-update every time the bot closes a position or runs EOD flatten —
**you stop having to type `refresh`**.

The hook itself ships in the quant-bot repo on branch
[`claude/dashboard-publish-hook-2026-05-15`](https://github.com/ZJBLJB-code/zach-pocs/tree/claude/dashboard-publish-hook-2026-05-15)
(it's in zach-pocs so it stays private; only the resulting dashboard.json
ever hits the public lendawgbot repo).

## What this enables

```
bot closes a position
       │ fires _publish_dashboard_async()  (fire-and-forget asyncio task)
       ▼
publisher reads bot's journal
       │ writes data/dashboard.json into local lendawgbot clone
       ▼
git add + commit + push  (via fine-scoped PAT)
       │
       ▼
GitHub Actions rebuilds + Pages deploys  (~90 seconds)
       │
       ▼
public URL reflects the trade.  Tom's open tab soft-reloads.
```

Nothing else needs to change after first-time setup. Bot keeps trading, site
stays current.

## Three things the operator must do (one-time)

### 1. Create the GitHub PAT

1. Open https://github.com/settings/tokens?type=beta
2. **Fine-grained personal access token** → Generate new token
3. **Token name:** `lendawgbot-publish-2026-05-15` (or similar — annual rotation)
4. **Expiration:** 1 year
5. **Resource owner:** `ZJBLJB-code`
6. **Repository access:** **Only select repositories** → choose `lendawgbot` only
7. **Permissions** → **Repository permissions:**
   - **Contents:** **Read and write**  (this is the only permission that needs to be touched)
   - Everything else: stay at "No access"
8. Generate → copy the `github_pat_...` token immediately (you can't see it again)

### 2. Stash the PAT in 1Password

```bash
op item create \
    --category="API Credential" \
    --title='lendawgbot-publish-token' \
    --vault='Private' \
    credential='github_pat_<your-token-here>' \
    notes='Fine-scoped PAT. contents:write on lendawgbot only. Expires <date>. Rotate annually.'
```

(Or paste it into the 1Password GUI under whatever account you use for service-credentials.
Whatever you do, **do not commit the raw PAT anywhere**.)

### 3. Wire the bot

On the machine where the bot runs (your Mac):

```bash
# Clone lendawgbot to a stable local path
git clone https://github.com/ZJBLJB-code/lendawgbot.git $HOME/lendawgbot-publish

# Embed the PAT in the local clone's remote URL  (1Password CLI reads the token at runtime)
TOKEN="$(op read 'op://Private/lendawgbot-publish-token/credential')"
git -C $HOME/lendawgbot-publish remote set-url origin \
    "https://${TOKEN}@github.com/ZJBLJB-code/lendawgbot.git"

# Tell the bot where to publish
echo 'LENDAWGBOT_REPO_DIR=/Users/zach_barbitta/lendawgbot-publish' >> $HOME/quant-bot/.env

# Merge the dashboard-publish-hook branch into bot's main when ready
gh pr create --repo ZJBLJB-code/zach-pocs \
    --base main \
    --head claude/dashboard-publish-hook-2026-05-15 \
    --title 'feat(dashboard_publisher): auto-publish to LenDawgBot dashboard'
# Review the PR, merge, then restart the bot
```

The hook is **gated on `LENDAWGBOT_REPO_DIR`** — if the env var is unset, the
hook is a no-op. So merging the orchestrator code without setting the env var
yet does nothing; you can stage the merge first and flip the flag whenever
you're ready.

## Verifying it works

After bot restart, watch for the first `position_closed` event in the journal.
The orchestrator's hook fires a fire-and-forget task. You'll see one of these
events written within ~5 seconds:

```json
{"event": "dashboard_publish_result",
 "trigger": "position_closed",
 "ok": true, "action": "pushed",
 "detail": "branch=main", "commit": "abc12345"}
```

Then within ~90s, the live URL's `/_status.json` will show the new commit SHA.

## Failure modes (operator-facing)

| Symptom | Cause | Fix |
|---|---|---|
| `dashboard_publish_result ok=false action="failed" detail="publish: ..."` | Publisher crashed on bot's journal data | Run `python -m src.dashboard_publisher --out /tmp/d.json` manually to reproduce; check the error |
| `... detail="git push exhausted retries"` | PAT expired / wrong remote URL / network | Re-issue PAT (annual), re-run the `git remote set-url` command |
| `dashboard_publish_failed` event with no `dashboard_publish_result` | Hook itself raised inside the asyncio task | Should never happen (everything wrapped in try/except), but if it does the bot keeps trading |
| Bot trades but no events appear | `LENDAWGBOT_REPO_DIR` is unset | Check `.env`; restart bot |
| Live URL still stale after a fresh trade | CI workflow failed on the push | Check https://github.com/ZJBLJB-code/lendawgbot/actions for red runs |

## Rolling back

Unset `LENDAWGBOT_REPO_DIR` in `$HOME/quant-bot/.env`. Bot continues trading;
hook becomes a no-op. No code revert needed.

If you want the hook out of the bot's code entirely, revert the
`claude/dashboard-publish-hook-2026-05-15` branch's commit on bot's main.

## Why this design

- **Allow-list at schema, not strip-list at sync.** Every field that ships in
  dashboard.json must be declared in `publisher_schema.py`. Unknown keys are
  dropped at construction time. New fields the bot adds tomorrow can't leak.
- **Fire-and-forget asyncio task.** The orchestrator never awaits the
  publisher. A git push that times out (45s cap) cannot block trading.
- **Fine-scoped PAT.** `contents:write` on one repo only. Worst-case theft
  scenario: attacker can push to the public dashboard repo. They cannot
  read private repos, cannot affect billing, cannot escalate.
- **Idempotent push.** If two `position_closed` events fire in the same
  second, both publish-tasks write the same JSON; the second push is a
  no-op (diff-vs-HEAD short-circuit). No spurious CI runs.
- **Three-layer error containment.** env-var gate → run_in_executor isolation
  → bare except inside the inner async closure. A bug here can never
  propagate to the bot's trading loop.
