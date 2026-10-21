"""Ensure system 3/4-finger sequences cannot leak a page pinch into Zen."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/touchscreen-swipe.py"
spec = importlib.util.spec_from_file_location("zen_touchscreen_multifinger", SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
ev = m.ecodes


def event(type_, code, value):
    return SimpleNamespace(type=type_, code=code, value=value)


def contact(slot, tracking_id, x, y):
    return [event(ev.EV_ABS, ev.ABS_MT_SLOT, slot),
            event(ev.EV_ABS, ev.ABS_MT_TRACKING_ID, tracking_id),
            event(ev.EV_ABS, ev.ABS_MT_POSITION_X, x),
            event(ev.EV_ABS, ev.ABS_MT_POSITION_Y, y),
            event(ev.EV_SYN, ev.SYN_REPORT, 0)]


class MultifingerTest(unittest.TestCase):
    def test_two_finger_pinch_does_not_leak_before_third_contact(self):
        class FakeUI:
            device = "/dev/input/test-virtual"
            def __init__(self):
                self.writes = []
            def write(self, *args):
                self.writes.append(args)
            def close(self):
                pass

        class FakeDevice:
            path = "/dev/input/test-hardware"
            name = "Wacom HID 53B7 Finger"
            fd = 71
            def __init__(self, ui):
                self.ui = ui
                self.grabbed = False
                self.pre_third_emits = None
                self.queue = [
                    contact(0, 10, 10000, 10000),
                    contact(1, 11, 12000, 10000),
                    [event(ev.EV_ABS, ev.ABS_MT_SLOT, 0),
                     event(ev.EV_ABS, ev.ABS_MT_POSITION_X, 8500),
                     event(ev.EV_ABS, ev.ABS_MT_SLOT, 1),
                     event(ev.EV_ABS, ev.ABS_MT_POSITION_X, 13500),
                     event(ev.EV_SYN, ev.SYN_REPORT, 0)],
                    contact(2, 12, 9000, 10000),
                    contact(3, 13, 15000, 10000),
                ]
                self.queue.append(sum(([event(ev.EV_ABS, ev.ABS_MT_SLOT, slot),
                                        event(ev.EV_ABS, ev.ABS_MT_TRACKING_ID, -1)]
                                        for slot in range(4)), [])
                                  + [event(ev.EV_SYN, ev.SYN_REPORT, 0)])
                self.reads = 0
            def capabilities(self, absinfo=False):
                return {ev.EV_ABS: [(ev.ABS_MT_POSITION_X, SimpleNamespace(min=0, max=30000)),
                                     (ev.ABS_MT_POSITION_Y, SimpleNamespace(min=0, max=18000))]}
            def read(self):
                self.reads += 1
                if self.reads == 4:
                    self.pre_third_emits = len(self.ui.writes)
                return self.queue.pop(0)
            def grab(self):
                self.grabbed = True
            def ungrab(self):
                self.grabbed = False
            def close(self):
                pass

        ui = FakeUI()
        dev = FakeDevice(ui)
        class UInputFactory:
            @staticmethod
            def from_device(*args, **kwargs):
                return ui
        def ready(*args):
            if dev.queue:
                return ([dev.fd], [], [])
            raise m.StopRequested()

        with patch.object(m, "find_device", return_value=dev), \
             patch.object(m, "UInput", UInputFactory), \
             patch.object(m, "zen_active", return_value=True), \
             patch.object(m, "refresh_config"), \
             patch.object(m, "write_state"), \
             patch.object(m.time, "sleep"), \
             patch.object(m.time, "monotonic", return_value=100.0), \
             patch.object(m.select, "select", side_effect=ready), \
             patch.object(m.signal, "signal"):
            m.RUNNING = True
            m.CONFIG["enabled"] = True
            m.main()
        self.assertEqual(dev.pre_third_emits, 0, "browser got pinch before 3rd finger")
        self.assertGreater(len(ui.writes), 0, "3/4-finger contacts were not forwarded to compositor")
        self.assertFalse(dev.grabbed, "device was not released on stop")


if __name__ == "__main__":
    unittest.main()
