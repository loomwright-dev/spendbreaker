# spendbreaker

A small, self-hosted watchdog for people running AI agents and usage-billed SaaS.
Every hour (cron or a systemd timer) it asks your providers' **official, read-only billing/usage
APIs** what you have spent, compares that against caps you set, flags sudden spikes, and pings
you on **Telegram** or **ntfy**. If you explicitly turn it on, it can also pull an official
"off switch" (deactivate a key, pause a project) for the providers that offer one.

- Keys stay on your machine: read from environment variables only, never from the config file
  (the config loader refuses anything that looks like an inline secret).
- No telemetry, no phone-home, no account with us. The only outbound requests are to the
  provider APIs you enable and the alert channel you configure.
- Python 3.9+, one dependency (PyYAML); `boto3` only if you enable AWS.
- MIT licensed.

> **Built by an AI.** This MVP was written by an AI coding agent (Claude) in one session, with
> unit tests against *mocked* API responses. It has **not** been run against live provider
> accounts yet. Treat it as a second line of defence, read the code before giving it admin keys,
> and please open an issue when a provider's response shape differs from what is assumed here.

## What it watches

| Provider | API used (read-only) | Granularity / lag | Optional break action | Status |
|---|---|---|---|---|
| OpenAI | Admin API `GET /v1/organization/costs` (Admin key) | daily buckets; minutes-to-hours lag | `delete_project_api_keys` - **irreversible**, needs `confirm_irreversible: true` | shape per public docs, mocked tests |
| Anthropic | Admin API `GET /v1/organizations/cost_report` (Admin key) | daily buckets; recent hours may lag | `deactivate_api_keys` (`status: inactive`, reversible in Console) | shape per public docs, mocked tests |
| Cloudflare | GraphQL Analytics `workersInvocationsAdaptive` | daily; **estimate only** | none (no official off switch) | estimate = Workers requests x the price you configure |
| Vercel | REST `GET /v1/billing/charges` (FOCUS JSONL) | daily; lag set by Vercel | `pause_projects` (`POST /v1/projects/{id}/pause`) | **least verified** - written from docs |
| AWS | Cost Explorer `GetCostAndUsage` (DAILY) | up to ~24h lag | none (use AWS Budgets Actions inside AWS) | **costs $0.01 per request**; polled every 6h by default |

### Providers / signals we deliberately do not cover
- **Cloudflare real billed spend**: we found no official API that returns current invoice-to-date
  spend, so we estimate from Workers request counts only (no CPU time, R2, D1, KV, images,
  bandwidth). For anything else use Cloudflare's own budget alerts.
- **Anthropic / OpenAI hourly cost**: the cost endpoints are daily; hourly *token* usage exists but
  needs a price table per model, which goes stale. Hourly rate here = difference between two
  polls of "today so far", so it inherits the provider's reporting lag.
- **Anything that needs scraping a dashboard**: not done, by design. If a provider has no API, the
  row above says so.

## How alerts work

Each pass records "today so far" and "month so far" per provider in a local JSON state file
(`chmod 600`). From that it emits:

| Event | When | Repeats |
|---|---|---|
| `daily_warn` / `monthly_warn` | spend >= `warn_at` (default 80%) of a cap | once per day / month |
| `daily_breach` / `monthly_breach` | spend >= cap | once per day / month |
| `monthly_projected` | after day 3, linear projection > monthly cap | once per month |
| `anomaly` | hourly rate > `multiplier` x median of trailing rates, and >= `min_hourly_amount` | cooldown (6h) |
| `fetch_error` | provider call failed (bad key, outage, shape change) | cooldown (6h) |
| `break` | a break action ran (or failed) | once |

Caps can be per provider and/or a `total` across providers (assumes one currency).
If a push channel fails, the alert is retried on the next pass instead of being marked sent.

## The break switch (default OFF)

Three independent conditions must all hold before anything is changed at a provider:
1. top-level `break.enabled: true` (default `false`),
2. that provider has a `break:` block with a supported `action` and explicit IDs,
3. the triggering event kind is listed in its `on:` (default `[daily_breach, monthly_breach]`).

`--dry-run` shows what would happen; `--no-break` disables actions for one pass. Each break runs
at most once per period. OpenAI key deletion cannot be undone, so it also requires
`confirm_irreversible: true`. Prefer the provider's native hard limits (OpenAI project budgets,
Vercel auto-pause, AWS Budgets Actions) where you have them; spendbreaker is the cross-provider
safety net, not a replacement.

## Install and run

```bash
git clone <repo> spendbreaker && cd spendbreaker
pip install pyyaml            # + boto3 if you enable AWS
cp config.example.yaml ~/.config/spendbreaker/spendbreaker.yaml   # edit caps
cat > ~/.config/spendbreaker/env <<'X'
OPENAI_ADMIN_KEY=sk-admin-...
ANTHROPIC_ADMIN_KEY=sk-ant-admin-...
TELEGRAM_BOT_TOKEN=123:abc
TELEGRAM_CHAT_ID=42
X
chmod 600 ~/.config/spendbreaker/env
set -a; . ~/.config/spendbreaker/env; set +a
python3 -m spendbreaker validate   -c ~/.config/spendbreaker/spendbreaker.yaml
python3 -m spendbreaker test-alert -c ~/.config/spendbreaker/spendbreaker.yaml
python3 -m spendbreaker check      -c ~/.config/spendbreaker/spendbreaker.yaml --dry-run
```

Schedule it with `deploy/spendbreaker.timer` + `.service` (systemd user units) or
`deploy/crontab.example`. Exit codes: `0` ok, `1` validate/test-alert problem, `2` config error,
`3` a provider or alert channel failed (cron will mail you). `check --json` prints a report.

Least-privilege credentials: OpenAI and Anthropic cost endpoints need **Admin** keys, which are
powerful (they can also manage keys and members). That is a real risk of running this tool:
keep the env file `600`, run it as a dedicated user, and do not enable break actions you do not
need. Cloudflare: an API token with *Account Analytics: Read*. AWS: an IAM user/role with only
`ce:GetCostAndUsage`. Vercel tokens are not finely scoped.

## Honest limits

- **Not real-time.** Every provider reports cost with a lag (minutes to ~24h). A runaway agent can
  spend a lot between the spend and the report. This tool shortens the time to *notice*; it does
  not guarantee a ceiling. Use a capped virtual card / provider hard limit for that.
- **Untested against live accounts.** Endpoint paths and response shapes come from public docs as
  of Nov 2026 and are covered by mocked tests only. Vercel and Cloudflare are the least certain.
- **Anomaly detection is simple** (ratio to a trailing median of poll-to-poll rates). Daily-bucket
  providers produce lumpy rates; tune `multiplier`, `min_hourly_amount` or disable per provider.
  AWS anomaly is off by default because of the 24h lag.
- **Cloudflare numbers are estimates** from prices you type in.
- **Currency**: amounts are reported in the provider's currency; `total` caps just add numbers.
- **No warranty** (MIT). A missed alert is possible: outages, key rotation, API changes, your
  machine being off. Keep the providers' own alerts on as well.

## Development

```bash
python3 -m unittest -v      # all tests use mocked HTTP; real network access fails the test run
```

Layout: `spendbreaker/providers/*.py` (one file per provider), `engine.py` (caps, rate,
anomaly, de-dupe), `runner.py` (one pass), `alerts.py`, `config.py`, `cli.py`.
Adding a provider = a class with `fetch(now) -> Snapshot` and, only if the provider documents an
official control, `do_break()`.
