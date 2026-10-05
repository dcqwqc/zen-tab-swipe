#!/usr/bin/env python3
"""Progressive two-finger touchpad tab swipe for Zen Browser.

Reads Mirai's physical ELAN touchpad without grabbing it. Normal pointer,
scrolling, tapping and pinch continue through libinput unchanged. Only a clear
two-finger horizontal centroid movement is mirrored into Zen's progressive
preview state file.
"""

from __future__ import annotations

import configparser
import glob
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import time

EVENT = struct.Struct("llHHi")
EV_SYN = 0x00
EV_ABS = 0x03
SYN_REPORT = 0
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39

# Touchpad-relative thresholds. Mirai's ELAN pad is ~118.7 mm wide.
INTENT_X = 0.012          # ~1.4 mm of coherent horizontal movement
INTENT_FINGER_X = 0.010   # each finger must actually travel horizontally
VERTICAL_REJECT = 0.020
PINCH_REJECT_SCALE = 0.085
CLASSIFY_TIMEOUT = 0.55
COMMIT_DISTANCE = 0.17    # ~20 mm
FAST_COMMIT_MIN = 0.075   # ~8.9 mm
FAST_COMMIT_VELOCITY = 0.85
STATE_THROTTLE = 0.008

CONFIG = {"enabled": True, "reverse": False}
CONFIG_MTIME: int | None = None


def profile_dir() -> Path | None:
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


PROFILE = profile_dir()
CONFIG_PATH = PROFILE / "chrome/qwqc-tab-swipe-config.json" if PROFILE else None
STATE_PATH = PROFILE / "chrome/qwqc-tab-swipe-state.json" if PROFILE else None


def log(*parts: object) -> None:
    print("[qwqc-zen-touchpad]", *parts, flush=True)


def refresh_config() -> None:
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


def write_state(payload: dict) -> None:
    if not STATE_PATH:
        return
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")))
        os.replace(tmp, STATE_PATH)
    except Exception as exc:
        log("state write failed:", exc)


def zen_active() -> bool:
    try:
        proc = subprocess.run(
            ["hyprctl", "activewindow", "-j"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=0.12,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return False
        klass = str(json.loads(proc.stdout).get("class", "")).lower()
        return "zen_browser" in klass or klass in {"zen", "zen-browser"}
    except Exception:
        return False


def find_touchpad() -> str | None:
    override = os.environ.get("QWQC_TOUCHPAD_DEVICE")
    if override and os.path.exists(override):
        return override
    for dev in sorted(glob.glob("/dev/input/event*")):
        name_file = Path("/sys/class/input") / Path(dev).name / "device/name"
        try:
            name = name_file.read_text(errors="replace").strip().lower()
        except OSError:
            continue
        if "touchpad" in name:
            return dev
    return None


def read_abs_range(event: str) -> tuple[float, float]:
    # sysfs abs capability text doesn't expose ranges portably; use python-evdev
    # when available from the dedicated venv, otherwise Mirai's known pad range.
    try:
        from evdev import InputDevice, ecodes  # type: ignore
        dev = InputDevice(event)
        caps = dict(dev.capabilities(absinfo=True).get(ecodes.EV_ABS, []))
        x = caps[ecodes.ABS_MT_POSITION_X]
        y = caps[ecodes.ABS_MT_POSITION_Y]
        dev.close()
        return float(max(1, x.max - x.min)), float(max(1, y.max - y.min))
    except Exception:
        return 3679.0, 2261.0


def distance(a: tuple[float, float], b: tuple[float, float], xr: float, yr: float) -> float:
    return math.hypot((a[0] - b[0]) / xr, (a[1] - b[1]) / yr)


def watch(device: str) -> None:
    xr, yr = read_abs_range(device)
    log("watching", device, f"range={xr:.0f}x{yr:.0f}")

    slot = 0
    slots: dict[int, dict[str, int | None]] = {}
    mode = "idle"  # idle | classify | swipe | ignore
    two_slots: tuple[int, int] | None = None
    start_positions: dict[int, tuple[float, float]] = {}
    start_dist = 0.0
    start_time = 0.0
    seq = 0
    last_dx = 0.0
    last_time = 0.0
    velocity = 0.0
    last_state_write = 0.0

    def state_for(index: int) -> dict[str, int | None]:
        return slots.setdefault(index, {"id": None, "x": None, "y": None})

    def contacts() -> dict[int, tuple[float, float]]:
        out = {}
        for index, state in slots.items():
            if state["id"] is not None and state["x"] is not None and state["y"] is not None:
                out[index] = (float(state["x"]), float(state["y"]))
        return out

    def reset() -> None:
        nonlocal mode, two_slots, start_positions, start_dist, start_time
        nonlocal last_dx, last_time, velocity, last_state_write
        mode = "idle"
        two_slots = None
        start_positions = {}
        start_dist = 0.0
        start_time = 0.0
        last_dx = 0.0
        last_time = 0.0
        velocity = 0.0
        last_state_write = 0.0

    def begin_classify(current: dict[int, tuple[float, float]], now: float) -> None:
        nonlocal mode, two_slots, start_positions, start_dist, start_time
        mode = "classify"
        two_slots = tuple(sorted(current))  # type: ignore[assignment]
        start_positions = {i: current[i] for i in two_slots}
        start_dist = distance(start_positions[two_slots[0]], start_positions[two_slots[1]], xr, yr)
        start_time = now

    def geometry(current: dict[int, tuple[float, float]]):
        p0 = start_positions[two_slots[0]]
        p1 = start_positions[two_slots[1]]
        c0 = current[two_slots[0]]
        c1 = current[two_slots[1]]
        sx = (p0[0] + p1[0]) / 2
        sy = (p0[1] + p1[1]) / 2
        cx = (c0[0] + c1[0]) / 2
        cy = (c0[1] + c1[1]) / 2
        dx = (cx - sx) / xr
        dy = (cy - sy) / yr
        d0x = (c0[0] - p0[0]) / xr
        d1x = (c1[0] - p1[0]) / xr
        d0y = (c0[1] - p0[1]) / yr
        d1y = (c1[1] - p1[1]) / yr
        dist_now = distance(c0, c1, xr, yr)
        scale = dist_now / start_dist - 1.0 if start_dist > 0.005 else 0.0
        return dx, dy, d0x, d1x, d0y, d1y, scale

    def publish(phase: str, dx: float, dy: float = 0.0, speed: float = 0.0,
                commit: bool | None = None, reason: str | None = None, force: bool = False) -> None:
        nonlocal last_state_write
        now = time.monotonic()
        if not force and phase == "update" and now - last_state_write < STATE_THROTTLE:
            return
        payload = {
            "phase": phase,
            "seq": seq,
            "delta": round(dx, 6),
            "vertical": round(dy, 6),
            "velocity": round(speed, 6),
            "source": "touchpad",
            "updatedAt": time.time_ns(),
        }
        if commit is not None:
            payload["commit"] = commit
        if reason:
            payload["reason"] = reason
        write_state(payload)
        last_state_write = now

    write_state({"phase": "idle", "seq": 0, "source": "touchpad", "updatedAt": time.time_ns()})

    with open(device, "rb", buffering=0) as f:
        while True:
            data = f.read(EVENT.size)
            if len(data) != EVENT.size:
                raise OSError("touchpad event stream ended")
            _sec, _usec, ev_type, code, value = EVENT.unpack(data)

            if ev_type == EV_ABS:
                if code == ABS_MT_SLOT:
                    slot = value
                elif code == ABS_MT_TRACKING_ID:
                    st = state_for(slot)
                    if value < 0:
                        st["id"] = None
                        st["x"] = None
                        st["y"] = None
                    else:
                        st["id"] = value
                        st["x"] = None
                        st["y"] = None
                elif code == ABS_MT_POSITION_X:
                    state_for(slot)["x"] = value
                elif code == ABS_MT_POSITION_Y:
                    state_for(slot)["y"] = value

            if ev_type != EV_SYN or code != SYN_REPORT:
                continue

            now = time.monotonic()
            current = contacts()
            count = len(current)

            if mode == "idle":
                if count == 2:
                    refresh_config()
                    if CONFIG.get("enabled", True) and zen_active():
                        begin_classify(current, now)
                    else:
                        mode = "ignore"
                continue

            if mode == "ignore":
                if count == 0:
                    reset()
                elif count != 2:
                    mode = "ignore"
                continue

            if mode == "classify":
                if count != 2 or not two_slots or any(i not in current for i in two_slots):
                    reset()
                    continue

                dx, dy, d0x, d1x, d0y, d1y, scale = geometry(current)
                same_x = d0x * d1x > 0 and min(abs(d0x), abs(d1x)) >= INTENT_FINGER_X
                same_y = d0y * d1y > 0
                opposite_x = d0x * d1x < 0

                horizontal = abs(dx) >= INTENT_X and abs(dx) > abs(dy) * 1.15 and same_x
                pinch = abs(scale) >= PINCH_REJECT_SCALE and (opposite_x or abs(dx) < INTENT_X * 0.75)
                vertical = abs(dy) >= VERTICAL_REJECT and abs(dy) > abs(dx) * 1.15 and same_y

                if horizontal:
                    seq += 1
                    mode = "swipe"
                    last_dx = dx
                    last_time = now
                    velocity = 0.0
                    publish("begin", dx, dy, reason="touchpad-horizontal", force=True)
                    log("swipe begin", f"dx={dx:.3f}", f"dy={dy:.3f}", f"scale={scale:.3f}")
                elif pinch or vertical or now - start_time >= CLASSIFY_TIMEOUT:
                    mode = "ignore"
                continue

            if mode == "swipe":
                if count == 2 and two_slots and all(i in current for i in two_slots):
                    dx, dy, *_ = geometry(current)
                    dt = max(1e-3, now - last_time)
                    instant = (dx - last_dx) / dt
                    velocity = velocity * 0.72 + instant * 0.28
                    last_dx = dx
                    last_time = now
                    publish("update", dx, dy, velocity)
                    continue

                commit = (
                    abs(last_dx) >= COMMIT_DISTANCE
                    or (
                        abs(last_dx) >= FAST_COMMIT_MIN
                        and abs(velocity) >= FAST_COMMIT_VELOCITY
                        and last_dx * velocity > 0
                    )
                )
                publish("end", last_dx, speed=velocity, commit=commit, reason="touchpad-release", force=True)
                log("swipe end", f"dx={last_dx:.3f}", f"v={velocity:.2f}", "commit" if commit else "cancel")
                reset()


def main() -> None:
    while True:
        device = find_touchpad()
        if not device:
            log("touchpad not found")
            time.sleep(2)
            continue
        try:
            watch(device)
        except (OSError, PermissionError) as exc:
            log("reader restart:", exc)
            time.sleep(1)


if __name__ == "__main__":
    main()
