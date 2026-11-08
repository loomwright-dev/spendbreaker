import datetime as dt
import io
import json
import os
import tempfile
import unittest

from spendbreaker import runner
from spendbreaker.config import parse
from spendbreaker.http import Response

from .helpers import NOW, block_network, fake_client, ts

block_network()

ENV = {"OPENAI_ADMIN_KEY": "sk-admin-x", "ANTHROPIC_ADMIN_KEY": "sk-ant-admin-x",
       "NTFY_TOPIC": "topic", "AWS_ACCESS_KEY_ID": "AKIAEXAMPLE", "AWS_SECRET_ACCESS_KEY": "x"}


class Costs:
    """Mutable fake OpenAI costs endpoint: set .today to change today's spend."""

    def __init__(self, today=0.0):
        self.today = today

    def __call__(self, m, url, h, b):
        return {"has_more": False, "data": [
            {"start_time": ts(7), "results": [{"amount": {"value": self.today, "currency": "usd"}}]}]}


def anthropic_body(cents):
    return {"has_more": False, "data": [{"starting_at": "2026-11-07T00:00:00Z", "results": [{"amount": str(cents)}]}]}


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.state = os.path.join(self.tmp, "state.json")
        self.out = io.StringIO()

    def run_(self, cfg, http, now=NOW, **kw):
        return runner.run_once(parse(cfg), now=now, http=http, env=ENV, state_path=self.state,
                               stream=self.out, **kw)

    def base_routes(self, costs, anth_cents=0):
        return {("GET", "https://api.openai.com/v1/organization/costs"): costs,
                ("GET", "https://api.anthropic.com/v1/organizations/cost_report"): anthropic_body(anth_cents),
                ("POST", "https://ntfy.sh/"): "",
                ("POST", "https://api.anthropic.com/v1/organizations/api_keys/"): {"id": "k"},
                ("DELETE", "https://api.openai.com/v1/organization/projects/"): {"deleted": True}}

    def test_breach_alert_sent_once(self):
        http, t = fake_client(self.base_routes(Costs(12.0)))
        cfg = {"providers": {"openai": {"daily_cap": 10}}, "alerts": {"ntfy": {}}}
        rep = self.run_(cfg, http)
        self.assertIn("daily_breach:openai:2026-11-07", rep["sent"])
        ntfy = [c for c in t.calls if "ntfy.sh" in c["url"]]
        self.assertEqual(len(ntfy), 1)
        rep2 = self.run_(cfg, http, now=NOW + dt.timedelta(hours=1))
        self.assertEqual(rep2["sent"], [])
        self.assertEqual(len([c for c in t.calls if "ntfy.sh" in c["url"]]), 1)
        with open(self.state) as f:
            self.assertEqual(len(json.load(f)["providers"]["openai"]["samples"]), 2)
        self.assertEqual(oct(os.stat(self.state).st_mode & 0o777), "0o600")

    def test_stdout_only_is_never_marked_sent(self):
        # Without a working push channel the alert must repeat, not be silently suppressed.
        http, t = fake_client(self.base_routes(Costs(12.0)))
        cfg = {"providers": {"openai": {"daily_cap": 10}}}
        rep = self.run_(cfg, http)
        self.assertEqual(rep["sent"], [])
        self.assertTrue(any("no push alert channel" in e for e in rep["errors"]))
        rep2 = self.run_(cfg, http, now=NOW + dt.timedelta(hours=1))
        self.assertTrue(any(e["provider"] == "openai" for e in rep2["events"]))

    def test_break_off_by_default_even_when_configured(self):
        http, t = fake_client(self.base_routes(Costs(), anth_cents=5000))
        cfg = {"providers": {"anthropic": {"daily_cap": 10, "break": {"action": "deactivate_api_keys",
                                                                      "key_ids": ["apikey_1"]}}}}
        rep = self.run_(cfg, http)
        self.assertFalse(any(c["method"] == "POST" and "api_keys" in c["url"] for c in t.calls))
        self.assertIn("disabled", rep["breaks"][0])

    def test_break_on_runs_once(self):
        http, t = fake_client(self.base_routes(Costs(), anth_cents=5000))
        cfg = {"break": {"enabled": True},
               "providers": {"anthropic": {"daily_cap": 10, "break": {"action": "deactivate_api_keys",
                                                                      "key_ids": ["apikey_1"]}}}}
        rep = self.run_(cfg, http)
        posts = [c for c in t.calls if c["method"] == "POST" and "api_keys/apikey_1" in c["url"]]
        self.assertEqual(len(posts), 1)
        self.assertIn("deactivated", rep["breaks"][0])
        self.assertIn("[CRITICAL]", self.out.getvalue())
        self.run_(cfg, http, now=NOW + dt.timedelta(hours=1))
        posts = [c for c in t.calls if c["method"] == "POST" and "api_keys/apikey_1" in c["url"]]
        self.assertEqual(len(posts), 1)

    def test_break_dry_run_and_no_break(self):
        http, t = fake_client(self.base_routes(Costs(), anth_cents=5000))
        cfg = {"break": {"enabled": True},
               "providers": {"anthropic": {"daily_cap": 10, "break": {"action": "deactivate_api_keys",
                                                                      "key_ids": ["apikey_1"]}}}}
        rep = self.run_(cfg, http, dry_run=True)
        self.assertIn("dry-run", rep["breaks"][0])
        self.assertFalse(os.path.exists(self.state))
        rep = self.run_(cfg, http, no_break=True)
        self.assertIn("disabled", rep["breaks"][0])
        self.assertFalse(any(c["method"] == "POST" and "api_keys" in c["url"] for c in t.calls))

    def test_break_not_triggered_by_unlisted_event(self):
        http, t = fake_client(self.base_routes(Costs(), anth_cents=850))  # 8.50 -> warn only
        cfg = {"break": {"enabled": True},
               "providers": {"anthropic": {"daily_cap": 10, "break": {"action": "deactivate_api_keys",
                                                                      "key_ids": ["apikey_1"]}}}}
        rep = self.run_(cfg, http)
        self.assertEqual(rep["breaks"], [])
        self.assertEqual([e["kind"] for e in rep["events"]], ["daily_warn"])

    def test_anomaly_over_hourly_polls(self):
        costs = Costs(0.0)
        http, _ = fake_client(self.base_routes(costs))
        cfg = {"providers": {"openai": {}}, "anomaly": {"min_samples": 6, "min_hourly_amount": 1}}
        start = dt.datetime(2026, 11, 7, 1, 0, tzinfo=dt.timezone.utc)
        for h in range(9):  # steady 2/h
            costs.today = 2.0 * h
            rep = self.run_(cfg, http, now=start + dt.timedelta(hours=h))
            self.assertEqual([e["kind"] for e in rep["events"]], [])
        costs.today = 2.0 * 8 + 20.0  # 20/h spike
        rep = self.run_(cfg, http, now=start + dt.timedelta(hours=9))
        self.assertEqual([e["kind"] for e in rep["events"]], ["anomaly"])
        self.assertIn("10.0x", rep["events"][0]["message"])

    def test_one_provider_failing_does_not_block_others(self):
        routes = self.base_routes(Costs(12.0))
        routes[("GET", "https://api.anthropic.com/v1/organizations/cost_report")] = Response(500, "boom")
        http, _ = fake_client(routes)
        cfg = {"providers": {"openai": {"daily_cap": 10}, "anthropic": {"daily_cap": 10}}}
        rep = self.run_(cfg, http)
        kinds = sorted(e["kind"] for e in rep["events"])
        self.assertEqual(kinds, ["daily_breach", "fetch_error"])
        self.assertTrue(rep["errors"])

    def test_total_cap_across_providers(self):
        http, _ = fake_client(self.base_routes(Costs(6.0), anth_cents=600))
        cfg = {"total": {"daily_cap": 10}, "providers": {"openai": {"daily_cap": 100}, "anthropic": {}}}
        rep = self.run_(cfg, http)
        self.assertEqual([(e["provider"], e["kind"]) for e in rep["events"]], [("total", "daily_breach")])

    def test_aws_min_interval_limits_paid_calls(self):
        class CE:
            calls = 0

            def get_cost_and_usage(self, **kw):
                CE.calls += 1
                return {"ResultsByTime": [{"TimePeriod": {"Start": "2026-11-07"},
                                           "Total": {"UnblendedCost": {"Amount": "5", "Unit": "USD"}}}]}
        from spendbreaker.providers import AWSProvider
        http, _ = fake_client()
        factory = lambda n, c: AWSProvider(c, http, ENV, ce_client=CE())  # noqa: E731
        cfg = parse({"providers": {"aws": {"monthly_cap": 100}}})
        for h in range(4):
            runner.run_once(cfg, now=NOW + dt.timedelta(hours=h), http=http, env=ENV, state_path=self.state,
                            provider_factory=factory, stream=self.out)
        self.assertEqual(CE.calls, 1)  # default 360-minute interval

    def test_failed_alert_retried_next_pass(self):
        routes = self.base_routes(Costs(12.0))
        routes[("POST", "https://ntfy.sh/")] = Response(503, "down")
        http, t = fake_client(routes)
        cfg = {"providers": {"openai": {"daily_cap": 10}}, "alerts": {"ntfy": {}}}
        rep = self.run_(cfg, http)
        self.assertEqual(rep["sent"], [])
        routes[("POST", "https://ntfy.sh/")] = ""
        rep = self.run_(cfg, http, now=NOW + dt.timedelta(hours=1))
        self.assertEqual(rep["sent"], ["daily_breach:openai:2026-11-07"])


class CLITest(unittest.TestCase):
    def test_validate_and_config_error(self):
        from contextlib import redirect_stderr, redirect_stdout

        from spendbreaker.cli import main
        d = tempfile.mkdtemp()
        good, bad = os.path.join(d, "g.yaml"), os.path.join(d, "b.yaml")
        with open(good, "w") as f:
            f.write("providers:\n  openai:\n    key_env: SB_TEST_UNSET_VAR\n")
        with open(bad, "w") as f:
            f.write("providers:\n  openai:\n    api_key: sk-live\n")
        buf = io.StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            self.assertEqual(main(["validate", "-c", good]), 1)
            self.assertEqual(main(["validate", "-c", bad]), 2)
        self.assertIn("SB_TEST_UNSET_VAR", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
