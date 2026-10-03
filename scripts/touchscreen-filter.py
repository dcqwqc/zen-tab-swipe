#!/usr/bin/env python3
import json
import math
import os
import signal
import subprocess
import configparser
import sys
import time
from glob import glob
from pathlib import Path

from evdev import InputDevice, UInput, ecodes

DEVICE_NAME = os.environ.get("QWQC_TOUCH_DEVICE", "Wacom HID 53B7 Finger")

# Delay a new one-finger sequence briefly so a second finger can join without
# Firefox first seeing half of a pinch. Moving a single finger flushes earlier.
SECOND_FINGER_GRACE = 0.090
SINGLE_MOVE_FLUSH = 0.012

# All movement values below are fractions of the physical touchscreen range.
SWIPE_X = 0.042
SWIPE_FINGER_X = 0.025
DIAGONAL_FLUSH = 0.032
MAX_SCALE_CHANGE_FOR_SWIPE = 0.16
PINCH_SCALE_CHANGE = 0.105
CLASSIFY_TIMEOUT = 0.260

RUNNING = True
CONFIG = {"enabled": True, "reverse": False}
CONFIG_MTIME = None


def active_profile_config():
    root = Path.home() / ".var/app/app.zen_browser.zen/.zen"
    cfg = configparser.ConfigParser()
    cfg.read(root / "profiles.ini")
    for section in cfg.sections():
        if section.startswith("Install") and cfg.has_option(section, "Default"):
            return root / cfg.get(section, "Default") / "chrome/qwqc-tab-swipe-config.json"
    for section in cfg.sections():
        if section.startswith("Profile") and cfg.get(section, "Default", fallback="0") == "1":
            return root / cfg.get(section, "Path") / "chrome/qwqc-tab-swipe-config.json"
    return None


CONFIG_PATH = active_profile_config()


def refresh_config():
    global CONFIG_MTIME
    if not CONFIG_PATH or not CONFIG_PATH.exists():
        return
    try:
        mtime = CONFIG_PATH.stat().st_mtime_ns
        if mtime == CONFIG_MTIME:
            return
        data = json.loads(CONFIG_PATH.read_text())
        CONFIG["enabled"] = bool(data.get("enabled", True))
        CONFIG["reverse"] = bool(data.get("reverse", False))
        CONFIG_MTIME = mtime
        log("config", CONFIG)
    except Exception as exc:
        log("config read failed:", exc)


def log(*parts):
    print("[qwqc-zen-touch]", *parts, flush=True)


def find_device():
    for path in sorted(glob("/dev/input/event*")):
        try:
            dev = InputDevice(path)
        except OSError:
            continue
        if dev.name == DEVICE_NAME:
            return dev
        dev.close()
    raise RuntimeError(f"Touch device not found: {DEVICE_NAME}")


def zen_active():
    try:
        proc = subprocess.run(
            ["hyprctl", "activewindow", "-j"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.18,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return False
        data = json.loads(proc.stdout)
        klass = str(data.get("class", "")).lower()
        return "zen_browser" in klass or klass in {"zen", "zen-browser"}
    except Exception:
        return False


def switch_tab(direction):
    # User-facing direction: fingers moving right -> tab to the right.
    if CONFIG.get("reverse"):
        direction = -direction
    key = "Page_Down" if direction > 0 else "Page_Up"
    try:
        subprocess.Popen(
            ["wtype", "-M", "ctrl", "-k", key, "-m", "ctrl"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log("tab", "right" if direction > 0 else "left")
    except Exception as exc:
        log("wtype failed:", exc)


def norm_distance(a, b, xr, yr):
    dx = (a[0] - b[0]) / xr
    dy = (a[1] - b[1]) / yr
    return math.hypot(dx, dy)


def main():
    global RUNNING

    dev = find_device()
    caps = dev.capabilities(absinfo=True)
    abs_caps = dict(caps.get(ecodes.EV_ABS, []))
    xinfo = abs_caps.get(ecodes.ABS_MT_POSITION_X) or abs_caps.get(ecodes.ABS_X)
    yinfo = abs_caps.get(ecodes.ABS_MT_POSITION_Y) or abs_caps.get(ecodes.ABS_Y)
    if not xinfo or not yinfo:
        raise RuntimeError("Touchscreen has no usable absolute XY range")
    xr = max(1, xinfo.max - xinfo.min)
    yr = max(1, yinfo.max - yinfo.min)

    ui = UInput.from_device(
        dev,
        name="QWQC Zen Touch Filter",
        phys="qwqc/zen-touch-filter",
        filtered_types=(ecodes.EV_SYN,),
    )
    # Let Hyprland discover the virtual touchscreen before the physical one is grabbed.
    time.sleep(0.35)
    dev.grab()
    log(f"grabbed {dev.path} ({dev.name}); virtual={ui.device}; range={xr}x{yr}")

    def stop(_sig, _frame):
        global RUNNING
        RUNNING = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    current_slot = 0
    slots = {}
    frame = []
    mode = "idle"  # idle | single | two | pass | suppress
    buffer = []
    gesture_start = 0.0
    single_start = None
    two_slots = None
    two_start = None
    two_start_dist = None
    two_start_time = 0.0
    started_in_zen = False

    def active_contacts():
        out = {}
        for slot, st in slots.items():
            if st.get("id", -1) >= 0 and st.get("x") is not None and st.get("y") is not None:
                out[slot] = (st["x"], st["y"])
        return out

    def emit(events):
        for ev in events:
            ui.write(ev.type, ev.code, ev.value)

    def flush_buffer():
        nonlocal buffer
        if buffer:
            emit(buffer)
            buffer = []

    def reset():
        nonlocal mode, buffer, gesture_start, single_start, two_slots, two_start
        nonlocal two_start_dist, two_start_time, started_in_zen
        mode = "idle"
        buffer = []
        gesture_start = 0.0
        single_start = None
        two_slots = None
        two_start = None
        two_start_dist = None
        two_start_time = 0.0
        started_in_zen = False

    try:
        for ev in dev.read_loop():
            if not RUNNING:
                break

            frame.append(ev)

            if ev.type == ecodes.EV_ABS:
                if ev.code == ecodes.ABS_MT_SLOT:
                    current_slot = ev.value
                    slots.setdefault(current_slot, {"id": -1, "x": None, "y": None})
                elif ev.code == ecodes.ABS_MT_TRACKING_ID:
                    st = slots.setdefault(current_slot, {"id": -1, "x": None, "y": None})
                    st["id"] = ev.value
                    if ev.value < 0:
                        st["x"] = None
                        st["y"] = None
                elif ev.code == ecodes.ABS_MT_POSITION_X:
                    slots.setdefault(current_slot, {"id": -1, "x": None, "y": None})["x"] = ev.value
                elif ev.code == ecodes.ABS_MT_POSITION_Y:
                    slots.setdefault(current_slot, {"id": -1, "x": None, "y": None})["y"] = ev.value

            if ev.type != ecodes.EV_SYN or ev.code != ecodes.SYN_REPORT:
                continue

            now = time.monotonic()
            contacts = active_contacts()
            count = len(contacts)
            current_frame = frame
            frame = []

            if mode == "idle":
                if count == 0:
                    emit(current_frame)
                    continue
                refresh_config()
                started_in_zen = zen_active() and CONFIG.get("enabled", True)
                if not started_in_zen:
                    emit(current_frame)
                    mode = "pass"
                    continue
                mode = "single" if count == 1 else "two" if count == 2 else "pass"
                buffer.extend(current_frame)
                gesture_start = now
                if count == 1:
                    single_start = next(iter(contacts.values()))
                elif count == 2:
                    two_slots = tuple(sorted(contacts))
                    two_start = {slot: contacts[slot] for slot in two_slots}
                    a, b = (two_start[s] for s in two_slots)
                    two_start_dist = norm_distance(a, b, xr, yr)
                    two_start_time = now
                else:
                    flush_buffer()
                continue

            if mode == "pass":
                emit(current_frame)
                if count == 0:
                    reset()
                continue

            if mode == "suppress":
                # Entire gesture was never exposed to the compositor/browser.
                if count == 0:
                    reset()
                continue

            buffer.extend(current_frame)

            if mode == "single":
                if count == 0:
                    flush_buffer()
                    reset()
                    continue
                if count >= 3:
                    flush_buffer()
                    mode = "pass"
                    continue
                if count == 2:
                    mode = "two"
                    two_slots = tuple(sorted(contacts))
                    two_start = {slot: contacts[slot] for slot in two_slots}
                    a, b = (two_start[s] for s in two_slots)
                    two_start_dist = norm_distance(a, b, xr, yr)
                    two_start_time = now
                    continue

                pos = next(iter(contacts.values()))
                dx = abs(pos[0] - single_start[0]) / xr
                dy = abs(pos[1] - single_start[1]) / yr
                if max(dx, dy) >= SINGLE_MOVE_FLUSH or now - gesture_start >= SECOND_FINGER_GRACE:
                    flush_buffer()
                    mode = "pass"
                continue

            # mode == "two"
            if count != 2 or not two_slots or any(slot not in contacts for slot in two_slots):
                flush_buffer()
                mode = "pass" if count else "idle"
                if count == 0:
                    reset()
                continue

            p0 = two_start[two_slots[0]]
            p1 = two_start[two_slots[1]]
            c0 = contacts[two_slots[0]]
            c1 = contacts[two_slots[1]]

            start_cx = (p0[0] + p1[0]) / 2
            start_cy = (p0[1] + p1[1]) / 2
            cx = (c0[0] + c1[0]) / 2
            cy = (c0[1] + c1[1]) / 2
            dx = (cx - start_cx) / xr
            dy = (cy - start_cy) / yr

            d0x = (c0[0] - p0[0]) / xr
            d1x = (c1[0] - p1[0]) / xr
            same_x_direction = d0x * d1x > 0 and min(abs(d0x), abs(d1x)) >= SWIPE_FINGER_X

            dist = norm_distance(c0, c1, xr, yr)
            if two_start_dist and two_start_dist > 0.005:
                scale_change = abs(dist / two_start_dist - 1.0)
            else:
                scale_change = 0.0

            horizontal = (
                abs(dx) >= SWIPE_X
                and abs(dx) > abs(dy) * 1.35
                and same_x_direction
                and scale_change <= MAX_SCALE_CHANGE_FOR_SWIPE
            )

            if horizontal:
                buffer = []
                mode = "suppress"
                switch_tab(1 if dx > 0 else -1)
                continue

            # Once the fingers clearly change their spacing, treat it as a real
            # pinch and replay the exact buffered beginning to Firefox.
            if scale_change >= PINCH_SCALE_CHANGE:
                flush_buffer()
                mode = "pass"
                continue

            if max(abs(dx), abs(dy)) >= DIAGONAL_FLUSH and abs(dy) >= abs(dx) * 0.9:
                flush_buffer()
                mode = "pass"
                continue

            if now - two_start_time >= CLASSIFY_TIMEOUT:
                flush_buffer()
                mode = "pass"
                continue

    finally:
        try:
            dev.ungrab()
        except Exception:
            pass
        ui.close()
        dev.close()
        log("stopped")


if __name__ == "__main__":
    main()
