import datetime as dt
import unittest

from spendbreaker import engine
from spendbreaker.providers.base import Snapshot

from .helpers import NOW

ANOM = {"enabled": True, "multiplier": 3.0, "window_samples": 168, "min_samples": 6,
        "min_hourly_amount": 1.0, "cooldown_hours": 6}


def snap(day, month=None, daily=None):
    return Snapshot("openai", day, month if month is not None else day, daily=daily or {})


class RateTest(unittest.TestCase):
    def test_rate_same_day(self):
        ps = {}
        self.assertIsNone(engine.record_sample(ps, snap(1.0), NOW))
        r = engine.record_sample(ps, snap(3.0), NOW + dt.timedelta(hours=2))
        self.assertAlmostEqual(r, 1.0)

    def test_rate_day_rollover_includes_late_prev_day_spend(self):
        ps = {}
        t0 = dt.datetime(2026, 11, 7, 23, 0, tzinfo=dt.timezone.utc)
        engine.record_sample(ps, snap(10.0), t0)
        t1 = t0 + dt.timedelta(hours=2)  # 01:00 next day
        r = engine.record_sample(ps, snap(2.0, daily={"2026-11-07": 11.0, "2026-11-08": 2.0}), t1)
        self.assertAlmostEqual(r, 1.5)  # (2 today + 1 late) / 2h

    def test_negative_delta_clamped(self):
        ps = {}
        engine.record_sample(ps, snap(5.0), NOW)
        self.assertEqual(engine.record_sample(ps, snap(4.0), NOW + dt.timedelta(hours=1)), 0.0)


class AnomalyTest(unittest.TestCase):
    def _hist(self, rates):
        return {"samples": [{"rate": r} for r in rates] + [{"rate": None}]}

    def test_fires_above_multiplier(self):
        ev = engine.anomaly_event("openai", self._hist([1, 1, 1.2, 0.8, 1, 1]), 5.0, ANOM, NOW, "USD")
        self.assertIsNotNone(ev)
        self.assertEqual(ev.kind, "anomaly")
        self.assertIn("5.0x", ev.message)

    def test_quiet_below_multiplier(self):
        self.assertIsNone(engine.anomaly_event("openai", self._hist([1] * 6), 2.9, ANOM, NOW, "USD"))

    def test_needs_history(self):
        self.assertIsNone(engine.anomaly_event("openai", self._hist([1] * 5), 50, ANOM, NOW, "USD"))

    def test_min_hourly_floor(self):
        self.assertIsNone(engine.anomaly_event("openai", self._hist([0.01] * 6), 0.5, ANOM, NOW, "USD"))

    def test_from_zero(self):
        ev = engine.anomaly_event("openai", self._hist([0] * 10), 2.0, ANOM, NOW, "USD")
        self.assertIn("from ~0", ev.message)

    def test_disabled(self):
        self.assertIsNone(engine.anomaly_event("o", self._hist([1] * 9), 99, dict(ANOM, enabled=False), NOW, "USD"))


class CapTest(unittest.TestCase):
    def kinds(self, day, month, cfg):
        return sorted(e.kind for e in engine.cap_events("openai", day, month, cfg, 0.8, NOW, "USD"))

    def test_breach_and_warn(self):
        self.assertEqual(self.kinds(10, 10, {"daily_cap": 10}), ["daily_breach"])
        self.assertEqual(self.kinds(8.5, 8.5, {"daily_cap": 10}), ["daily_warn"])
        self.assertEqual(self.kinds(1, 1, {"daily_cap": 10}), [])
        self.assertEqual(self.kinds(1, 100, {"monthly_cap": 100}), ["monthly_breach"])

    def test_projection(self):
        # Nov 7 15:00 -> 6.625 days elapsed; 50 spent -> ~226 projected
        self.assertEqual(self.kinds(1, 50, {"monthly_cap": 200}), ["monthly_projected"])
        self.assertEqual(self.kinds(1, 50, {"monthly_cap": 300}), [])

    def test_no_caps_no_events(self):
        self.assertEqual(self.kinds(1e6, 1e6, {}), [])


class DedupeTest(unittest.TestCase):
    def test_once_per_period_and_cooldown(self):
        sent = {}
        e = engine.Event("daily_breach", "openai", "m", "daily_breach:openai:2026-11-07")
        self.assertTrue(engine.should_send(e, sent, NOW))
        engine.mark_sent(e, sent, NOW)
        self.assertFalse(engine.should_send(e, sent, NOW + dt.timedelta(hours=20)))
        a = engine.Event("anomaly", "openai", "m", "anomaly:openai", cooldown_hours=6)
        engine.mark_sent(a, sent, NOW)
        self.assertFalse(engine.should_send(a, sent, NOW + dt.timedelta(hours=5)))
        self.assertTrue(engine.should_send(a, sent, NOW + dt.timedelta(hours=6)))


if __name__ == "__main__":
    unittest.main()
