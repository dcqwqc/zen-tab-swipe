# QWQC Two-Finger Tab Swipe

Progressive **two-finger touchpad** tab switching for Zen Browser.

## Interaction

- Put two fingers on the touchpad and move them together horizontally.
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

The deployment script installs and enables `qwqc-zen-touchpad-swipe.service`, and removes/disables the older touchscreen proxy implementation automatically.
