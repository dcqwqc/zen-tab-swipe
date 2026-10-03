# QWQC Two-Finger Tab Swipe

Progressive two-finger tab switching for Zen Browser on Mirai-style convertible touchscreens.

- Move two fingers together horizontally to **grab the current page**.
- The page follows the gesture continuously; the neighboring tab is shown behind it.
- Release early to cancel and spring back.
- Drag far enough (about 22% of the screen) or flick decisively to commit.
- Move the fingers apart/together instead and the sequence is passed through as native pinch zoom.
- Swipe recognition runs only while Zen is the active window.

The Sine mod renders the page preview using Firefox `PageThumbs`, while the host-side evdev proxy distinguishes direct-touchscreen swipe intent from pinch intent before Firefox converts everything into zoom.
