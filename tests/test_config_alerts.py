import io
import json
import unittest

from spendbreaker.alerts import NtfySink, TelegramSink, build_sinks
from spendbreaker.config import ConfigError, parse
from spendbreaker.engine import Event

from .helpers import block_network, fake_client

block_network()


class ConfigTest(unittest.TestCase):
    def test_defaults_break_off(self):
        c = parse({"providers": {"openai": {"daily_cap": 5}}})
        self.assertFalse(c["break"]["enabled"])
        self.assertEqual(c["anomaly"]["multiplier"], 3.0)

    def test_inline_secret_rejected(self):
        for bad in ({"providers": {"openai": {"api_key": "abc"}}},
                    {"providers": {"openai": {}}, "alerts": {"telegram": {"bot_token": "123"}}},
                    {"providers": {"openai": {"key_env": "sk-proj-abcdef"}}}):
            with self.assertRaises(ConfigError):
                parse(bad)

    def test_env_name_allowed(self):
        parse({"providers": {"openai": {"key_env": "MY_OPENAI_ADMIN"}}})

    def test_unknown_provider_and_bad_action(self):
        with self.assertRaises(ConfigError):
            parse({"providers": {"gcp": {}}})
        with self.assertRaises(ConfigError):
            parse({"providers": {"cloudflare": {"break": {"action": "pause_projects"}}}})
        with self.assertRaises(ConfigError):
            parse({"providers": {"openai": {"daily_cap": -1}}})
        with self.assertRaises(ConfigError):
            parse({"providers": {"openai": {}}, "break": {"enabled": "yes"}})

    def test_break_default_triggers(self):
        c = parse({"providers": {"anthropic": {"break": {"action": "deactivate_api_keys", "key_ids": ["a"]}}}})
        self.assertEqual(c["providers"]["anthropic"]["break"]["on"], ["daily_breach", "monthly_breach"])

    def test_disabled_provider_dropped(self):
        with self.assertRaises(ConfigError):
            parse({"providers": {"openai": {"enabled": False}}})

    def test_example_config_parses(self):
        import os
        import yaml
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "config.example.yaml")) as f:
            c = parse(yaml.safe_load(f))
        self.assertFalse(c["break"]["enabled"])


EV = Event("daily_breach", "openai", "openai: daily spend 12.00 USD >= cap 10.00", "k", "critical")


class AlertTest(unittest.TestCase):
    def test_telegram_request(self):
        http, t = fake_client({("POST", "https://api.telegram.org/"): {"ok": True}})
        TelegramSink({}, http, {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"}).send(EV)
        self.assertEqual(t.calls[0]["url"], "https://api.telegram.org/bot123:abc/sendMessage")
        body = json.loads(t.calls[0]["body"])
        self.assertEqual(body["chat_id"], "42")
        self.assertIn("[CRITICAL]", body["text"])

    def test_ntfy_request(self):
        http, t = fake_client({("POST", "https://ntfy.example/"): ""})
        NtfySink({"server": "https://ntfy.example/"}, http, {"NTFY_TOPIC": "sb-secret", "NTFY_ACCESS_TOKEN": "tk"}).send(EV)
        c = t.calls[0]
        self.assertEqual(c["url"], "https://ntfy.example/sb-secret")
        self.assertEqual(c["headers"]["Priority"], "5")
        self.assertEqual(c["headers"]["Authorization"], "Bearer tk")
        self.assertIn(b"cap 10.00", c["body"])

    def test_missing_env_reported_not_fatal(self):
        http, _ = fake_client()
        sinks, errs = build_sinks({"stdout": True, "telegram": {}, "ntfy": {}}, http, {}, io.StringIO())
        self.assertEqual([s.name for s in sinks], ["stdout"])
        self.assertEqual(len(errs), 2)

    def test_telegram_token_redacted_in_errors(self):
        from spendbreaker.http import HTTPError, Response
        http, _ = fake_client({("POST", "https://api.telegram.org/"): Response(400, "bad")})
        s = TelegramSink({}, http, {"TELEGRAM_BOT_TOKEN": "123:SECRET", "TELEGRAM_CHAT_ID": "1"})
        with self.assertRaises(HTTPError) as cm:
            s.send(EV)
        self.assertNotIn("SECRET", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
