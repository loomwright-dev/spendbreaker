import json
import unittest
import urllib.parse

from spendbreaker.http import HTTPError, Response
from spendbreaker.providers import (AnthropicProvider, AWSProvider, CloudflareProvider, OpenAIProvider,
                                    ProviderError, VercelProvider)

from .helpers import NOW, block_network, fake_client, ts

block_network()


def qs(url):
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)


class OpenAITest(unittest.TestCase):
    def test_costs_parsed_and_paginated(self):
        page1 = {"object": "page", "has_more": True, "next_page": "p2", "data": [
            {"object": "bucket", "start_time": ts(1), "end_time": ts(2), "results": [
                {"object": "organization.costs.result", "amount": {"value": 1.5, "currency": "usd"}},
                {"object": "organization.costs.result", "amount": {"value": 0.5, "currency": "usd"}}]}]}
        page2 = {"object": "page", "has_more": False, "next_page": None, "data": [
            {"object": "bucket", "start_time": ts(7), "end_time": ts(8), "results": [
                {"amount": {"value": 4.25, "currency": "usd"}}]}]}

        def route(m, url, h, b):
            return page2 if "page=p2" in url else page1
        http, t = fake_client({("GET", "https://api.openai.com/v1/organization/costs"): route})
        snap = OpenAIProvider({}, http, env={"OPENAI_ADMIN_KEY": "sk-admin-x"}).fetch(NOW)
        self.assertAlmostEqual(snap.day_total, 4.25)
        self.assertAlmostEqual(snap.month_total, 6.25)
        self.assertEqual(snap.currency, "USD")
        self.assertEqual(len(t.calls), 2)
        q = qs(t.calls[0]["url"])
        self.assertEqual(q["bucket_width"], ["1d"])
        self.assertEqual(q["start_time"], [str(ts(1))])
        self.assertEqual(t.calls[0]["headers"]["Authorization"], "Bearer sk-admin-x")
        self.assertTrue(all(c["method"] == "GET" for c in t.calls))

    def test_missing_key(self):
        http, _ = fake_client()
        with self.assertRaises(ProviderError):
            OpenAIProvider({}, http, env={}).fetch(NOW)

    def test_http_error_raises(self):
        http, _ = fake_client({("GET", "https://api.openai.com"): Response(401, '{"error":"bad key"}')})
        with self.assertRaises(HTTPError):
            OpenAIProvider({}, http, env={"OPENAI_ADMIN_KEY": "k"}).fetch(NOW)

    def test_break_requires_irreversible_confirmation(self):
        http, t = fake_client({("DELETE", "https://api.openai.com"): {"deleted": True}})
        p = OpenAIProvider({}, http, env={"OPENAI_ADMIN_KEY": "k"})
        cfg = {"action": "delete_project_api_keys", "project_id": "proj_1", "key_ids": ["key_1"]}
        with self.assertRaises(ProviderError):
            p.do_break(cfg)
        self.assertEqual(t.calls, [])
        out = p.do_break(dict(cfg, confirm_irreversible=True))
        self.assertEqual(t.calls[0]["method"], "DELETE")
        self.assertTrue(t.calls[0]["url"].endswith("/organization/projects/proj_1/api_keys/key_1"))
        self.assertIn("key_1", out[0])


class AnthropicTest(unittest.TestCase):
    def test_cost_report_cents(self):
        body = {"has_more": False, "next_page": None, "data": [
            {"starting_at": "2026-11-06T00:00:00Z", "ending_at": "2026-11-07T00:00:00Z",
             "results": [{"currency": "USD", "amount": "1234.5"}]},
            {"starting_at": "2026-11-07T00:00:00Z", "ending_at": "2026-11-08T00:00:00Z",
             "results": [{"currency": "USD", "amount": "250"}, {"currency": "USD", "amount": "50"}]}]}
        http, t = fake_client({("GET", "https://api.anthropic.com/v1/organizations/cost_report"): body})
        snap = AnthropicProvider({}, http, env={"ANTHROPIC_ADMIN_KEY": "sk-ant-admin-x"}).fetch(NOW)
        self.assertAlmostEqual(snap.day_total, 3.0)
        self.assertAlmostEqual(snap.month_total, 15.345)
        h = t.calls[0]["headers"]
        self.assertEqual(h["x-api-key"], "sk-ant-admin-x")
        self.assertEqual(h["anthropic-version"], "2023-06-01")
        self.assertEqual(qs(t.calls[0]["url"])["starting_at"], ["2026-11-01T00:00:00Z"])

    def test_deactivate_and_dry_run(self):
        http, t = fake_client({("POST", "https://api.anthropic.com/v1/organizations/api_keys/"): {"id": "apikey_1"}})
        p = AnthropicProvider({}, http, env={"ANTHROPIC_ADMIN_KEY": "k"})
        cfg = {"action": "deactivate_api_keys", "key_ids": ["apikey_1"]}
        out = p.do_break(cfg, dry_run=True)
        self.assertEqual(t.calls, [])
        self.assertIn("dry-run", out[0])
        p.do_break(cfg)
        self.assertEqual(json.loads(t.calls[0]["body"]), {"status": "inactive"})


class CloudflareTest(unittest.TestCase):
    def test_estimate_with_free_allowance(self):
        body = {"data": {"viewer": {"accounts": [{"workersInvocationsAdaptive": [
            {"sum": {"requests": 8_000_000}, "dimensions": {"date": "2026-11-01"}},
            {"sum": {"requests": 4_000_000}, "dimensions": {"date": "2026-11-06"}},
            {"sum": {"requests": 5_000_000}, "dimensions": {"date": "2026-11-07"}}]}]}}, "errors": None}
        http, t = fake_client({("POST", "https://api.cloudflare.com/client/v4/graphql"): body})
        snap = CloudflareProvider({"account_id": "acc"}, http, env={"CLOUDFLARE_API_TOKEN": "t"}).fetch(NOW)
        self.assertTrue(snap.estimated)
        # 10M free: day1 8M free, day6 2M free + 2M paid, day7 5M paid -> 7M * 0.30
        self.assertAlmostEqual(snap.month_total, 2.1)
        self.assertAlmostEqual(snap.day_total, 1.5)
        sent = json.loads(t.calls[0]["body"])
        self.assertEqual(sent["variables"]["start"], "2026-11-01")

    def test_graphql_errors(self):
        http, _ = fake_client({("POST", "https://api.cloudflare.com"): {"data": None, "errors": [{"message": "x"}]}})
        with self.assertRaises(ProviderError):
            CloudflareProvider({"account_id": "a"}, http, env={"CLOUDFLARE_API_TOKEN": "t"}).fetch(NOW)

    def test_no_break(self):
        http, _ = fake_client()
        with self.assertRaises(ProviderError):
            CloudflareProvider({}, http, env={}).do_break({"action": "x"})


class VercelTest(unittest.TestCase):
    def test_jsonl_charges(self):
        lines = "\n".join(json.dumps(r) for r in [
            {"BilledCost": 1.25, "BillingCurrency": "USD", "ChargePeriodStart": "2026-11-03T00:00:00Z"},
            {"BilledCost": 0.75, "BillingCurrency": "USD", "ChargePeriodStart": "2026-11-07T00:00:00Z"},
            {"BilledCost": 2, "BillingCurrency": "USD", "ChargePeriodStart": "2026-11-07T00:00:00Z"}]) + "\n"
        http, t = fake_client({("GET", "https://api.vercel.com/v1/billing/charges"): lines})
        snap = VercelProvider({"team_id": "team_1"}, http, env={"VERCEL_TOKEN": "v"}).fetch(NOW)
        self.assertAlmostEqual(snap.day_total, 2.75)
        self.assertAlmostEqual(snap.month_total, 4.0)
        self.assertEqual(qs(t.calls[0]["url"])["teamId"], ["team_1"])

    def test_pause(self):
        http, t = fake_client({("POST", "https://api.vercel.com/v1/projects/"): {}})
        VercelProvider({"team_id": "team_1"}, http, env={"VERCEL_TOKEN": "v"}).do_break(
            {"action": "pause_projects", "project_ids": ["prj_1"]})
        self.assertIn("/v1/projects/prj_1/pause?teamId=team_1", t.calls[0]["url"])


class FakeCE:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get_cost_and_usage(self, **kw):
        self.calls.append(kw)
        return self.pages[len(self.calls) - 1]


class AWSTest(unittest.TestCase):
    def test_cost_explorer(self):
        ce = FakeCE([
            {"ResultsByTime": [{"TimePeriod": {"Start": "2026-11-01", "End": "2026-11-02"},
                                "Total": {"UnblendedCost": {"Amount": "10.5", "Unit": "USD"}}}], "NextPageToken": "n"},
            {"ResultsByTime": [{"TimePeriod": {"Start": "2026-11-07", "End": "2026-11-08"},
                                "Total": {"UnblendedCost": {"Amount": "3.5", "Unit": "USD"}}}]}])
        snap = AWSProvider({}, None, env={}, ce_client=ce).fetch(NOW)
        self.assertAlmostEqual(snap.month_total, 14.0)
        self.assertAlmostEqual(snap.day_total, 3.5)
        self.assertEqual(ce.calls[0]["Granularity"], "DAILY")
        self.assertEqual(ce.calls[0]["TimePeriod"], {"Start": "2026-11-01", "End": "2026-11-08"})
        self.assertEqual(ce.calls[1]["NextPageToken"], "n")

    def test_aws_requires_env_creds(self):
        with self.assertRaises(ProviderError):
            AWSProvider({}, None, env={}).fetch(NOW)


if __name__ == "__main__":
    unittest.main()
