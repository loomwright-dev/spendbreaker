"""OpenAI: Admin API Costs endpoint (read-only) + optional API-key deletion.

Docs: https://platform.openai.com/docs/api-reference/usage/costs
      https://platform.openai.com/docs/api-reference/project-api-keys/delete
Requires an *Admin* key (sk-admin-...), created by an org owner.
"""
import datetime as dt

from .base import BreakNotSupported, Provider, ProviderError, snapshot_from_daily, utc_bounds

BASE = "https://api.openai.com/v1"


class OpenAIProvider(Provider):
    name = "openai"
    default_key_env = "OPENAI_ADMIN_KEY"
    break_actions = ("delete_project_api_keys",)

    def _headers(self):
        return {"Authorization": f"Bearer {self.secret()}"}

    def fetch(self, now):
        month_start, _, tomorrow = utc_bounds(now)
        params = {"start_time": int(month_start.timestamp()), "end_time": int(tomorrow.timestamp()),
                  "bucket_width": "1d", "limit": 31}
        if self.cfg.get("project_ids"):
            params["project_ids"] = list(self.cfg["project_ids"])
        daily, currency, page = {}, "USD", None
        for _ in range(20):  # pagination guard
            p = dict(params, **({"page": page} if page else {}))
            data = self.http.get_json(f"{BASE}/organization/costs", params=p, headers=self._headers())
            for bucket in data.get("data", []):
                day = dt.datetime.fromtimestamp(bucket["start_time"], dt.timezone.utc).strftime("%Y-%m-%d")
                for r in bucket.get("results", []):
                    amt = r.get("amount") or {}
                    daily[day] = daily.get(day, 0.0) + float(amt.get("value") or 0)
                    currency = (amt.get("currency") or currency).upper()
            if not data.get("has_more"):
                break
            page = data.get("next_page")
            if not page:
                break
        return snapshot_from_daily(self.name, daily, now, currency=currency,
                                   note="OpenAI costs API has daily granularity and may lag.")

    def do_break(self, action_cfg, dry_run=False):
        if action_cfg.get("action") != "delete_project_api_keys":
            raise BreakNotSupported(f"openai: unknown action {action_cfg.get('action')!r}")
        if action_cfg.get("confirm_irreversible") is not True:
            raise ProviderError("openai: deleting keys is irreversible; set confirm_irreversible: true")
        project, keys = action_cfg.get("project_id"), action_cfg.get("key_ids") or []
        if not project or not keys:
            raise ProviderError("openai: break needs project_id and key_ids")
        out = []
        for k in keys:
            url = f"{BASE}/organization/projects/{project}/api_keys/{k}"
            if dry_run:
                out.append(f"[dry-run] would DELETE {url}")
                continue
            self.http.request("DELETE", url, headers=self._headers())
            out.append(f"deleted OpenAI key {k} in project {project}")
        return out
