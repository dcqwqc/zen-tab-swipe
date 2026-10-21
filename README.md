# QWQC Two-Finger Tab Swipe

Progressive **two-finger touchpad and direct-touchscreen** tab switching for Zen Browser.

## Interaction

- Put two fingers on the touchpad **or directly on the touchscreen** and move them together horizontally.
- The current page follows the gesture continuously.
- The neighboring tab appears behind it using Firefox's real tab-preview capture.
- Recently used neighboring tabs are prewarmed and cached, so an already-rendered tab appears immediately instead of flashing the dark title fallback.
- Release early and it springs back.
- Drag about 17% of the touchpad width (~20 mm on Mirai) or make a decisive flick to commit.
- Two-finger vertical movement remains normal scrolling.
- Pinch remains pinch/zoom.
- The gesture only drives tab switching while Zen is the active window.

## Architecture

The Sine mod renders the progressive preview and owns the final tab selection. A small host-side observer reads the physical ELAN touchpad's multitouch coordinates from evdev **without grabbing the device**, so pointer movement, taps, scrolling and pinch still go through libinput normally. Firefox's own horizontal swipe command is disabled while this mod is active to avoid an immediate duplicate tab switch underneath the preview.

The deployment script installs both `qwqc-zen-touchpad-swipe.service` and `qwqc-zen-touchscreen-swipe.service`. The touchscreen service suppresses only a recognized horizontal two-finger tab gesture and passes normal touch, vertical movement, and pinch through.

## Touch-scroll priority on Mirai

The touchscreen classifier now flushes ordinary one-finger movement after ~0.35% of device travel (previously 1%) or a 75 ms second-finger grace period (previously 120 ms). This reduces initial swipe latency and accidental click/selection behavior in Zen content such as long ChatGPT conversations. Clear two-finger horizontal gestures continue to switch tabs; the physical touchpad remains a passive-observer path.

Outside Zen, the service relinquishes its exclusive touchscreen grab while idle, leaving native hardware input in charge rather than replaying a second synthetic stream into other apps. When Zen gains focus, the special two-finger tab-switch bridge is re-enabled. Capture never changes midway through a contact. This preserves system-wide native scrolling and prevents duplicate presses from the auxiliary gesture service.

The evdev reader also exits promptly on service stop and releases its exclusive device grab before restart, instead of waiting for another touch or being SIGKILLed. Test the classifier and input routing with `~/.local/share/qwqc-zen-touchpad-swipe/venv/bin/python -m unittest discover -s tests -v`.

### Three-/four-finger touchscreen system gestures

The touchscreen bridge owns physical touch events only while Zen is focused.
It buffers the beginning of a two-finger interaction for 105 ms before
classifying a tab swipe or pinch. If a third/fourth contact arrives in this
settling interval, it forwards the original ordered sequence to Hyprland,
where Hyprgrass recognizes the system-wide gesture and cancels the app's
touch sequence. This prevents a four-finger keyboard command from accidentally
starting as a browser pinch while keeping one-finger scrolling fast.

The behavior is regression-tested in `tests/test_multifinger_arbitration.py`.
