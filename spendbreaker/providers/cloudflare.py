"""Cloudflare: ESTIMATE from Workers request counts (GraphQL Analytics API, read-only).

Cloudflare does not (as far as we could verify) expose an API that returns current
billed spend, so this provider multiplies Workers requests by prices YOU configure.
It ignores CPU time, R2, D1, KV, bandwidth, etc. No break action: Cloudflare has no
official "pause account/worker" switch; use Cloudflare's own budget alerts too.
Docs: https://developers.cloudflare.com/analytics/graphql-api/
"""
from .base import Provider, ProviderError, snapshot_from_daily, utc_bounds

URL = "https://api.cloudflare.com/client/v4/graphql"
QUERY = """query($accountTag: string!, $start: Date!, $end: Date!) {
  viewer { accounts(filter: {accountTag: $accountTag}) {
    workersInvocationsAdaptive(limit: 10000, filter: {date_geq: $start, date_leq: $end}) {
      sum { requests }
      dimensions { date }
    } } } }"""


class CloudflareProvider(Provider):
    name = "cloudflare"
    default_key_env = "CLOUDFLARE_API_TOKEN"

    def fetch(self, now):
        account = self.cfg.get("account_id") or self.env.get(self.cfg.get("account_id_env", "CLOUDFLARE_ACCOUNT_ID"), "")
        if not account:
            raise ProviderError("cloudflare: account_id (or CLOUDFLARE_ACCOUNT_ID) is required")
        price = float(self.cfg.get("price_per_million_requests", 0.30))
        included = float(self.cfg.get("included_requests_per_month", 10_000_000))
        month_start, day, _ = utc_bounds(now)
        body = {"query": QUERY, "variables": {"accountTag": account,
                "start": month_start.strftime("%Y-%m-%d"), "end": day.strftime("%Y-%m-%d")}}
        data = self.http.request("POST", URL, headers={"Authorization": f"Bearer {self.secret()}"},
                                 json_body=body).json()
        if data.get("errors"):
            raise ProviderError(f"cloudflare: GraphQL errors: {data['errors']}")
        accounts = (data.get("data") or {}).get("viewer", {}).get("accounts") or []
        reqs = {}
        for acct in accounts:
            for row in acct.get("workersInvocationsAdaptive") or []:
                d = row["dimensions"]["date"]
                reqs[d] = reqs.get(d, 0) + int(row["sum"]["requests"])
        # Allocate the free allowance to the earliest days of the month, then price the rest.
        daily, remaining = {}, included
        for d in sorted(reqs):
            free = min(remaining, reqs[d])
            remaining -= free
            daily[d] = (reqs[d] - free) / 1e6 * price
        snap = snapshot_from_daily(self.name, daily, now, estimated=True,
                                   note=f"ESTIMATE: Workers requests only at ${price}/M after {included:,.0f} included.")
        return snap
