#!/usr/bin/env python3
"""Event-driven Auto Power Profile service for KDE Plasma 6 / KWin 6.

Power detection and profile-selection rules are ported from the user's
uploaded GNOME extension. UPower and power-profiles-daemon are monitored
through D-Bus PropertiesChanged. KWin calls this daemon's session-bus
interface for window lifecycle events. There are no periodic battery/window polls.
"""
import argparse
import asyncio
import configparser
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from dbus_next import BusType, Message, MessageType, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import NameFlag, RequestNameReply
from dbus_next.service import ServiceInterface, method

from logic import (
    PowerState,
    brightness_percent,
    choose_profile,
    configured_apps,
    normalize_app_id,
    normalize_window_snapshot,
    resolve_power_state,
)

LOG = logging.getLogger("auto-power-profile-kde")

UP_BUS = "org.freedesktop.UPower"
UP_ROOT = "/org/freedesktop/UPower"
UP_IFACE = "org.freedesktop.UPower"
UP_DEVICE_IFACE = "org.freedesktop.UPower.Device"
# PPD shipped multiple D-Bus interface/path generations. Probe both the
# established net.hadess API and the newer UPower-namespaced API.
PPD_ENDPOINTS = (
    ("net.hadess.PowerProfiles", "/net/hadess/PowerProfiles", "net.hadess.PowerProfiles"),
    ("net.hadess.PowerProfiles", "/org/freedesktop/UPower/PowerProfiles", "org.freedesktop.UPower.PowerProfiles"),
    ("org.freedesktop.UPower", "/org/freedesktop/UPower/PowerProfiles", "org.freedesktop.UPower.PowerProfiles"),
)
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
TRACKER_BUS_NAME = "io.github.iftikar.AutoPowerProfileKDE"
TRACKER_OBJECT_PATH = "/io/github/iftikar/AutoPowerProfileKDE"
TRACKER_INTERFACE = "io.github.iftikar.AutoPowerProfileKDE.Tracker"
BRIGHTNESS_PATH = "/org/kde/Solid/PowerManagement/Actions/BrightnessControl"
BRIGHTNESS_IFACE = "org.kde.Solid.PowerManagement.Actions.BrightnessControl"


def unwrap(value: Any) -> Any:
    return value.value if isinstance(value, Variant) else value


class DBusCallError(RuntimeError):
    pass


class KWinTrackerInterface(ServiceInterface):
    """D-Bus endpoint used by KWin to report window lifecycle events."""

    def __init__(self, service: Any):
        super().__init__(TRACKER_INTERFACE)
        self.service = service

    @method()
    async def WindowAdded(self, window_id: 's', raw_app: 's') -> 'b':
        await self.service._handle_window_event("ADD", window_id, raw_app)
        return True

    @method()
    async def WindowRemoved(self, window_id: 's', raw_app: 's') -> 'b':
        await self.service._handle_window_event("REMOVE", window_id, raw_app)
        return True

    @method()
    async def SyncWindows(self, snapshot_json: 's') -> 'b':
        """Atomically reconcile daemon state with KWin's in-memory window map."""
        await self.service._sync_window_snapshot(snapshot_json)
        return True

    @method()
    def TrackerReady(self, window_count: 'i') -> 'b':
        # Kept for compatibility with the previous dynamically-loaded tracker.
        LOG.info(
            "Legacy KWin tracker reported ready; startup snapshot contained %d windows",
            window_count,
        )
        return True


async def dbus_call(
    bus: MessageBus,
    *,
    destination: str,
    path: str,
    interface: str,
    member: str,
    signature: str = "",
    body: list[Any] | None = None,
) -> list[Any]:
    reply = await bus.call(
        Message(
            destination=destination,
            path=path,
            interface=interface,
            member=member,
            signature=signature,
            body=body or [],
        )
    )
    if reply is None:
        raise DBusCallError(f"No reply from {destination}.{interface}.{member}")
    if reply.message_type == MessageType.ERROR:
        raise DBusCallError(f"{reply.error_name}: {' '.join(map(str, reply.body or []))}")
    return reply.body or []


async def add_match(bus: MessageBus, rule: str) -> None:
    await dbus_call(
        bus,
        destination="org.freedesktop.DBus",
        path="/org/freedesktop/DBus",
        interface="org.freedesktop.DBus",
        member="AddMatch",
        signature="s",
        body=[rule],
    )


async def get_all_properties(
    bus: MessageBus, destination: str, path: str, interface: str
) -> dict[str, Any]:
    (raw,) = await dbus_call(
        bus,
        destination=destination,
        path=path,
        interface=PROPERTIES_IFACE,
        member="GetAll",
        signature="s",
        body=[interface],
    )
    return {str(key): unwrap(value) for key, value in raw.items()}


async def get_property(
    bus: MessageBus, destination: str, path: str, interface: str, name: str
) -> Any:
    (value,) = await dbus_call(
        bus,
        destination=destination,
        path=path,
        interface=PROPERTIES_IFACE,
        member="Get",
        signature="ss",
        body=[interface, name],
    )
    return unwrap(value)


class Config:
    def __init__(self, path: Path):
        self.path = path
        self.parser = configparser.ConfigParser(interpolation=None)
        if not path.exists():
            raise FileNotFoundError(
                f"Config file not found: {path}. Copy config.ini.example to config.ini first."
            )
        self.parser.read(path)

    def get(self, section: str, key: str, fallback: str) -> str:
        return self.parser.get(section, key, fallback=fallback)

    def get_bool(self, section: str, key: str, fallback: bool) -> bool:
        return self.parser.getboolean(section, key, fallback=fallback)

    def get_int(self, section: str, key: str, fallback: int) -> int:
        value = self.parser.getint(section, key, fallback=fallback)
        if not 0 <= value <= 100:
            raise ValueError(f"[{section}] {key} must be between 0 and 100")
        return value

    def app_ids(self) -> set[str]:
        if not self.get_bool("PerformanceApplications", "enabled", True):
            return set()
        return configured_apps(self.get("PerformanceApplications", "apps", ""))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as stream:
            self.parser.write(stream)
        os.replace(tmp, self.path)


class BrightnessController:
    """Controls the internal display using PowerDevil's D-Bus brightness API."""

    def __init__(self, bus: MessageBus, config: Config, state_file: Path):
        self.bus = bus
        self.config = config
        self.state_file = state_file
        self.service_names = [
            item.strip()
            for item in config.get(
                "Brightness",
                "powerdevil_services",
                "local.org_kde_powerdevil;org.kde.Solid.PowerManagement",
            ).split(";")
            if item.strip()
        ]
        self.service: str | None = None
        self.maximum: int | None = None
        self.original: int | None = None
        self.last_applied: int | None = None
        self.boot_id = self._read_boot_id()
        self.enabled = config.get_bool("Brightness", "enabled", True)
        self.restore = config.get_bool(
            "Brightness", "restore_pre_service_brightness", True
        )

    @staticmethod
    def _read_boot_id() -> str:
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            return "unknown"

    async def _method(self, method: str, signature: str = "", body: list[Any] | None = None) -> Any:
        if not self.service:
            raise DBusCallError("PowerDevil brightness service has not been selected")
        result = await dbus_call(
            self.bus,
            destination=self.service,
            path=BRIGHTNESS_PATH,
            interface=BRIGHTNESS_IFACE,
            member=method,
            signature=signature,
            body=body,
        )
        return result[0] if result else None

    def _load_saved_original(self) -> int | None:
        if not self.restore or not self.state_file.exists():
            return None
        try:
            state = json.loads(self.state_file.read_text(encoding="utf-8"))
            if state.get("boot_id") == self.boot_id:
                value = int(state["original_brightness"])
                LOG.info("Reusing pre-service brightness snapshot: %d", value)
                return value
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            LOG.warning("Ignoring invalid/stale brightness restore state")
        return None

    def _save_original(self) -> None:
        if not self.restore or self.original is None:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_file.parent, 0o700)
        temp = self.state_file.with_suffix(".tmp")
        temp.write_text(
            json.dumps(
                {"boot_id": self.boot_id, "original_brightness": self.original},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(temp, 0o600)
        os.replace(temp, self.state_file)

    async def initialize(self) -> None:
        if not self.enabled:
            LOG.info("Brightness automation disabled in config")
            return
        errors: list[str] = []
        for service in self.service_names:
            self.service = service
            try:
                maximum = await self._method("brightnessMax")
                current = await self._method("brightness")
                self.maximum = int(maximum)
                current_int = int(current)
                if self.maximum <= 0:
                    raise DBusCallError(f"Invalid PowerDevil brightnessMax: {self.maximum}")
                self.original = self._load_saved_original()
                if self.original is None:
                    self.original = current_int
                    self._save_original()
                LOG.info(
                    "PowerDevil brightness ready via %s (current=%d, max=%d, saved-original=%d)",
                    service,
                    current_int,
                    self.maximum,
                    self.original,
                )
                return
            except Exception as exc:  # try the alternate well-known name
                errors.append(f"{service}: {exc}")
                self.service = None
        LOG.warning(
            "Could not initialize PowerDevil brightness D-Bus API. Brightness automation "
            "will be skipped. Probed: %s",
            " | ".join(errors),
        )

    async def apply(self, power: PowerState) -> None:
        if not self.enabled or not self.service or not self.maximum:
            return
        if power.on_battery and power.low_battery:
            target_pct = self.config.get_int("Brightness", "low_battery_percent", 30)
        elif power.on_battery:
            target_pct = self.config.get_int("Brightness", "battery_percent", 60)
        else:
            target_pct = self.config.get_int("Brightness", "ac_percent", 100)
        target = round(self.maximum * target_pct / 100)
        if self.last_applied == target:
            return
        await self._method("setBrightnessSilent", "i", [target])
        self.last_applied = target
        LOG.info("Brightness set to %d%% (%d/%d)", target_pct, target, self.maximum)

    async def restore_original(self) -> None:
        if not self.enabled or not self.service:
            return
        if self.restore and self.original is not None:
            try:
                await self._method("setBrightnessSilent", "i", [self.original])
                LOG.info("Restored pre-service brightness: %d", self.original)
            except Exception:
                LOG.exception("Failed to restore pre-service brightness")
        try:
            self.state_file.unlink(missing_ok=True)
        except OSError:
            LOG.warning("Could not remove brightness state file %s", self.state_file)


class RefreshRateController:
    """Switches the configured KScreen output mode when AC/battery state changes."""

    def __init__(self, config: Config):
        self.enabled = config.get_bool("Display", "refresh_rate_enabled", True)
        self.output = config.get("Display", "output", "1").strip()
        self.ac_mode = config.get("Display", "ac_mode", "1920x1080@144").strip()
        self.battery_mode = config.get("Display", "battery_mode", "1920x1080@60").strip()
        self.command = config.get("Display", "kscreen_doctor", "/usr/bin/kscreen-doctor").strip()
        self.last_applied_mode: str | None = None

    async def apply(self, power: PowerState) -> None:
        if not self.enabled:
            return
        if not self.output:
            LOG.error("Refresh-rate switching is enabled, but [Display] output is empty")
            return

        target_mode = self.battery_mode if power.on_battery else self.ac_mode
        if target_mode == self.last_applied_mode:
            LOG.debug("Refresh rate already set to %s; skipping", target_mode)
            return

        command = [self.command, f"output.{self.output}.mode.{target_mode}"]
        LOG.info(
            "Applying refresh rate: output=%s mode=%s power_source=%s",
            self.output, target_mode, "battery" if power.on_battery else "AC",
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                LOG.error(
                    "KScreen rejected refresh-rate change (exit=%d): %s",
                    process.returncode,
                    stderr.decode(errors="replace").strip()
                    or stdout.decode(errors="replace").strip(),
                )
                return

            self.last_applied_mode = target_mode
            output = stdout.decode(errors="replace").strip()
            LOG.info(
                "Refresh rate applied: %s (%s)%s",
                target_mode,
                "battery" if power.on_battery else "AC",
                f" — {output}" if output else "",
            )
        except FileNotFoundError:
            LOG.exception(
                "Cannot execute kscreen-doctor at %s; set [Display] kscreen_doctor "
                "to its actual path", self.command,
            )
        except Exception:
            LOG.exception("Refresh-rate change failed for mode %s", target_mode)


class AutoPowerProfileService:
    def __init__(self, config: Config, app_dir: Path, state_dir: Path):
        self.config = config
        self.app_dir = app_dir
        self.state_dir = state_dir
        self.system_bus: MessageBus | None = None
        self.session_bus: MessageBus | None = None
        self.stop_event = asyncio.Event()
        self.check_lock = asyncio.Lock()
        self.display_path = "/org/freedesktop/UPower/devices/DisplayDevice"
        self.line_power_path: str | None = None
        self.display_props: dict[str, Any] = {}
        self.line_props: dict[str, Any] = {}
        self.ppd_props: dict[str, Any] = {}
        self.ppd_bus_name: str | None = None
        self.ppd_path: str | None = None
        self.ppd_interface: str | None = None
        self.available_profiles: set[str] = set()
        self.active_profile: str | None = None
        self.requested_profile: str | None = None
        self.current_profile: str | None = None
        self.current_power_state: dict[str, Any] = {}
        self.weak_adapter_suspected_count = 0
        self.weak_adapter_mode_active = False
        self.tracked_windows: dict[str, str] = {}
        self.window_event_lock = asyncio.Lock()
        self.kwin_snapshot_seen = False
        self.performance_apps = self.config.app_ids()
        self.tracker_interface: KWinTrackerInterface | None = None
        self.tracker_name_owned = False
        self._lap_debounce: asyncio.TimerHandle | None = None
        self.brightness: BrightnessController | None = None
        self.refresh_rate: RefreshRateController | None = None
        self._system_match_paths: set[str] = set()
        self._loop = asyncio.get_running_loop()

    async def run(self) -> None:
        self.system_bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self.session_bus = await MessageBus(bus_type=BusType.SESSION).connect()
        self.system_bus.add_message_handler(self._handle_system_message)

        # Export the session-bus API consumed by the separately managed
        # KWin Script package. The script retries until this name is available.
        self.tracker_interface = KWinTrackerInterface(self)
        self.session_bus.export(TRACKER_OBJECT_PATH, self.tracker_interface)
        name_reply = await self.session_bus.request_name(
            TRACKER_BUS_NAME,
            NameFlag.DO_NOT_QUEUE,
        )
        if name_reply not in (
            RequestNameReply.PRIMARY_OWNER,
            RequestNameReply.ALREADY_OWNER,
        ):
            raise DBusCallError(
                f"Could not own session D-Bus name {TRACKER_BUS_NAME}: {name_reply}"
            )
        self.tracker_name_owned = True
        LOG.info("KWin tracker D-Bus endpoint ready: %s", TRACKER_BUS_NAME)

        await self._initialize_upower()
        await self._initialize_power_profiles()
        self.brightness = BrightnessController(
            self.session_bus,
            self.config,
            self.state_dir / "brightness-state.json",
        )
        await self.brightness.initialize()
        self.refresh_rate = RefreshRateController(self.config)
        LOG.info(
            "Refresh-rate automation %s (output=%s, AC=%s, battery=%s)",
            "enabled" if self.refresh_rate.enabled else "disabled",
            self.refresh_rate.output or "(unset)",
            self.refresh_rate.ac_mode,
            self.refresh_rate.battery_mode,
        )

        # The KWin script is a separately packaged KPackage enabled by the
        # user in System Settings. It retries SyncWindows until this D-Bus
        # endpoint is available; Python does not load or own the script.
        await self.check_profile(source="initial", count_weak_event=True)
        LOG.info(
            "Running event-driven. Performance app IDs (normalized): %s",
            ", ".join(sorted(self.performance_apps)) or "(none configured)",
        )
        if self.performance_apps:
            LOG.info(
                "Performance-app profiles: AC=%s, battery=%s",
                self.config.get("PerformanceApplications", "ac_profile", "performance"),
                self.config.get("PerformanceApplications", "battery_profile", "balanced"),
            )
        await self.stop_event.wait()

    async def _initialize_upower(self) -> None:
        # Find UPower's composite display device, as the GNOME implementation does.
        (display_path,) = await dbus_call(
            self.system_bus,
            destination=UP_BUS,
            path=UP_ROOT,
            interface=UP_IFACE,
            member="GetDisplayDevice",
        )
        self.display_path = str(display_path)
        (device_paths,) = await dbus_call(
            self.system_bus,
            destination=UP_BUS,
            path=UP_ROOT,
            interface=UP_IFACE,
            member="EnumerateDevices",
        )

        for path in device_paths:
            try:
                props = await get_all_properties(
                    self.system_bus, UP_BUS, str(path), UP_DEVICE_IFACE
                )
                # Type 1 = LINE_POWER, matching the uploaded extension.
                if int(props.get("Type", 0)) == 1:
                    self.line_power_path = str(path)
                    self.line_props = props
                    break
            except Exception as exc:
                LOG.debug("Could not inspect UPower device %s: %s", path, exc)

        # Install event subscriptions before taking initial property snapshots.
        await self._add_upower_match(self.display_path)
        if self.line_power_path:
            await self._add_upower_match(self.line_power_path)
        else:
            LOG.warning("UPower did not expose a LINE_POWER device; using battery-state fallback")

        self.display_props = await get_all_properties(
            self.system_bus, UP_BUS, self.display_path, UP_DEVICE_IFACE
        )
        if self.line_power_path:
            self.line_props = await get_all_properties(
                self.system_bus, UP_BUS, self.line_power_path, UP_DEVICE_IFACE
            )

        LOG.info(
            "UPower initialized (display=%s, line-power=%s)",
            self.display_path,
            self.line_power_path or "not found",
        )

    async def _add_upower_match(self, path: str) -> None:
        if path in self._system_match_paths:
            return
        rule = (
            "type='signal',sender='org.freedesktop.UPower',"
            "interface='org.freedesktop.DBus.Properties',"
            f"member='PropertiesChanged',path='{path}'"
        )
        await add_match(self.system_bus, rule)
        self._system_match_paths.add(path)

    async def _initialize_power_profiles(self) -> None:
        errors: list[str] = []
        for bus_name, path, interface in PPD_ENDPOINTS:
            try:
                self.ppd_props = await get_all_properties(
                    self.system_bus, bus_name, path, interface
                )
                self.ppd_bus_name = bus_name
                self.ppd_path = path
                self.ppd_interface = interface
                rule = (
                    f"type='signal',sender='{bus_name}',"
                    "interface='org.freedesktop.DBus.Properties',"
                    f"member='PropertiesChanged',path='{path}'"
                )
                await add_match(self.system_bus, rule)
                LOG.info(
                    "Using power-profiles-daemon D-Bus endpoint %s %s %s",
                    bus_name, path, interface
                )
                break
            except Exception as exc:
                errors.append(f"{bus_name} {path} {interface}: {exc}")
                self.ppd_props = {}
        else:
            raise DBusCallError(
                "Could not find a supported power-profiles-daemon D-Bus endpoint: "
                + " | ".join(errors)
            )

        self.active_profile = str(self.ppd_props.get("ActiveProfile", "")) or None
        self._update_available_profiles(self.ppd_props.get("Profiles", []))
        if not self.active_profile:
            raise DBusCallError("power-profiles-daemon did not report ActiveProfile")
        LOG.info(
            "power-profiles-daemon active profile=%s; available=%s",
            self.active_profile,
            ", ".join(sorted(self.available_profiles)) or "unknown",
        )
        if not self.available_profiles:
            LOG.warning("Profiles property was empty; profile requests will still be validated by D-Bus")
        drivers = self._profile_drivers(self.ppd_props.get("Profiles", []))
        if not any(driver and driver != "placeholder" for driver in drivers):
            LOG.warning("No non-placeholder power profile driver was reported")

    @staticmethod
    def _profile_drivers(raw_profiles: Any) -> list[str]:
        drivers: list[str] = []
        for entry in raw_profiles or []:
            if isinstance(entry, dict):
                for key in ("Driver", "PlatformDriver", "CpuDriver"):
                    value = unwrap(entry.get(key))
                    if value is not None:
                        drivers.append(str(value))
        return drivers

    def _update_available_profiles(self, raw_profiles: Any) -> None:
        profiles: set[str] = set()
        for entry in raw_profiles or []:
            if isinstance(entry, dict):
                profile = unwrap(entry.get("Profile"))
                if profile:
                    profiles.add(str(profile))
        self.available_profiles = profiles

    def _handle_system_message(self, message: Message) -> bool:
        if (
            message.message_type != MessageType.SIGNAL
            or message.interface != PROPERTIES_IFACE
            or message.member != "PropertiesChanged"
            or not message.body
        ):
            return False

        if message.path == self.display_path or message.path == self.line_power_path:
            asyncio.create_task(self._handle_upower_properties(message))
            return False
        if message.path == self.ppd_path and message.sender:
            asyncio.create_task(self._handle_ppd_properties(message))
        return False

    async def _handle_upower_properties(self, message: Message) -> None:
        try:
            interface_name, changed, invalidated = message.body
            if interface_name != UP_DEVICE_IFACE:
                return
            target = self.display_props if message.path == self.display_path else self.line_props
            target.update({key: unwrap(value) for key, value in changed.items()})
            if invalidated:
                refreshed = await get_all_properties(
                    self.system_bus, UP_BUS, message.path, UP_DEVICE_IFACE
                )
                target.update(refreshed)
            is_display_event = message.path == self.display_path
            await self.check_profile(
                source="upower-display" if is_display_event else "upower-line-power",
                count_weak_event=is_display_event,
            )
        except Exception:
            LOG.exception("Failed to process UPower PropertiesChanged")

    async def _handle_ppd_properties(self, message: Message) -> None:
        try:
            interface_name, changed, invalidated = message.body
            if interface_name != self.ppd_interface:
                return
            self.ppd_props.update({key: unwrap(value) for key, value in changed.items()})
            if "Profiles" in changed:
                self._update_available_profiles(self.ppd_props.get("Profiles", []))
            if invalidated:
                refreshed = await get_all_properties(
                    self.system_bus, self.ppd_bus_name, self.ppd_path, self.ppd_interface
                )
                self.ppd_props.update(refreshed)
                self._update_available_profiles(self.ppd_props.get("Profiles", []))

            if "Profiles" in changed or "Profiles" in invalidated:
                await self.check_profile(source="power-profiles-list-changed")

            if "ActiveProfile" in changed:
                self.active_profile = str(self.ppd_props.get("ActiveProfile", "")) or None
                await self._on_active_profile_changed()

            degraded = unwrap(changed.get("PerformanceDegraded")) if "PerformanceDegraded" in changed else None
            if degraded:
                await self._on_performance_degraded(str(degraded))
        except Exception:
            LOG.exception("Failed to process power-profiles-daemon PropertiesChanged")

    async def _handle_window_event(
        self, action: str, window_id: str, raw_app: str
    ) -> None:
        app_id = normalize_app_id(raw_app)
        matches = app_id in self.performance_apps
        LOG.debug(
            "KWin event received: action=%s window_id=%s app_id_raw=%r "
            "app_id_normalized=%r configured_performance_app=%s",
            action, window_id, raw_app, app_id, matches,
        )

        # KWin may send several D-Bus method calls close together. Serialize
        # state updates so zero-to-one / one-to-zero transitions are reliable.
        async with self.window_event_lock:
            before = bool(self.tracked_windows)

            if action == "ADD":
                if matches:
                    previous = self.tracked_windows.get(window_id)
                    if previous != app_id:
                        self.tracked_windows[window_id] = app_id
                        LOG.info(
                            "PERFORMANCE APP OPENED: app_id=%s raw_id=%s "
                            "window_id=%s tracked_performance_windows=%d",
                            app_id, raw_app, window_id, len(self.tracked_windows),
                        )
                    else:
                        LOG.debug(
                            "Duplicate performance-window ADD ignored: app_id=%s window_id=%s",
                            app_id, window_id,
                        )
                else:
                    LOG.debug(
                        "Window is not configured as a performance app: app_id=%s configured=%s",
                        app_id or "(empty)",
                        ", ".join(sorted(self.performance_apps)) or "(none)",
                    )

            elif action == "REMOVE":
                removed = self.tracked_windows.pop(window_id, None)
                if removed:
                    LOG.info(
                        "PERFORMANCE APP CLOSED: app_id=%s window_id=%s "
                        "remaining_performance_windows=%d",
                        removed, window_id, len(self.tracked_windows),
                    )
                else:
                    LOG.debug(
                        "Window REMOVE had no tracked performance-app entry: app_id=%s window_id=%s",
                        app_id or "(empty)", window_id,
                    )
            else:
                LOG.warning("Ignoring unknown KWin window action: %s", action)
                return

            after = bool(self.tracked_windows)
            if before != after:
                selected_profile = self._choose_profile(self._read_power_state())
                LOG.info(
                    "PERFORMANCE APP STATE CHANGED: active=%s tracked_windows=%d selected_profile=%s",
                    after, len(self.tracked_windows), selected_profile,
                )
                await self.check_profile(source="performance-app-window")

    async def _sync_window_snapshot(self, snapshot_json: str) -> None:
        """Replace tracked-window state with the KWin script's current snapshot.

        This is used at startup and as a low-frequency D-Bus reconciliation
        heartbeat. KWin itself maintains the window map from lifecycle signals;
        this method does not enumerate windows or poll process state.
        """
        snapshot = normalize_window_snapshot(snapshot_json, self.performance_apps)
        async with self.window_event_lock:
            previous = self.tracked_windows
            first_snapshot = not self.kwin_snapshot_seen
            self.kwin_snapshot_seen = True
            if first_snapshot:
                LOG.info(
                    "KWin Script connected; startup snapshot has %d configured performance window(s)",
                    len(snapshot),
                )
            if previous == snapshot:
                LOG.debug(
                    "KWin window snapshot reconciled; performance windows=%d",
                    len(snapshot),
                )
                return

            was_active = bool(previous)
            self.tracked_windows = snapshot
            LOG.info(
                "KWin window snapshot reconciled: performance windows=%d",
                len(snapshot),
            )
            if was_active != bool(snapshot):
                await self.check_profile(source="kwin-window-snapshot")

    def _read_power_state(self) -> PowerState:
        ac_online_raw = self.line_props.get("Online") if self.line_power_path else None
        ac_online = bool(ac_online_raw) if ac_online_raw is not None else None
        battery_state = int(self.display_props.get("State", 0))
        warning_level = int(self.display_props.get("WarningLevel", 0))
        energy_rate = float(self.display_props.get("EnergyRate", 0.0))
        return resolve_power_state(ac_online, battery_state, warning_level, energy_rate)

    def _update_weak_adapter_state(self, power: PowerState, count_event: bool) -> None:
        enabled = self.config.get_bool("PowerProfiles", "weak_adapter_protection", True)
        if not enabled:
            self._reset_weak_adapter_state()
            return
        if power.on_battery:
            self._reset_weak_adapter_state()
        elif power.weak_adapter_suspected and count_event:
            if not self.weak_adapter_mode_active:
                self.weak_adapter_suspected_count += 1
                LOG.debug(
                    "Weak-adapter suspicion event %d/2", self.weak_adapter_suspected_count
                )
                if self.weak_adapter_suspected_count >= 2:
                    self.weak_adapter_mode_active = True
                    LOG.warning("Weak AC adapter suspected; forcing power-saver until AC is unplugged")
        elif not power.weak_adapter_suspected:
            # Match the original latch behavior: reset the counter, but keep an
            # already-active weak-adapter mode until AC is unplugged.
            self.weak_adapter_suspected_count = 0

    def _reset_weak_adapter_state(self) -> None:
        self.weak_adapter_mode_active = False
        self.weak_adapter_suspected_count = 0

    def _choose_profile(self, power: PowerState) -> str:
        return choose_profile(
            power,
            performance_apps_active=bool(self.tracked_windows),
            weak_adapter_mode=self.weak_adapter_mode_active,
            ac_profile=self.config.get("PowerProfiles", "ac_profile", "performance"),
            battery_profile=self.config.get("PowerProfiles", "battery_profile", "balanced"),
            performance_apps_ac_profile=self.config.get(
                "PerformanceApplications", "ac_profile", "performance"
            ),
            performance_apps_battery_profile=self.config.get(
                "PerformanceApplications", "battery_profile", "balanced"
            ),
            low_battery_protection=self.config.get_bool(
                "PowerProfiles", "low_battery_protection", True
            ),
        )

    async def check_profile(self, *, source: str, count_weak_event: bool = False) -> None:
        async with self.check_lock:
            power = self._read_power_state()
            self._update_weak_adapter_state(power, count_weak_event)
            perf_active = bool(self.tracked_windows)
            target = self._choose_profile(power)

            if self.brightness:
                try:
                    await self.brightness.apply(power)
                except Exception:
                    LOG.exception("Brightness update failed on %s event", source)

            if self.refresh_rate:
                try:
                    await self.refresh_rate.apply(power)
                except Exception:
                    LOG.exception("Refresh-rate update failed on %s event", source)

            new_state = {
                "on_battery": power.on_battery,
                "low_battery": power.low_battery,
                "perf_apps": perf_active,
                "weak_adapter_mode": self.weak_adapter_mode_active,
                "configured_profile": target,
            }
            changed = any(
                self.current_power_state.get(key) != new_state[key]
                for key in ("on_battery", "low_battery", "perf_apps", "weak_adapter_mode")
            )

            if changed and self.current_profile == target:
                self.current_power_state = new_state
                LOG.debug("Power state changed without profile change (%s)", source)
            elif changed or self.current_profile is None:
                if self.active_profile == target:
                    self.current_profile = target
                    self.current_power_state = new_state
                    self.requested_profile = None
                    LOG.info("Profile already correct: %s (%s)", target, source)
                else:
                    await self._switch_profile(target, source)

    async def _switch_profile(self, target: str, source: str) -> None:
        if self.available_profiles and target not in self.available_profiles:
            LOG.error(
                "Requested profile '%s' is unavailable. Available profiles: %s",
                target,
                ", ".join(sorted(self.available_profiles)),
            )
            return
        if target == self.active_profile:
            self.current_profile = target
            return
        self.requested_profile = target
        try:
            await dbus_call(
                self.system_bus,
                destination=self.ppd_bus_name,
                path=self.ppd_path,
                interface=PROPERTIES_IFACE,
                member="Set",
                signature="ssv",
                body=[self.ppd_interface, "ActiveProfile", Variant("s", target)],
            )
            LOG.info("Requested power profile: %s (%s)", target, source)
        except Exception:
            self.requested_profile = None
            LOG.exception("Failed to request power profile '%s'", target)

    async def _on_active_profile_changed(self) -> None:
        if not self.active_profile:
            return
        power = self._read_power_state()
        state = {
            "on_battery": power.on_battery,
            "low_battery": power.low_battery,
            "perf_apps": bool(self.tracked_windows),
            "weak_adapter_mode": self.weak_adapter_mode_active,
            "configured_profile": self._choose_profile(power),
        }
        if self._lap_debounce:
            self._lap_debounce.cancel()
            self._lap_debounce = None

        self.current_profile = self.active_profile
        self.current_power_state = state
        if self.current_profile == self.requested_profile:
            self.requested_profile = None
            LOG.info("Power profile change applied: %s", self.current_profile)
            return

        # Unrequested profile changes are treated as user changes, matching
        # the GNOME extension. Do not remember overrides during low battery,
        # weak-adapter mode, or while a performance application is open.
        self.requested_profile = None
        remember = self.config.get_bool("PowerProfiles", "remember_user_profile", True)
        if (
            remember
            and not power.low_battery
            and not self.weak_adapter_mode_active
            and not self.tracked_windows
        ):
            section_key = "ac_profile" if power.on_ac else "battery_profile"
            old = self.config.get("PowerProfiles", section_key, "balanced")
            if old != self.current_profile:
                self.config.parser.set("PowerProfiles", section_key, self.current_profile)
                self.config.save()
                LOG.info(
                    "Remembered manual profile change: %s will be used for %s power",
                    self.current_profile,
                    "AC" if power.on_ac else "battery",
                )

    async def _on_performance_degraded(self, reason: str) -> None:
        power = self._read_power_state()
        if power.on_ac and reason == "lap-detected":
            if self._lap_debounce:
                self._lap_debounce.cancel()
            # This is a one-shot reaction to PPD's lap-detected event, not a poll.
            self._lap_debounce = self._loop.call_later(
                10,
                lambda: asyncio.create_task(self._debounced_recheck()),
            )
            LOG.info("PPD reported lap-detected; scheduling one recheck in 10 seconds")
        elif reason:
            LOG.info("Power profile degraded: %s (active=%s)", reason, self.active_profile)

    async def _debounced_recheck(self) -> None:
        self._lap_debounce = None
        self.current_profile = None
        self.current_power_state = {}
        await self.check_profile(source="performance-degraded-debounce")

    async def stop(self) -> None:
        self.stop_event.set()

    async def cleanup(self) -> None:
        if self._lap_debounce:
            self._lap_debounce.cancel()
            self._lap_debounce = None
        if self.brightness:
            await self.brightness.restore_original()
        if self.session_bus and self.tracker_interface:
            try:
                self.session_bus.unexport(TRACKER_OBJECT_PATH, self.tracker_interface)
                if self.tracker_name_owned:
                    await self.session_bus.release_name(TRACKER_BUS_NAME)
                    self.tracker_name_owned = False
                LOG.info("Released KWin tracker D-Bus endpoint")
            except Exception:
                LOG.exception("Could not release KWin tracker D-Bus endpoint")
        for bus in (self.system_bus, self.session_bus):
            if bus:
                try:
                    bus.disconnect()
                except Exception:
                    pass


def configure_logging(config: Config) -> None:
    level_name = config.get("Logging", "level", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


async def async_main(args: argparse.Namespace) -> int:
    config = Config(args.config)
    configure_logging(config)
    app_dir = Path(__file__).resolve().parent
    state_dir = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "auto-power-profile-kde"
    service = AutoPowerProfileService(config, app_dir, state_dir)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(service.stop()))

    try:
        await service.run()
        return 0
    except asyncio.CancelledError:
        return 0
    except Exception:
        LOG.exception("Auto Power Profile KDE failed to start or run")
        return 1
    finally:
        await service.cleanup()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path.home() / ".config/auto-power-profile-kde/config.ini",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main(parse_args())))
