"""Vercel: billing charges API (read-only, FOCUS-format JSONL) + optional project pause.

Docs: https://vercel.com/docs/rest-api  (billing "charges", projects "pause")
NOTE: endpoint shapes were written from public docs and NOT verified against a live
account by the author; please open an issue if they drift.
Vercel's own Spend Management can also auto-pause; prefer it if you are on Pro.
"""
import json

from .base import BreakNotSupported, Provider, ProviderError, snapshot_from_daily, utc_bounds

BASE = "https://api.vercel.com"


class VercelProvider(Provider):
    name = "vercel"
    default_key_env = "VERCEL_TOKEN"
    break_actions = ("pause_projects",)

    def _headers(self):
        return {"Authorization": f"Bearer {self.secret()}"}

    def _team(self):
        t = self.cfg.get("team_id")
        return {"teamId": t} if t else {}

    def fetch(self, now):
        month_start, _, tomorrow = utc_bounds(now)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        params = dict(self._team(), **{"from": month_start.strftime(fmt), "to": tomorrow.strftime(fmt)})
        resp = self.http.request("GET", f"{BASE}/v1/billing/charges", params=params, headers=self._headers())
        daily, currency = {}, "USD"
        for line in resp.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError as e:
                raise ProviderError(f"vercel: unparseable charges line: {line[:120]!r}") from e
            d = str(rec.get("ChargePeriodStart", ""))[:10]
            if not d:
                continue
            daily[d] = daily.get(d, 0.0) + float(rec.get("BilledCost") or 0)
            currency = (rec.get("BillingCurrency") or currency).upper()
        return snapshot_from_daily(self.name, daily, now, currency=currency,
                                   note="Vercel charges are aggregated by Vercel and may lag.")

    def do_break(self, action_cfg, dry_run=False):
        if action_cfg.get("action") != "pause_projects":
            raise BreakNotSupported(f"vercel: unknown action {action_cfg.get('action')!r}")
        projects = action_cfg.get("project_ids") or []
        if not projects:
            raise ProviderError("vercel: break needs project_ids")
        # Never validated against a live account: refuse real pauses unless explicitly opted in.
        if not dry_run and not action_cfg.get("experimental"):
            raise BreakNotSupported("vercel: pause_projects is unvalidated; set experimental: true to enable")
        out = []
        for p in projects:
            url = f"{BASE}/v1/projects/{p}/pause"
            if dry_run:
                out.append(f"[dry-run] would POST {url}")
                continue
            self.http.request("POST", url, params=self._team(), headers=self._headers(), json_body={})
            out.append(f"paused Vercel project {p} (unpause via dashboard or /unpause)")
        return out
