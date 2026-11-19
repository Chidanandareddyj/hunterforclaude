# Hunter: Implementation Plan

Companion to `DESIGN.md`. Each milestone is independently shippable and ends with a checkpoint you can verify. Work happens in order; do not start a milestone until the previous one's acceptance criteria pass.

## Milestone overview

| # | Milestone | Outcome | Stop for review? |
|---|---|---|---|
| M0 | Project skeleton | Installable package, config, DB, logging, CI-style checks | No |
| M1 | Core pipeline | Extract, filter, normalize, dedup and score, fully unit-tested | No |
| M2 | Telegram notifier and run modes | Alerts reach your phone end to end, with `--dry-run` and `--replay` | **Yes** |
| M3 | Phase 1 collectors | Lemmy, Bluesky, HN, aggregators, `t.me/s/` | Per collector |
| M4 | Feedback and scoring loop | Buttons update scores and mutes | No |
| M5 | Ops hardening | Heartbeat, health alerts, Docker, docs | Yes |
| M6 | Phase 2 | Mastodon, GitHub, more Lemmy, MTProto (optional) | Per collector |

Recommended pause after **M2** so you can test the alert flow before more collectors go in.

---

## M0: Project skeleton

**Tasks**
1. Initialize `pyproject.toml` (Python 3.12), with `src/hunter/` layout and a console entry point.
2. Pick dependencies and pin them: `httpx` (async HTTP), `pydantic` and `pydantic-settings` (config), `aiosqlite` (storage), `structlog` (logs), `python-telegram-bot` or `aiogram` (Telegram), `websockets` (Jetstream), `beautifulsoup4` or `selectolax` (HTML parsing), `pytest`, `pytest-asyncio`, `respx` (HTTP mocking), `ruff`, `mypy`.
3. Config loading: `config.yaml` for settings, `.env` for secrets, with validation that fails fast with clear errors. Ship `config.example.yaml` and `.env.example`.
4. Logging setup (structured, with source and link-code context fields).
5. SQLite layer: schema creation and simple migrations (a version table plus numbered SQL scripts). Tables per `DESIGN.md` §5.
6. `.gitignore` (`.env`, DB files, caches), `README.md` stub.
7. Lint, type-check and test commands wired up in one task runner (Makefile or `pyproject` scripts).

**Acceptance**
- Fresh clone, install, then `pytest`, `ruff` and `mypy` all run clean.
- Starting the app with a missing required secret fails with a clear message.
- DB schema is created on first run and is idempotent on re-run.

---

## M1: Core pipeline (no network)

Build as pure, testable functions and small classes.

**Tasks**
1. **Models:** `RawItem`, `Candidate` (code, url, item reference), `Alert`.
2. **Extractor:** find URLs in text, link fields and Markdown or HTML. Pattern comes from config.
3. **Filter rules** (each independently testable):
   - Exact `claude.ai` host match; reject lookalikes, userinfo tricks and unicode confusables.
   - Reject shorteners and non-direct links.
   - Payment, DM and funnel heuristics.
   - Staleness cutoff (default 30 min).
   - Source and author mute checks.
4. **Normalizer and dedup:** canonical code key, tracking param stripping, sighting recording, once-per-code alerting, 24h re-alert rule, ❌ Dead and ✅ Worked suppression.
5. **Scorer:** source trust × freshness decay × corroboration boost × author history, with confidence tag and the silent-vs-normal decision. Weights are in config.
6. **Pipeline orchestrator:** `RawItem` in, zero or one `Alert` out, with all DB writes in one transaction.
7. **Test fixtures:** a collection of realistic posts (valid, expired-looking, scam funnel, lookalike domain, shortener, payment request, stale, duplicate across sources, re-post after 24h).

**Acceptance**
- Tests cover each filter rule, dedup and re-alert edge cases, scoring monotonicity (older means lower score, more sources means higher), and auto-mute thresholds.
- Pipeline run over the fixture set produces the exact expected alerts, with no network.

**Open check before M1 is final:** confirm the real referral URL and code shape from a few real public examples, and set the pattern in config. Do not open or redeem any link.

---

## M2: Telegram notifier and run modes

**Tasks**
1. **Bot setup flow:** README steps for BotFather. On first run with no stored chat ID, the bot waits for `/start`, stores that chat ID, and from then on ignores all other chats.
2. **Message formatter:** link, age, source with permalink, number of sources, confidence tag. Preview disabled. Escape text properly.
3. **Send queue:** serialized sending with rate limiting, retry and backoff, and message ID storage.
4. **Silent vs normal notifications** per the scorer decision.
5. **Commands:** `/pause`, `/resume`, `/stats`, `/sources` (buttons wired in M4).
6. **Run modes:**
   - `--dry-run`: full pipeline, prints alerts to console, never sends.
   - `--replay <file>`: feeds saved items through the pipeline (combined with dry-run or real send).
   - `--send-test`: sends one sample alert to the locked chat.

**Acceptance**
- `--send-test` delivers a correctly formatted message to your phone.
- A message from any other chat gets no response.
- `--replay` with the fixture file produces the expected alerts in dry-run, and in real mode they arrive in Telegram.
- A rate-limit or network failure is retried without duplicate messages.

**Review checkpoint.** Test the alert flow on your phone before continuing.

---

## M3: Phase 1 collectors

Common base: abstract collector with cursor load and save, interval with jitter, exponential backoff, per-source health, and enable flag. Build the base first, with tests for backoff, cursor persistence and failure isolation (one failing source must not affect the others).

Per collector, in this order:

1. **Lemmy**
   - Query the search API for the referral substring, sorted by new, for posts and comments.
   - Parse author, timestamp, permalink and link fields.
   - First verify the mirror community is still receiving posts, and record its typical lag in the README.
2. **Bluesky Jetstream**
   - Subscribe to the post collection, substring-match client-side.
   - Reconnect with cursor, handle backpressure and keepalive.
3. **Hacker News (Algolia)**
   - `search_by_date` for stories and comments, with cursor on `created_at_i`.
4. **claudecoworkcourse directory**
   - Parse codes and relative timestamps (convert "3h ago" to approximate absolute time with a margin).
   - Mark as low-confidence, since codes may be exhausted.
   - Check `robots.txt` programmatically at startup, and skip if disallowed.
5. **1parrainage** (gated)
   - **Do not enable until you have read its robots.txt and ToS manually.** Ship it disabled by default, and document the gate.
   - Date-only timestamps, so treat age as unknown and cap its confidence.
6. **Generic `t.me/s/<handle>` collector**
   - Empty seed list by default. Parse message text, links, timestamps and permalinks.
   - 15s per channel with jitter, small channel count assumed.

**Acceptance (per collector)**
- Recorded-response tests cover normal, empty, malformed and rate-limited responses.
- A live smoke test (opt-in, manual) shows items flowing into `--dry-run`.
- Cursor survives a restart without re-alerting.
- Source health is visible in `/sources`.

---

## M4: Feedback and scoring loop

**Tasks**
1. Inline buttons: ✅ Worked, ❌ Dead, 🔇 Mute source. Callbacks are accepted only from the locked chat.
2. Vote storage, with message editing to show the recorded outcome.
3. Score updates from votes for sources and authors.
4. Auto-mute rules (author: 3 consecutive ❌; source: at least 70% dead over at least 5 votes) plus `/unmute`.
5. `/stats`: alerts sent, votes, worked rate per source.

**Acceptance**
- Votes change scores and mutes as specified, covered by tests.
- A muted source or author stops generating alerts, and `/unmute` restores it.
- Voting twice on one alert is idempotent or updates cleanly.

---

## M5: Ops hardening

**Tasks**
1. Dockerfile and `docker-compose.yml`: one service, SQLite volume, `.env` file, restart policy, healthcheck.
2. Heartbeat: daily "alive" message with counts and source health.
3. Health alerts: source errored for over 10 minutes, or silent for 24 hours.
4. Graceful shutdown that persists cursors and finishes in-flight sends.
5. Structured logs with rotation.
6. README: setup, BotFather steps, config reference, running locally and in Docker, VPS migration notes, and a short ToS and limitations section.

**Acceptance**
- `docker compose up` on a clean machine gets a working monitor with only `.env` and config filled in.
- Killing and restarting the container loses no cursors and creates no duplicate alerts.
- A simulated source outage triggers a health alert.

---

## M6: Phase 2 (optional, per collector)

- Mastodon public and hashtag streams.
- GitHub search (authenticated, rate-limit aware).
- Additional Lemmy communities and instances.
- Telegram MTProto collector: throwaway account, read-only, public channels only, and only if `t.me/s/` latency proves insufficient.
- X: stub only, no implementation.

Each follows the M3 collector acceptance criteria.

---

## Cross-cutting rules

- **No auto-redemption and no logged-in sessions**, at any milestone.
- **Never follow redirects to unknown hosts.** Host allowlist stays strict.
- **No secrets in the repo or logs.**
- **Respect robots.txt and ToS.** Each collector documents its access basis in the README.
- **Everything configurable** (intervals, cutoff, weights, thresholds, URL pattern) without code changes.
- **Failure isolation.** One collector failing never stops the others.

## Risks to track during the build

| Risk | Mitigation |
|---|---|
| Referral URL format differs from assumption | Config-driven pattern, confirmed from public examples in M1 |
| Lemmy mirror is stale | Verify before building the collector, and surface staleness in `/sources` |
| Aggregators change layout | Recorded-fixture tests plus a parse-failure health alert |
| Telegram rate limits | Send queue with backoff |
| Source ToS changes | Per-source enable flag and documented access basis |
| Thin signal volume | Set expectations in the README. Low-score alerts go silent rather than dropped. |

## Suggested order of work

M0, M1, M2, **review**, then M3 collectors (Lemmy, Bluesky, HN, claudecoworkcourse, `t.me/s/`, 1parrainage once cleared), M4, M5, M6 as wanted.

## Definition of done (Phase 1)

- Phase 1 collectors are running in Docker.
- A new referral link posted on a monitored source reaches your Telegram chat, correctly deduped and tagged by age.
- Feedback buttons work and visibly change scoring over time.
- Heartbeat and health alerts work.
- Tests pass, and linting and type checks are clean.
