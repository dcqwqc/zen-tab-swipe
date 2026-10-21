"""Regression tests: taps must remain taps; real scroll motion must flush early."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace

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

    def test_capture_policy_avoids_duplicated_non_zen_touch(self):
        self.assertFalse(script.should_grab_touchscreen(False, True, False, False))
        self.assertFalse(script.should_grab_touchscreen(True, False, False, False))
        self.assertTrue(script.should_grab_touchscreen(True, True, False, False))
        self.assertTrue(script.should_grab_touchscreen(False, True, True, True))
        self.assertFalse(script.should_grab_touchscreen(True, True, True, False))

    def test_end_to_end_native_outside_zen_bridge_inside_zen(self):
        ev = script.ecodes
        class FakeDevice:
            def __init__(self):
                self.path = "/dev/input/fake"
                self.name = "Fake Wacom Touchscreen"
                self.fd = 77
                self.grabs = 0
                self.ungrabs = 0
                self.queue = [
                    [SimpleNamespace(type=ev.EV_ABS, code=ev.ABS_MT_SLOT, value=0),
                     SimpleNamespace(type=ev.EV_ABS, code=ev.ABS_MT_TRACKING_ID, value=8),
                     SimpleNamespace(type=ev.EV_ABS, code=ev.ABS_MT_POSITION_X, value=1000),
                     SimpleNamespace(type=ev.EV_ABS, code=ev.ABS_MT_POSITION_Y, value=1000),
                     SimpleNamespace(type=ev.EV_SYN, code=ev.SYN_REPORT, value=0)],
                    [SimpleNamespace(type=ev.EV_ABS, code=ev.ABS_MT_POSITION_Y, value=1300),
                     SimpleNamespace(type=ev.EV_SYN, code=ev.SYN_REPORT, value=0)],
                    [SimpleNamespace(type=ev.EV_ABS, code=ev.ABS_MT_TRACKING_ID, value=-1),
                     SimpleNamespace(type=ev.EV_SYN, code=ev.SYN_REPORT, value=0)]
                ]
            def capabilities(self, absinfo=False):
                return {ev.EV_ABS: [(ev.ABS_MT_POSITION_X, SimpleNamespace(min=0,max=30000)),
                                    (ev.ABS_MT_POSITION_Y, SimpleNamespace(min=0,max=18000))]}
            def read(self): return self.queue.pop(0)
            def grab(self): self.grabs += 1
            def ungrab(self): self.ungrabs += 1
            def close(self): pass
        class FakeUI:
            device = "/dev/input/virtual-test"
            def __init__(self): self.writes = []
            def write(self, *args): self.writes.append(args)
            def close(self): pass
        class FakeUInput:
            def __init__(self, ui): self.ui = ui
            def from_device(self, *args, **kwargs): return self.ui
        for is_zen in (False, True):
            with self.subTest(is_zen=is_zen):
                device = FakeDevice()
                ui = FakeUI()
                def read_ready(_read, _write, _error, _timeout):
                    if device.queue: return ([device.fd], [], [])
                    raise script.StopRequested()
                with patch.object(script, "InputDevice", return_value=device),                      patch.object(script, "UInput", FakeUInput(ui)),                      patch.object(script, "find_device", return_value=device),                      patch.object(script, "zen_active", return_value=is_zen),                      patch.object(script, "refresh_config"),                      patch.object(script, "write_state"),                      patch.object(script.signal, "signal"),                      patch.object(script.time, "sleep"),                      patch.object(script.select, "select", side_effect=read_ready):
                    script.RUNNING = True
                    script.CONFIG["enabled"] = True
                    script.main()
                if is_zen:
                    self.assertEqual(device.grabs, 1)
                    self.assertEqual(device.ungrabs, 1)
                    self.assertGreater(len(ui.writes), 0)
                else:
                    self.assertEqual(device.grabs, 0)
                    self.assertEqual(device.ungrabs, 0)
                    self.assertEqual(ui.writes, [])

if __name__ == "__main__":
    unittest.main()
