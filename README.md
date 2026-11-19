# Hunter

Monitors public sources for freshly posted Claude guest-pass referral links
(`https://claude.ai/referral/<code>`) and alerts one owner via Telegram so the
link can be opened and redeemed manually.

This is a **personal tripwire, not a guarantee**: links are first-come,
first-served and usually claimed within minutes. The design companion is
`DESIGN.md`; the milestone plan is `IMPLEMENTATION_PLAN.md`.

## Ground rules (enforced in code and config)

- Alert only. No auto-redemption, no logged-in sessions, no multiple accounts.
- Official APIs, public feeds and public pages only. `robots.txt` respected;
  the `claudecoworkcourse` collector checks it at startup and skips if disallowed.
- 1parrainage ships **disabled** and must be manually cleared (robots/ToS) first.
- Never follow redirects to unknown hosts; strict `claude.ai` host allowlist.
- No secrets in the repo or logs. Telegram bot is locked to the owner chat.

## Quick start (local)

```bash
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"      # Windows; on POSIX: .venv/bin/pip ...
copy config.example.yaml config.yaml       # POSIX: cp
copy .env.example .env                     # then paste your bot token

# Smoke without Telegram:
hunter --dry-run

# First real run: start the bot, send /start from your Telegram account,
# the bot stores that chat ID and locks to it.
hunter
```

Modes:

| Mode | What it does |
|---|---|
| `hunter` | Full monitor: collectors → pipeline → Telegram. |
| `hunter --dry-run` | Same pipeline, alerts printed to console, nothing sent. |
| `hunter --replay FILE --dry-run` | Feed saved `RawItem`s (JSONL) through the pipeline. |
| `hunter --replay FILE` | Replay with real sends (needs token and locked chat). |
| `hunter --send-test` | Send one sample alert to the locked chat and exit. |

## Telegram bot setup

1. Message @BotFather → `/newbot` → copy the token into `.env`.
2. `hunter --send-test` starts the bot and waits for your `/start`. Open the
   bot in Telegram and send `/start`; the bot stores that chat ID, locks to it,
   and from then on ignores every other chat. The sample alert then arrives.
3. Later runs of `--send-test` (once locked) just deliver the sample directly.

## Layout

```
src/hunter/
  config.py        settings loading and validation
  db.py            SQLite access, migrations
  models.py        RawItem, Candidate, Alert
  extract.py       URL extraction, referral pattern matching
  filters.py       filter rules (host, shorteners, funnel, staleness, mutes)
  normalize.py     canonical codes, dedup, re-alert rules
  score.py         scoring and confidence tags
  pipeline.py      RawItem -> Alert orchestrator
  collectors/      lemmy, bluesky, bluesky_search, hackernews, reddit,
                   mastodon, github, claudecoworkcourse, oneparrainage
                   (gated), telegram_preview, web_search
  notify/          telegram sender, formatter, queue, commands
  feedback.py      button callbacks -> votes -> scores -> mutes
  scheduler.py     collector supervision and health
  cli.py           entry point and run modes
```

## Source access basis

| Source | Method | Notes |
|---|---|---|
| Lemmy | public search API, no auth | falls back to instance-wide search |
| Bluesky | Jetstream firehose | real-time, client-side substring match |
| Bluesky search | public AppView `searchPosts` | catches posts made while offline |
| Hacker News | Algolia `search_by_date` | free |
| Reddit | PullPush / Reddit JSON / Arctic Shift | public posts, no auth |
| Mastodon | public hashtag timelines | no auth; public search is mostly empty |
| GitHub | public issues search | **disabled by default** (~10 req/min limit) |
| claudecoworkcourse | public page, robots.txt checked | relative timestamps → approximate ages |
| 1parrainage | public page | **disabled until manual robots/ToS review** |
| Telegram | `t.me/s/<handle>` preview | empty seed list by default |
| Web search | DuckDuckGo / Bing | **disabled by default** (often blocked) |

## Getting links right now

The monitor only alerts on *fresh* posts (within the 30-minute staleness
cutoff), so a directory of older codes will be correctly filtered out. To grab
whatever is publicly available immediately, use the standalone probe — it needs
no config, DB or Telegram:

```bash
.venv\Scripts\python tools/find_links.py
.venv\Scripts\python tools/find_links.py --write data/found_links.jsonl
```

It queries several public sources, filters obvious troll codes, and prints the
most plausible recent links. Guest passes are first-come, first-served, so many
will already be claimed.

ToS and limitations: Anthropic's Guest Pass Terms forbid selling passes,
posting them on deal/referral-exchange sites, or bulk/automated distribution.
This tool only *observes* already-public posts for the owner's personal use;
it does not distribute links, automate redemption, or interact with Anthropic
systems at all.
