#!/usr/bin/env bash
set -euo pipefail

APP_ID="auto-power-profile-kde"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/$APP_ID"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP_ID"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/$APP_ID"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PURGE=false

for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=true ;;
        -h|--help)
            printf 'Usage: %s [--purge]\n' "$0"
            printf '  default: remove service, daemon files and KWin package; preserve config/state\n'
            printf '  --purge: also remove user configuration and runtime state\n'
            exit 0
            ;;
        *) printf 'Unknown argument: %s\n' "$arg" >&2; exit 2 ;;
    esac
done

# Stopping the service runs its normal brightness-restore and D-Bus cleanup.
systemctl --user disable --now "$APP_ID.service" 2>/dev/null || true

# Disable only this KWin script in kwinrc. KWin Scripts remains the control
# surface; this avoids leaving an enabled flag for a package that is removed.
if command -v kwriteconfig6 >/dev/null 2>&1; then
    kwriteconfig6 --file kwinrc --group Plugins --key "${APP_ID}Enabled" false
fi

# Best-effort cleanup of a legacy dynamically loaded script from older builds.
if command -v busctl >/dev/null 2>&1; then
    busctl --user call org.kde.KWin /Scripting org.kde.kwin.Scripting \
        unloadScript s "$APP_ID" >/dev/null 2>&1 || true
fi

if command -v qdbus6 >/dev/null 2>&1; then
    qdbus6 org.kde.KWin /KWin reconfigure >/dev/null 2>&1 || true
elif command -v qdbus >/dev/null 2>&1; then
    qdbus org.kde.KWin /KWin reconfigure >/dev/null 2>&1 || true
fi

if command -v kpackagetool6 >/dev/null 2>&1; then
    kpackagetool6 --type=KWin/Script --remove "$APP_ID" >/dev/null 2>&1 || true
fi

rm -f "$UNIT_DIR/$APP_ID.service"
rm -rf "$APP_DIR"
rm -f "$UNIT_DIR/graphical-session.target.wants/$APP_ID.service"
systemctl --user daemon-reload

if [[ "$PURGE" == true ]]; then
    rm -rf "$CONFIG_DIR" "$STATE_DIR"
    printf 'Removed application, KWin package, service, configuration and runtime state.\n'
else
    printf 'Removed application, KWin package and user service.\n'
    printf 'Preserved configuration: %s/config.ini\n' "$CONFIG_DIR"
    printf 'Preserved runtime state directory, if present: %s\n' "$STATE_DIR"
    printf 'Use --purge to also remove configuration and runtime state.\n'
fi
