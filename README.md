# QWQC Two-Finger Tab Swipe

A Zen/Sine mod plus a small host-side touchscreen filter for convertible Linux devices.

- Two fingers moving together horizontally: switch to the adjacent tab.
- Finger spacing changing: pass the sequence through as native pinch zoom.
- One-finger touch: pass through unchanged after a short second-finger grace window.
- The filter only converts gestures while Zen is the active window.

The Sine settings control enable/disable and direction. A native Firefox swipe-pref mapping remains as a touchpad fallback, but direct touchscreen recognition is handled by the Wacom/evdev proxy because Firefox otherwise classifies these sequences as pinch zoom.
