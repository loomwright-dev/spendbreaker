"""Anthropic: Admin API cost report (read-only) + optional API-key deactivation.

Docs: https://platform.claude.com/docs/en/manage-claude/usage-cost-api
      Admin API: POST /v1/organizations/api_keys/{id} {"status": "inactive"}
Requires an Admin API key (sk-ant-admin...). Deactivation is reversible in the Console.
"""
from .base import BreakNotSupported, Provider, ProviderError, snapshot_from_daily, utc_bounds

BASE = "https://api.anthropic.com/v1/organizations"
VERSION = "2023-06-01"


class AnthropicProvider(Provider):
    name = "anthropic"
    default_key_env = "ANTHROPIC_ADMIN_KEY"
    break_actions = ("deactivate_api_keys",)

    def _headers(self):
        return {"x-api-key": self.secret(), "anthropic-version": VERSION}

    def fetch(self, now):
        month_start, _, tomorrow = utc_bounds(now)
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        params = {"starting_at": month_start.strftime(fmt), "ending_at": tomorrow.strftime(fmt),
                  "bucket_width": "1d", "limit": 31}
        # The cost report returns amounts as decimal strings in the lowest currency unit (cents).
        divisor = float(self.cfg.get("amount_divisor", 100))
        daily, currency, page = {}, "USD", None
        for _ in range(20):
            p = dict(params, **({"page": page} if page else {}))
            data = self.http.get_json(f"{BASE}/cost_report", params=p, headers=self._headers())
            for bucket in data.get("data", []):
                day = bucket["starting_at"][:10]
                for r in bucket.get("results", []):
                    daily[day] = daily.get(day, 0.0) + float(r.get("amount") or 0) / divisor
                    currency = (r.get("currency") or currency).upper()
            if not data.get("has_more") or not data.get("next_page"):
                break
            page = data["next_page"]
        return snapshot_from_daily(self.name, daily, now, currency=currency,
                                   note="Anthropic cost report is daily; recent hours may lag.")

    def do_break(self, action_cfg, dry_run=False):
        if action_cfg.get("action") != "deactivate_api_keys":
            raise BreakNotSupported(f"anthropic: unknown action {action_cfg.get('action')!r}")
        keys = action_cfg.get("key_ids") or []
        if not keys:
            raise ProviderError("anthropic: break needs key_ids")
        out = []
        for k in keys:
            url = f"{BASE}/api_keys/{k}"
            if dry_run:
                out.append(f"[dry-run] would POST {url} status=inactive")
                continue
            self.http.request("POST", url, headers=self._headers(), json_body={"status": "inactive"})
            out.append(f"deactivated Anthropic key {k} (reversible in Console)")
        return out
