#!/usr/bin/env bash
set -euo pipefail

MOD_ID="qwqc-zen-tab-swipe"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROFILE_ROOT="${ZEN_PROFILE_ROOT:-$HOME/.var/app/app.zen_browser.zen/.zen}"
PROFILES_INI="$PROFILE_ROOT/profiles.ini"

PROFILE_REL="$(python3 - "$PROFILES_INI" <<'PY'
from configparser import ConfigParser
from pathlib import Path
import sys
cfg=ConfigParser(); cfg.read(Path(sys.argv[1]))
for section in cfg.sections():
    if section.startswith('Install') and cfg.has_option(section,'Default'):
        print(cfg.get(section,'Default')); raise SystemExit
for section in cfg.sections():
    if section.startswith('Profile') and cfg.get(section,'Default',fallback='0') == '1':
        print(cfg.get(section,'Path')); raise SystemExit
raise SystemExit('No Zen profile found')
PY
)"
PROFILE="${ZEN_PROFILE:-$PROFILE_ROOT/$PROFILE_REL}"
SINE_ROOT="$PROFILE/chrome/sine-mods"
DEST="$SINE_ROOT/$MOD_ID"
mkdir -p "$DEST"

for file in theme.json preferences.json README.md LICENSE tab-swipe.uc.js style.css; do
  cp "$ROOT/$file" "$DEST/$file"
done

python3 - "$SINE_ROOT/mods.json" "$DEST/theme.json" <<'PY'
import json,sys
from pathlib import Path
mods_path=Path(sys.argv[1]); theme_path=Path(sys.argv[2])
mods_path.parent.mkdir(parents=True, exist_ok=True)
mods=json.loads(mods_path.read_text()) if mods_path.exists() else {}
theme=json.loads(theme_path.read_text())
old=mods.get(theme['id'], {})
theme['enabled']=old.get('enabled', True)
theme['no-updates']=True
theme['origin']='local'
mods[theme['id']]=theme
mods_path.write_text(json.dumps(mods, indent=2)+'\n')
PY

echo "Deployed $MOD_ID to $DEST"
