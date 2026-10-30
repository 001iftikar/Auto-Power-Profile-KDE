# Auto Power Profile KDE

An event-driven KDE Plasma 6 / KWin 6 port of the power-state and profile-selection logic from [`dmy3k/auto-power-profile`](https://github.com/dmy3k/auto-power-profile), with optional brightness and AC/battery refresh-rate automation.

The window tracker is installed as a **normal KWin Script KPackage** and enabled/disabled through **System Settings → Window Management → KWin Scripts**. The Python daemon does not dynamically load, run, or unload KWin scripts.

## Architecture

- `daemon.py` owns the session D-Bus endpoint, listens to UPower and power-profiles-daemon, chooses the profile, and applies brightness/refresh-rate settings.
- `logic.py` contains pure power-state, application-ID and profile-selection logic.
- `kwin/auto-power-profile-kde/` is the KPackage installed by `kpackagetool6`. Its `contents/code/main.js` receives KWin window lifecycle signals and reports state to Python over the session bus.
- `config.ini.example` documents the user configuration.

KWin uses `workspace.windowAdded` and `workspace.windowRemoved`. It also observes `desktopFileNameChanged` and `windowClassChanged` because application identity may be resolved after a window is first created. It takes one initial `workspace.stackingOrder` snapshot and then updates its own in-memory map from lifecycle signals.

### Startup and recovery

KWin scripts can start before the systemd user service has acquired its D-Bus name. The KWin script therefore retries the `SyncWindows` D-Bus handshake once per second until the daemon is available. Once connected, it sends window lifecycle events immediately and sends a small reconciliation snapshot every 10 seconds. That snapshot is made from the script's existing in-memory window map; it does **not** re-enumerate windows, inspect processes, or poll battery state. It repairs state after a daemon restart or missed D-Bus event.

UPower and power-profiles-daemon are monitored using D-Bus `PropertiesChanged`; there is no recurring battery poll. Python remains the only component that selects or applies a power profile.

## What it does

- Detects AC power using UPower's `LinePower.Online` property, with a battery-state fallback.
- Uses UPower `WarningLevel` and `EnergyRate` for low-battery and weak-adapter behavior.
- Changes `power-profiles-daemon`'s `ActiveProfile` over D-Bus.
- Selects an optional performance-app profile while one or more configured application windows are open.
- Optionally adjusts display brightness through PowerDevil and restores the captured brightness on clean shutdown.
- Optionally switches a configured KScreen output mode between AC and battery modes through `kscreen-doctor`.

## Profile priority

The order follows the uploaded GNOME extension:

1. Select the configured AC or battery default.
2. On low battery, select `power-saver` when low-battery protection is enabled.
3. If one or more configured performance-app windows exist, apply the configured performance-app AC/battery profile. This override follows the original extension's ordering and therefore overrides low-battery selection.
4. Weak-adapter mode has the highest priority and forces `power-saver` until AC is unplugged.

Weak-adapter mode is activated after two qualifying UPower composite-device property-change events. It is not a wall-clock timer.

## Requirements

- KDE Plasma 6 with KWin 6
- `kpackagetool6`
- UPower
- `power-profiles-daemon` and the profiles you intend to use
- Python 3 with `venv` support
- systemd user session
- Network access during the first installation to install `dbus-next` into the private virtual environment

On an Arch-based KDE installation, the required KDE package tools are normally present with Plasma/KF6. The installer checks for `kpackagetool6`, Python, systemd and KWin.

## Clean install

From the extracted project directory:

```bash
chmod +x install.sh uninstall.sh
./uninstall.sh --purge
./install.sh
```

`--purge` removes the prior user service, old daemon files, old KWin script package, user configuration and runtime state. It also disables this script's KWin plugin key and best-effort unloads the legacy dynamically loaded script from older builds. It does not remove or reconfigure UPower, power-profiles-daemon, PowerDevil, KScreen, or unrelated KWin scripts.

The installer then:

1. Copies the Python daemon and logic into `~/.local/share/auto-power-profile-kde` (or `$XDG_DATA_HOME`).
2. Creates a private virtual environment and installs `dbus-next`.
3. Creates `~/.config/auto-power-profile-kde/config.ini` from the example only when no config exists.
4. Installs or upgrades the KWin KPackage under `~/.local/share/kwin/scripts/auto-power-profile-kde` (or `$XDG_DATA_HOME/kwin/scripts`).
5. Installs and starts the systemd user service.

**The installer deliberately does not enable the KWin Script.** Enable it yourself in System Settings, so that script activation stays under KWin's own management.

## Enable the KWin Script in System Settings

After running `./install.sh`:

1. Open **System Settings → Window Management → KWin Scripts**.
2. Find **Auto Power Profile KDE**.
3. Enable its checkbox and click **Apply**.
4. If the settings page was already open during installation and the script is not listed, leave that page and open it again. KDE's KWin scripting tutorial notes that the list may not refresh immediately after a package is installed.

The installed package can also be inspected from a terminal:

```bash
kpackagetool6 --type=KWin/Script --list
kpackagetool6 --type=KWin/Script --show auto-power-profile-kde
```

The project also includes `kwin/auto-power-profile-kde.kwinscript`, a packaged archive for the local install-from-file option in the KWin Scripts page when that option is available. The standard `install.sh` uses `kpackagetool6` to install the package directory.

KDE's documented KPackage metadata format and installation procedure are described in the [KWin scripting tutorial](https://develop.kde.org/docs/plasma/kwin/).

## Configuration

Edit:

```text
~/.config/auto-power-profile-kde/config.ini
```

Example:

```ini
[PowerProfiles]
ac_profile = balanced
battery_profile = power-saver
low_battery_protection = true
weak_adapter_protection = true
remember_user_profile = true

[PerformanceApplications]
enabled = true
apps = dev.zed.Zed
ac_profile = performance
battery_profile = balanced

[Brightness]
enabled = true
ac_percent = 100
battery_percent = 60
low_battery_percent = 30
restore_pre_service_brightness = true
powerdevil_services = local.org_kde_powerdevil;org.kde.Solid.PowerManagement

[Display]
refresh_rate_enabled = true
output = 1
ac_mode = 1920x1080@144
battery_mode = 1920x1080@60
kscreen_doctor = /usr/bin/kscreen-doctor

[Logging]
level = DEBUG
```

`apps` accepts semicolon- or comma-separated KWin `desktopFileName` IDs. The daemon normalizes full desktop-file paths, case and `.desktop` suffixes. Use DEBUG logs to see the raw and normalized ID reported by KWin:

```bash
journalctl --user -u auto-power-profile-kde.service -f
```

After editing the configuration, restart the daemon:

```bash
systemctl --user restart auto-power-profile-kde.service
```

## Refresh-rate switching

Check output IDs and supported modes:

```bash
kscreen-doctor -o
```

Test the exact modes manually before enabling refresh-rate automation:

```bash
kscreen-doctor output.1.mode.1920x1080@144
kscreen-doctor output.1.mode.1920x1080@60
```

Use the output number and mode strings reported by your own `kscreen-doctor -o` output.

## Brightness and PowerDevil

The example config sets brightness to 100% on AC, 60% on battery and 30% on low battery, then restores the captured pre-service brightness on clean shutdown. The daemon probes the PowerDevil D-Bus service names listed under `[Brightness]`. Brightness support is best-effort because the older PowerDevil brightness D-Bus API is deprecated and exposure may differ by Plasma build.

Keep PowerDevil running. In **System Settings → Power Management → Energy Saving**, disable only the automatic brightness and automatic power-profile switching that would compete with this service. Keep unrelated PowerDevil behavior such as suspend, lid handling and charge thresholds enabled.

## Service and diagnostics

```bash
systemctl --user status auto-power-profile-kde.service
journalctl --user -u auto-power-profile-kde.service -f
systemctl --user restart auto-power-profile-kde.service
systemctl --user stop auto-power-profile-kde.service
```

The Python endpoint should expose these methods:

```bash
busctl --user introspect \
  io.github.iftikar.AutoPowerProfileKDE \
  /io/github/iftikar/AutoPowerProfileKDE
```

Expected methods include `WindowAdded`, `WindowRemoved`, `SyncWindows` and the backward-compatible `TrackerReady`.

For KWin JavaScript output, KDE documents:

```bash
journalctl -f QT_CATEGORY=js QT_CATEGORY=kwin_scripting
```

If KWin script output is absent, enable KWin Scripting debug output using `kdebugsettings` or `QT_LOGGING_RULES="kwin_*.debug=true"` and then log out/in. The Python daemon logs are the primary verification for successful D-Bus events and profile changes.

## Uninstall

Preserve config and state:

```bash
./uninstall.sh
```

Remove the application, KWin package, service, config and state:

```bash
./uninstall.sh --purge
```

For a manual baseline reset after stopping the service:

```bash
powerprofilesctl set balanced
```

## Known limitations

- The daemon uses KWin's `desktopFileName` as the application identity. Flatpak, AppImage and custom launchers may expose a different ID; use the DEBUG log and configure the reported value.
- PowerDevil brightness D-Bus support varies by build and is best-effort.
- A forced kill or power loss cannot run brightness restoration. A same-boot restart can reuse the saved original brightness snapshot.
- The original GNOME extension's preferences UI and notifications are not ported.

## Tests

Run these checks from the project root:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile daemon.py logic.py
bash -n install.sh uninstall.sh
node --check kwin/auto-power-profile-kde/contents/code/main.js
python3 -m json.tool kwin/auto-power-profile-kde/metadata.json >/dev/null
```

The local checks cover Python syntax, pure logic tests, shell syntax, JavaScript syntax and package metadata JSON. Live KWin package activation and D-Bus event delivery must still be verified in a running Plasma session.
