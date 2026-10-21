"""Regression tests: taps must remain taps; real scroll motion must flush early."""
import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/touchscreen-swipe.py"
spec = importlib.util.spec_from_file_location("zen_touchscreen_swipe", SOURCE)
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

class NativeScrollPriority(unittest.TestCase):
    def test_jitter_does_not_consume_a_tap(self):
        self.assertFalse(script.single_touch_should_pass(0.001, 0.0015, 0.025))
        self.assertFalse(script.single_touch_should_pass(0, 0, 0.045))

    def test_small_vertical_swipe_flushes_without_waiting_for_timeout(self):
        self.assertTrue(script.single_touch_should_pass(0.001, 0.004, 0.020))

    def test_small_horizontal_swipe_flushes_without_waiting_for_timeout(self):
        self.assertTrue(script.single_touch_should_pass(0.0036, 0.0005, 0.020))

    def test_stationary_contact_is_forwarded_after_short_grace(self):
        self.assertTrue(script.single_touch_should_pass(0, 0, 0.080))

    def test_two_finger_and_pinch_thresholds_are_unchanged(self):
        self.assertEqual(script.SWIPE_INTENT_X, 0.010)
        self.assertEqual(script.PINCH_INTENT_SCALE, 0.070)
        self.assertEqual(script.VERTICAL_INTENT, 0.014)

if __name__ == "__main__":
    unittest.main()
