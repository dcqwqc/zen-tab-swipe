#!/usr/bin/env python3
import configparser
import json
import math
import os
import signal
import subprocess
import time
from glob import glob
from pathlib import Path

from evdev import InputDevice, UInput, ecodes

DEVICE_NAME = os.environ.get("QWQC_TOUCH_DEVICE", "Wacom HID 53B7 Finger")

# Fractions of the physical touchscreen range.
SECOND_FINGER_GRACE = 0.120
SINGLE_MOVE_FLUSH = 0.010
SWIPE_INTENT_X = 0.010
SWIPE_INTENT_FINGER_X = 0.006
VERTICAL_INTENT = 0.014
PINCH_INTENT_SCALE = 0.070
CLASSIFY_TIMEOUT = 0.420
COMMIT_DISTANCE = 0.20
FAST_COMMIT_MIN_DISTANCE = 0.075
FAST_COMMIT_VELOCITY = 0.75
STATE_THROTTLE = 0.008
TRANSFORM_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "yoga-tablet-capture-transform"

RUNNING = True
CONFIG = {"enabled": True, "reverse": False}
CONFIG_MTIME = None


def active_profile_dir() -> Path | None:
    root = Path.home() / ".var/app/app.zen_browser.zen/.zen"
    cfg = configparser.ConfigParser()
    cfg.read(root / "profiles.ini")
    for section in cfg.sections():
        if section.startswith("Install") and cfg.has_option(section, "Default"):
            return root / cfg.get(section, "Default")
    for section in cfg.sections():
        if section.startswith("Profile") and cfg.get(section, "Default", fallback="0") == "1":
            return root / cfg.get(section, "Path")
    return None


PROFILE_DIR = active_profile_dir()
CONFIG_PATH = PROFILE_DIR / "chrome/qwqc-tab-swipe-config.json" if PROFILE_DIR else None
STATE_PATH = PROFILE_DIR / "chrome/qwqc-tab-swipe-state.json" if PROFILE_DIR else None


def log(*parts):
    print("[qwqc-zen-touchscreen]", *parts, flush=True)


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


def write_state(payload):
    if not STATE_PATH:
        return
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")))
        os.replace(tmp, STATE_PATH)
    except Exception as exc:
        log("state write failed:", exc)


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



def read_transform():
    try:
        return int(TRANSFORM_PATH.read_text().strip()) % 4
    except Exception:
        return 0


def screen_vector(dx, dy, transform):
    # Match Wayland/Hyprland's output transform so horizontal means horizontal
    # on the screen even when the convertible is in portrait.
    if transform == 1:
        return dy, -dx
    if transform == 2:
        return -dx, -dy
    if transform == 3:
        return -dy, dx
    return dx, dy

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
        name="QWQC Zen Touchscreen Swipe",
        phys="qwqc/zen-touchscreen-swipe",
        filtered_types=(ecodes.EV_SYN,),
    )
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
    mode = "idle"  # idle | single | two | swipe | pass | suppress
    buffer = []
    gesture_start = 0.0
    single_start = None
    two_slots = None
    two_start = None
    two_start_dist = None
    two_start_time = 0.0
    swipe_seq = 0
    swipe_last_dx = 0.0
    swipe_last_time = 0.0
    swipe_velocity = 0.0
    last_state_write = 0.0

    def active_contacts():
        out = {}
        for slot, st in slots.items():
            if st.get("id", -1) >= 0 and st.get("x") is not None and st.get("y") is not None:
                out[slot] = (st["x"], st["y"])
        return out

    def emit(events):
        for event in events:
            ui.write(event.type, event.code, event.value)

    def flush_buffer():
        nonlocal buffer
        if buffer:
            emit(buffer)
            buffer = []

    def reset(clear_state=False):
        nonlocal mode, buffer, gesture_start, single_start, two_slots, two_start
        nonlocal two_start_dist, two_start_time, swipe_last_dx, swipe_last_time
        nonlocal swipe_velocity, last_state_write
        mode = "idle"
        buffer = []
        gesture_start = 0.0
        single_start = None
        two_slots = None
        two_start = None
        two_start_dist = None
        two_start_time = 0.0
        swipe_last_dx = 0.0
        swipe_last_time = 0.0
        swipe_velocity = 0.0
        last_state_write = 0.0
        if clear_state:
            write_state({"phase": "idle", "seq": swipe_seq, "source": "touchscreen", "updatedAt": time.time_ns()})

    def begin_two(contacts, now):
        nonlocal mode, two_slots, two_start, two_start_dist, two_start_time
        mode = "two"
        two_slots = tuple(sorted(contacts))
        two_start = {slot: contacts[slot] for slot in two_slots}
        a, b = (two_start[s] for s in two_slots)
        two_start_dist = norm_distance(a, b, xr, yr)
        two_start_time = now

    def geometry(contacts):
        p0 = two_start[two_slots[0]]
        p1 = two_start[two_slots[1]]
        c0 = contacts[two_slots[0]]
        c1 = contacts[two_slots[1]]
        start_cx = (p0[0] + p1[0]) / 2
        start_cy = (p0[1] + p1[1]) / 2
        cx = (c0[0] + c1[0]) / 2
        cy = (c0[1] + c1[1]) / 2
        raw_dx = (cx - start_cx) / xr
        raw_dy = (cy - start_cy) / yr
        raw_d0x = (c0[0] - p0[0]) / xr
        raw_d1x = (c1[0] - p1[0]) / xr
        raw_d0y = (c0[1] - p0[1]) / yr
        raw_d1y = (c1[1] - p1[1]) / yr
        transform = read_transform()
        dx, dy = screen_vector(raw_dx, raw_dy, transform)
        d0x, d0y = screen_vector(raw_d0x, raw_d0y, transform)
        d1x, d1y = screen_vector(raw_d1x, raw_d1y, transform)
        dist = norm_distance(c0, c1, xr, yr)
        scale_change = 0.0
        if two_start_dist and two_start_dist > 0.005:
            scale_change = dist / two_start_dist - 1.0
        return dx, dy, d0x, d1x, d0y, d1y, scale_change

    def publish_swipe(phase, dx, dy=0.0, velocity=0.0, commit=None, reason=None, force=False):
        nonlocal last_state_write
        now = time.monotonic()
        if not force and phase == "update" and now - last_state_write < STATE_THROTTLE:
            return
        payload = {
            "phase": phase,
            "seq": swipe_seq,
            "delta": round(dx, 6),
            "vertical": round(dy, 6),
            "velocity": round(velocity, 6),
            "source": "touchscreen",
            "updatedAt": time.time_ns(),
        }
        if commit is not None:
            payload["commit"] = bool(commit)
        if reason:
            payload["reason"] = reason
        write_state(payload)
        last_state_write = now

    write_state({"phase": "idle", "seq": 0, "source": "touchscreen", "updatedAt": time.time_ns()})

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
                if not (zen_active() and CONFIG.get("enabled", True)):
                    emit(current_frame)
                    mode = "pass"
                    continue
                buffer.extend(current_frame)
                gesture_start = now
                if count == 1:
                    mode = "single"
                    single_start = next(iter(contacts.values()))
                elif count == 2:
                    begin_two(contacts, now)
                else:
                    flush_buffer()
                    mode = "pass"
                continue

            if mode == "pass":
                emit(current_frame)
                if count == 0:
                    reset(clear_state=False)
                continue

            if mode == "suppress":
                if count == 0:
                    reset(clear_state=False)
                continue

            if mode == "swipe":
                # Never expose any of this gesture to Firefox. It is now fully
                # owned by the interactive tab-switch preview.
                if count == 2 and two_slots and all(slot in contacts for slot in two_slots):
                    dx, dy, *_rest = geometry(contacts)
                    dt = max(1e-3, now - swipe_last_time)
                    inst_velocity = (dx - swipe_last_dx) / dt
                    # Smooth velocity enough that a tiny final wobble doesn't
                    # accidentally turn a deliberate flick into a cancel.
                    swipe_velocity = swipe_velocity * 0.72 + inst_velocity * 0.28
                    swipe_last_dx = dx
                    swipe_last_time = now
                    publish_swipe("update", dx, dy, swipe_velocity)
                    continue

                commit = (
                    abs(swipe_last_dx) >= COMMIT_DISTANCE
                    or (
                        abs(swipe_last_dx) >= FAST_COMMIT_MIN_DISTANCE
                        and abs(swipe_velocity) >= FAST_COMMIT_VELOCITY
                        and swipe_last_dx * swipe_velocity > 0
                    )
                )
                publish_swipe(
                    "end",
                    swipe_last_dx,
                    velocity=swipe_velocity,
                    commit=commit,
                    reason="release",
                    force=True,
                )
                log(
                    "swipe end",
                    f"dx={swipe_last_dx:.3f}",
                    f"v={swipe_velocity:.2f}",
                    "commit" if commit else "cancel",
                )
                mode = "suppress" if count else "idle"
                if count == 0:
                    reset(clear_state=False)
                continue

            buffer.extend(current_frame)

            if mode == "single":
                if count == 0:
                    flush_buffer()
                    reset(clear_state=False)
                    continue
                if count >= 3:
                    flush_buffer()
                    mode = "pass"
                    continue
                if count == 2:
                    begin_two(contacts, now)
                    continue
                pos = next(iter(contacts.values()))
                dx = abs(pos[0] - single_start[0]) / xr
                dy = abs(pos[1] - single_start[1]) / yr
                if max(dx, dy) >= SINGLE_MOVE_FLUSH or now - gesture_start >= SECOND_FINGER_GRACE:
                    flush_buffer()
                    mode = "pass"
                continue

            # mode == "two": classify the intent before Firefox sees the
            # sequence. Horizontal coherent movement wins over small incidental
            # spacing changes, which is what the old version got wrong.
            if count != 2 or not two_slots or any(slot not in contacts for slot in two_slots):
                flush_buffer()
                mode = "pass" if count else "idle"
                if count == 0:
                    reset(clear_state=False)
                continue

            dx, dy, d0x, d1x, d0y, d1y, scale_change = geometry(contacts)
            same_x = d0x * d1x > 0 and min(abs(d0x), abs(d1x)) >= SWIPE_INTENT_FINGER_X
            same_y = d0y * d1y > 0
            opposite_x = d0x * d1x < 0

            horizontal_intent = (
                abs(dx) >= SWIPE_INTENT_X
                and abs(dx) > abs(dy) * 1.10
                and same_x
            )
            clear_pinch = (
                abs(scale_change) >= PINCH_INTENT_SCALE
                and (opposite_x or abs(dx) < SWIPE_INTENT_X * 0.75)
            )
            vertical_intent = (
                abs(dy) >= VERTICAL_INTENT
                and abs(dy) > abs(dx) * 1.12
                and same_y
            )

            if horizontal_intent:
                swipe_seq += 1
                buffer = []
                mode = "swipe"
                swipe_last_dx = dx
                swipe_last_time = now
                swipe_velocity = 0.0
                publish_swipe("begin", dx, dy, 0.0, reason="horizontal-intent", force=True)
                log(
                    "swipe begin",
                    f"dx={dx:.3f}",
                    f"dy={dy:.3f}",
                    f"scale={scale_change:.3f}",
                )
                continue

            if clear_pinch:
                log("pinch pass", f"scale={scale_change:.3f}", f"dx={dx:.3f}")
                flush_buffer()
                mode = "pass"
                continue

            if vertical_intent:
                flush_buffer()
                mode = "pass"
                continue

            if now - two_start_time >= CLASSIFY_TIMEOUT:
                log("ambiguous pass", f"dx={dx:.3f}", f"dy={dy:.3f}", f"scale={scale_change:.3f}")
                flush_buffer()
                mode = "pass"
                continue

    finally:
        try:
            if mode == "swipe":
                publish_swipe("end", swipe_last_dx, velocity=0.0, commit=False, reason="service-stop", force=True)
            dev.ungrab()
        except Exception:
            pass
        ui.close()
        dev.close()
        log("stopped")


if __name__ == "__main__":
    main()
