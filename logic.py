"""Pure power-state logic ported from dmy3k/auto-power-profile."""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

# UPower org.freedesktop.UPower.Device enum values.
STATE_DISCHARGING = 2
STATE_PENDING_DISCHARGE = 6
WARNING_LOW = 3
WARNING_CRITICAL = 4
WARNING_ACTION = 5


@dataclass(frozen=True)
class PowerState:
    on_battery: bool
    on_ac: bool
    low_battery: bool
    weak_adapter_suspected: bool


def resolve_power_state(
    ac_online: bool | None,
    battery_state: int,
    warning_level: int,
    energy_rate: float,
) -> PowerState:
    """Mirror the uploaded extension's AC-first, battery-state-fallback logic."""
    battery_discharging = battery_state in (
        STATE_PENDING_DISCHARGE,
        STATE_DISCHARGING,
    )
    on_battery = (not ac_online) if ac_online is not None else battery_discharging
    low_battery = on_battery and warning_level in (
        WARNING_LOW,
        WARNING_CRITICAL,
        WARNING_ACTION,
    )
    weak_adapter = (
        ac_online is True
        and battery_discharging
        and abs(float(energy_rate)) > 1.0
    )
    return PowerState(
        on_battery=on_battery,
        on_ac=not on_battery,
        low_battery=low_battery,
        weak_adapter_suspected=weak_adapter,
    )


def normalize_app_id(raw: str | None) -> str:
    """Normalize KWin desktopFileName and configured .desktop IDs."""
    value = (raw or "").strip().replace("\\", "/")
    if "/" in value:
        value = value.rsplit("/", 1)[-1]
    if value.lower().endswith(".desktop"):
        value = value[:-8]
    return value.casefold()


def configured_apps(raw: str) -> set[str]:
    """Parse semicolon/comma-separated desktop IDs."""
    entries = raw.replace(",", ";").split(";")
    return {normalized for item in entries if (normalized := normalize_app_id(item))}


def normalize_window_snapshot(raw_snapshot: str, performance_apps: set[str]) -> dict[str, str]:
    """Parse a KWin snapshot and retain only configured performance windows.

    The KWin script sends a JSON object mapping KWin internal window UUIDs to
    desktop-file IDs. Keeping this parser independent makes the daemon's
    reconciliation behavior straightforward to test without a live D-Bus bus.
    """
    try:
        payload = json.loads(raw_snapshot)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON from KWin window tracker: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("KWin window snapshot must be a JSON object")

    normalized: dict[str, str] = {}
    for window_id, raw_app in payload.items():
        if not isinstance(window_id, str) or not window_id:
            continue
        if not isinstance(raw_app, str):
            continue
        app_id = normalize_app_id(raw_app)
        if app_id and app_id in performance_apps:
            normalized[window_id] = app_id
    return normalized


def choose_profile(
    power: PowerState,
    *,
    performance_apps_active: bool,
    weak_adapter_mode: bool,
    ac_profile: str,
    battery_profile: str,
    performance_apps_ac_profile: str,
    performance_apps_battery_profile: str,
    low_battery_protection: bool = True,
) -> str:
    """Preserve extension priority, including its performance-app override.

    The uploaded source applies performance-app overrides *after* low-battery
    selection, then gives weak-adapter protection the highest priority.
    """
    if not power.on_battery:
        target = ac_profile
    elif power.low_battery and low_battery_protection:
        target = "power-saver"
    else:
        target = battery_profile

    if performance_apps_active and power.on_battery:
        target = performance_apps_battery_profile
    elif performance_apps_active and not power.on_battery:
        target = performance_apps_ac_profile

    if weak_adapter_mode:
        target = "power-saver"

    return target


def brightness_percent(power: PowerState, *, ac: int, battery: int, low_battery: int) -> int:
    if power.on_battery and power.low_battery:
        return low_battery
    return battery if power.on_battery else ac
