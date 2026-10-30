#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="auto-power-profile-kde"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_ID"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP_ID"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
KWIN_PACKAGE_DIR="$PROJECT_DIR/kwin/$APP_ID"

for command in python3 systemctl kpackagetool6; do
    if ! command -v "$command" >/dev/null 2>&1; then
        printf 'Required command not found: %s\n' "$command" >&2
        exit 1
    fi
done

if ! command -v kwin_wayland >/dev/null 2>&1 && ! command -v kwin_x11 >/dev/null 2>&1; then
    printf 'KWin was not found. This project requires KDE Plasma 6 / KWin 6.\n' >&2
    exit 1
fi

if [[ ! -f "$KWIN_PACKAGE_DIR/metadata.json" || ! -f "$KWIN_PACKAGE_DIR/contents/code/main.js" ]]; then
    printf 'KWin package is incomplete: %s\n' "$KWIN_PACKAGE_DIR" >&2
    exit 1
fi

# Stop the previous daemon before replacing either side of the D-Bus contract.
systemctl --user stop "$APP_ID.service" 2>/dev/null || true

# Remove a script left behind by the old daemon-managed loader. This does not
# touch unrelated KWin scripts.
if command -v busctl >/dev/null 2>&1; then
    busctl --user call org.kde.KWin /Scripting org.kde.kwin.Scripting \
        unloadScript s "$APP_ID" >/dev/null 2>&1 || true
fi

mkdir -p "$APP_DIR" "$CONFIG_DIR" "$UNIT_DIR"
install -m 0644 "$PROJECT_DIR/daemon.py" "$APP_DIR/daemon.py"
install -m 0644 "$PROJECT_DIR/logic.py" "$APP_DIR/logic.py"
install -m 0644 "$PROJECT_DIR/requirements.txt" "$APP_DIR/requirements.txt"

if [[ ! -f "$CONFIG_DIR/config.ini" ]]; then
    install -m 0600 "$PROJECT_DIR/config.ini.example" "$CONFIG_DIR/config.ini"
    printf 'Created configuration: %s\n' "$CONFIG_DIR/config.ini"
else
    printf 'Keeping existing configuration: %s\n' "$CONFIG_DIR/config.ini"
fi

if [[ ! -d "$APP_DIR/.venv" ]]; then
    python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"

# Install or update the normal KPackage. It is deliberately NOT enabled here:
# the user enables/disables it from System Settings > Window Management > KWin Scripts.
if kpackagetool6 --type=KWin/Script --show "$APP_ID" >/dev/null 2>&1; then
    kpackagetool6 --type=KWin/Script --upgrade "$KWIN_PACKAGE_DIR"
else
    kpackagetool6 --type=KWin/Script --install "$KWIN_PACKAGE_DIR"
fi

cat > "$UNIT_DIR/$APP_ID.service" <<UNIT
[Unit]
Description=Event-driven Auto Power Profile for KDE Plasma
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/daemon.py --config $CONFIG_DIR/config.ini
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
UNIT

systemctl --user daemon-reload
systemctl --user enable --now "$APP_ID.service"

cat <<EOF

Auto Power Profile KDE is installed.

Daemon files:   $APP_DIR
KWin package:   ${XDG_DATA_HOME:-$HOME/.local/share}/kwin/scripts/$APP_ID
Configuration:  $CONFIG_DIR/config.ini
Service:        $APP_ID.service

Next steps:
  1. Open System Settings > Window Management > KWin Scripts.
  2. Enable "Auto Power Profile KDE" and click Apply.
  3. Configure application IDs in $CONFIG_DIR/config.ini.
  4. Restart the daemon after changing the INI file:
       systemctl --user restart $APP_ID.service
  5. Inspect daemon logs:
       journalctl --user -u $APP_ID.service -f

The KWin script is intentionally not enabled automatically; its activation is
managed by the KWin Scripts settings page. It retries the daemon D-Bus handshake
until the service is available.

EOF
