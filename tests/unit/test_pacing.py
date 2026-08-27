# P9-05 pure what-if pacing core: dates come from arithmetic, never the model.
import unittest
from datetime import datetime

from src.core.pacing import project_finish, project_milestones, pace_delta_days

NOW = datetime(2026, 8, 26, 12, 0)


class TestProjectFinish(unittest.TestCase):
    def test_simple_pace(self):
        # 12 hours at 6h/week = exactly two weeks out
        d = project_finish(12, 6, NOW)
        self.assertEqual((d - NOW).days, 14)

    def test_fractional_weeks(self):
        d = project_finish(9, 6, NOW)          # 1.5 weeks = 10.5 days
        self.assertAlmostEqual((d - NOW).total_seconds() / 86400, 10.5)

    def test_zero_pace_never_finishes(self):
        self.assertIsNone(project_finish(10, 0, NOW))
        self.assertIsNone(project_finish(10, -2, NOW))

    def test_nothing_remaining_lands_now(self):
        self.assertEqual(project_finish(0, 6, NOW), NOW)
        self.assertEqual(project_finish(-3, 0, NOW), NOW)


class TestProjectMilestones(unittest.TestCase):
    def test_sequential_accumulation(self):
        ms = [("m1", 6.0), ("m2", 6.0), ("m3", 12.0)]
        out = project_milestones(ms, 6, NOW)
        self.assertEqual([(o[0], (o[1] - NOW).days) for o in out],
                         [("m1", 7), ("m2", 14), ("m3", 28)])

    def test_zero_pace_all_none(self):
        out = project_milestones([("m1", 6.0), ("m2", 3.0)], 0, NOW)
        self.assertTrue(all(o[1] is None for o in out))

    def test_negative_remaining_clamped(self):
        out = project_milestones([("m1", -5.0), ("m2", 6.0)], 6, NOW)
        self.assertEqual((out[0][1] - NOW).days, 0)   # nothing left -> lands now
        self.assertEqual((out[1][1] - NOW).days, 7)


class TestPaceDelta(unittest.TestCase):
    def test_slower_pace_lands_later(self):
        # 12h: 6h/wk = 14 days, 4h/wk = 21 days -> +7
        self.assertAlmostEqual(pace_delta_days(12, 6, 4, NOW), 7.0)

    def test_faster_pace_lands_earlier(self):
        self.assertAlmostEqual(pace_delta_days(12, 4, 6, NOW), -7.0)

    def test_never_finishing_is_none(self):
        self.assertIsNone(pace_delta_days(12, 0, 6, NOW))
        self.assertIsNone(pace_delta_days(12, 6, 0, NOW))


if __name__ == "__main__":
    unittest.main()
