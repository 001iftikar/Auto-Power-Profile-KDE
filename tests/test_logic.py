import unittest

from logic import (
    PowerState,
    brightness_percent,
    choose_profile,
    configured_apps,
    normalize_app_id,
    normalize_window_snapshot,
    resolve_power_state,
)


class PowerStateTests(unittest.TestCase):
    def test_online_line_power_is_primary_over_battery_discharge_state(self):
        state = resolve_power_state(True, 2, 4, 12.0)
        self.assertFalse(state.on_battery)
        self.assertTrue(state.on_ac)
        self.assertFalse(state.low_battery)
        self.assertTrue(state.weak_adapter_suspected)

    def test_offline_line_power_means_battery(self):
        state = resolve_power_state(False, 1, 3, 0.0)
        self.assertTrue(state.on_battery)
        self.assertTrue(state.low_battery)

    def test_battery_state_is_fallback_when_line_power_unknown(self):
        state = resolve_power_state(None, 6, 1, 0.0)
        self.assertTrue(state.on_battery)
        self.assertFalse(state.low_battery)

    def test_low_warning_levels(self):
        for warning in (3, 4, 5):
            with self.subTest(warning=warning):
                self.assertTrue(resolve_power_state(False, 2, warning, 0).low_battery)


class ApplicationIdTests(unittest.TestCase):
    def test_normalizes_desktop_file_name_and_path(self):
        self.assertEqual(normalize_app_id("/usr/share/applications/code.desktop"), "code")
        self.assertEqual(normalize_app_id("org.kde.kate"), "org.kde.kate")

    def test_parses_comma_and_semicolon_lists(self):
        self.assertEqual(
            configured_apps("code.desktop;org.kde.kate, steam.desktop"),
            {"code", "org.kde.kate", "steam"},
        )

    def test_normalizes_and_filters_kwin_window_snapshot(self):
        snapshot = normalize_window_snapshot(
            '{"window-a":"dev.zed.Zed",'
            '"window-b":"/usr/share/applications/dev.zed.Zed.desktop",'
            '"window-c":"org.kde.dolphin"}',
            {"dev.zed.zed"},
        )
        self.assertEqual(
            snapshot,
            {"window-a": "dev.zed.zed", "window-b": "dev.zed.zed"},
        )

    def test_rejects_non_object_window_snapshot(self):
        with self.assertRaises(ValueError):
            normalize_window_snapshot('["dev.zed.Zed"]', {"dev.zed.zed"})


class ProfileSelectionTests(unittest.TestCase):
    def setUp(self):
        self.base = dict(
            ac_profile="performance",
            battery_profile="balanced",
            performance_apps_ac_profile="performance",
            performance_apps_battery_profile="balanced",
            low_battery_protection=True,
        )

    def test_ac_default(self):
        self.assertEqual(
            choose_profile(PowerState(False, True, False, False),
                           performance_apps_active=False, weak_adapter_mode=False,
                           **self.base),
            "performance",
        )

    def test_battery_default(self):
        self.assertEqual(
            choose_profile(PowerState(True, False, False, False),
                           performance_apps_active=False, weak_adapter_mode=False,
                           **self.base),
            "balanced",
        )

    def test_low_battery_uses_power_saver_without_perf_app(self):
        self.assertEqual(
            choose_profile(PowerState(True, False, True, False),
                           performance_apps_active=False, weak_adapter_mode=False,
                           **self.base),
            "power-saver",
        )

    def test_perf_app_override_matches_original_order_even_on_low_battery(self):
        self.assertEqual(
            choose_profile(PowerState(True, False, True, False),
                           performance_apps_active=True, weak_adapter_mode=False,
                           **self.base),
            "balanced",
        )

    def test_weak_adapter_has_highest_priority(self):
        self.assertEqual(
            choose_profile(PowerState(False, True, False, True),
                           performance_apps_active=True, weak_adapter_mode=True,
                           **self.base),
            "power-saver",
        )


class BrightnessTests(unittest.TestCase):
    def test_selected_user_brightness(self):
        self.assertEqual(brightness_percent(PowerState(False, True, False, False), ac=100, battery=60, low_battery=30), 100)
        self.assertEqual(brightness_percent(PowerState(True, False, False, False), ac=100, battery=60, low_battery=30), 60)
        self.assertEqual(brightness_percent(PowerState(True, False, True, False), ac=100, battery=60, low_battery=30), 30)


if __name__ == "__main__":
    unittest.main()
