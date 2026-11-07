"""AWS: Cost Explorer GetCostAndUsage (read-only), via boto3 (optional dependency).

WARNING: each Cost Explorer API request costs USD 0.01, and data lags up to ~24h.
spendbreaker therefore polls AWS at most every `min_interval_minutes` (default 360).
Credentials come ONLY from AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN
env vars (we do not read ~/.aws). Minimal IAM: ce:GetCostAndUsage.
No break action: AWS has no single official "stop spending" API; configure AWS Budgets
Actions inside AWS if you want automatic enforcement.
"""
import datetime as dt

from .base import Provider, ProviderError, snapshot_from_daily, utc_bounds


class AWSProvider(Provider):
    name = "aws"
    default_key_env = "AWS_ACCESS_KEY_ID"
    default_min_interval_minutes = 360

    def __init__(self, cfg, http, env=None, ce_client=None):
        super().__init__(cfg, http, env)
        self._ce = ce_client

    def _client(self):
        if self._ce is not None:
            return self._ce
        try:
            import boto3  # optional
        except ImportError as e:
            raise ProviderError("aws: pip install boto3 to enable the AWS provider") from e
        key = self.secret("key_env", "AWS_ACCESS_KEY_ID")
        sec = self.secret("secret_env", "AWS_SECRET_ACCESS_KEY")
        token = self.env.get(self.cfg.get("session_token_env", "AWS_SESSION_TOKEN")) or None
        sess = boto3.session.Session(aws_access_key_id=key, aws_secret_access_key=sec,
                                     aws_session_token=token, region_name="us-east-1")
        return sess.client("ce")

    def fetch(self, now):
        month_start, _, tomorrow = utc_bounds(now)
        ce = self._client()
        metric = self.cfg.get("metric", "UnblendedCost")
        args = {"TimePeriod": {"Start": month_start.strftime("%Y-%m-%d"), "End": tomorrow.strftime("%Y-%m-%d")},
                "Granularity": "DAILY", "Metrics": [metric]}
        daily, currency, token = {}, "USD", None
        for _ in range(10):
            resp = ce.get_cost_and_usage(**args, **({"NextPageToken": token} if token else {}))
            for r in resp.get("ResultsByTime", []):
                m = r.get("Total", {}).get(metric, {})
                daily[r["TimePeriod"]["Start"]] = daily.get(r["TimePeriod"]["Start"], 0.0) + float(m.get("Amount") or 0)
                currency = m.get("Unit") or currency
            token = resp.get("NextPageToken")
            if not token:
                break
        return snapshot_from_daily(self.name, daily, now, currency=currency,
                                   note="AWS Cost Explorer lags up to ~24h; each request costs $0.01.")
