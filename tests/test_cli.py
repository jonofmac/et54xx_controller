"""Menu, settings, and status-page tests for the command-line discharge. No load required."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capacity import Sample
from et54xx_cli import (
    RESOURCE_PROMPT,
    CliApp,
    Connection,
    DischargeForm,
    RunState,
    StatusView,
    apply_menu_key,
    apply_overrides,
    build_config,
    build_parser,
    choose_limits,
    classify_command,
    csv_path_problem,
    entry_problem,
    format_elapsed,
    format_sample_line,
    form_from_settings,
    overrides_used,
    parse_bounded,
    parse_mode,
    parse_optional_limit,
    resource_problem,
    render_plot,
    render_setup,
    render_status,
    render_voltage_bar,
    voltage_fraction,
)


def make_sample(**overrides) -> Sample:
    values = dict(
        timestamp=datetime(2026, 11, 23, 10, 0, 0),
        elapsed_s=30.0,
        voltage_v=48.213,
        loaded_voltage_v=47.5,
        open_circuit_voltage_v=None,
        current_a=2.001,
        power_w=96.4,
        capacity_ah=1.234,
        energy_wh=59.4,
        load_on=True,
        source="loaded",
    )
    values.update(overrides)
    return Sample(**values)


def make_view(samples, **overrides) -> StatusView:
    values = dict(
        mode_text="CC 2.000 A",
        identity="ET54 1",
        ranges="high voltage, low current",
        phase="running",
        elapsed_s=30.0,
        samples=samples,
        cutoff_voltage=40.0,
        cutoff_current=0.2,
        cutoff_ah=10.0,
        start_voltage=54.2,
        ocv=False,
        status="CC 2.000 A. High voltage range, low current range.",
        error=False,
        csv_note="Logging to pack.csv",
    )
    values.update(overrides)
    return StatusView(**values)


class FakeLoad:
    def __init__(self):
        self.on_flag = False
        self.voltage = 50.0

    def setup_mode(self, _config) -> None:
        self.on_flag = False

    def on(self) -> None:
        self.on_flag = True

    def off(self) -> None:
        self.on_flag = False

    def read_voltage(self) -> float:
        return self.voltage

    def read_current(self) -> float:
        return 2.0

    def read_ranges(self) -> tuple[str, str]:
        return "high", "low"

    def close(self) -> None:
        return None


class FormTests(unittest.TestCase):
    def test_defaults_match_the_window_and_validate(self):
        form = form_from_settings({})
        config, error = build_config(form)
        self.assertIsNone(error)
        self.assertEqual(config.mode, "CC")
        self.assertEqual(config.current_a, 2.0)
        self.assertEqual(config.sample_interval_s, 5.0)
        self.assertEqual(config.cutoff_voltage_v, 5.0)
        self.assertIsNone(config.cutoff_current_a)
        self.assertTrue(config.csv_enabled)
        self.assertEqual(config.csv_path, "capacity_log.csv")
        self.assertFalse(config.ocv_enabled)
        self.assertEqual(config.voltage_range, "high")
        self.assertEqual(config.current_range, "low")
        self.assertFalse(config.sync_voltage_gate)

    def test_window_settings_round_trip(self):
        saved = {
            "resource": "ASRL/dev/ttyUSB0::INSTR",
            "mode": "CR",
            "current_a": "2.0",
            "voltage_set_v": "60.0",
            "resistance_ohm": "12",
            "power_w": "100",
            "cutoff_voltage_on": True,
            "cutoff_voltage_v": "42",
            "cutoff_current_on": False,
            "cutoff_current_a": "0.20",
            "cutoff_time_on": True,
            "cutoff_time_s": "3600",
            "cutoff_ah_on": False,
            "cutoff_ah": "10",
            "cutoff_wh_on": False,
            "cutoff_wh": "500",
            "sync_voltage_gate": True,
            "csv_enabled": False,
        }
        form = form_from_settings(saved)
        self.assertEqual(form.mode, "CR")
        self.assertEqual(form.resistance_ohm, "12")
        self.assertTrue(form.cutoff_time_on)
        self.assertFalse(form.csv_enabled)
        self.assertTrue(form.sync_voltage_gate)
        self.assertEqual(form.sample_interval_s, "5")
        updates = form.to_updates()
        self.assertIsInstance(updates["current_a"], str)
        self.assertIs(updates["csv_enabled"], False)
        self.assertIs(updates["sync_voltage_gate"], True)
        self.assertEqual(updates["mode"], "CR")
        again = form_from_settings(updates)
        self.assertEqual(again.mode, "CR")
        self.assertEqual(again.cutoff_voltage_v, "42")
        self.assertEqual(again.cutoff_time_s, "3600")

    def test_prompt_resource_is_cleared(self):
        form = form_from_settings({"resource": RESOURCE_PROMPT, "mode": "cc+cv"})
        self.assertEqual(form.resource, "")
        self.assertEqual(form.mode, "CCCV")

    def test_disabled_cutoffs_are_rejected(self):
        form = form_from_settings({})
        form.cutoff_voltage_on = False
        config, error = build_config(form)
        self.assertIsNone(config)
        self.assertIn("cutoff", error.lower())

    def test_a_non_numeric_setpoint_is_rejected(self):
        form = form_from_settings({})
        form.current_a = "fast"
        _config, error = build_config(form)
        self.assertIn("number", error)

    def test_settings_file_keeps_the_types_the_window_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            app = CliApp(path)
            app.form.mode = "CV"
            app.form.csv_enabled = False
            app.form.voltage_set_v = "48"
            app.save()
            loaded = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(loaded["mode"], "CV")
            self.assertIs(loaded["csv_enabled"], False)
            self.assertIsInstance(loaded["voltage_set_v"], str)
            again = CliApp(path)
            self.assertEqual(again.form.mode, "CV")
            self.assertFalse(again.form.csv_enabled)
            self.assertEqual(again.form.voltage_set_v, "48")


class ParseTests(unittest.TestCase):
    def test_mode_words(self):
        self.assertEqual(parse_mode("cc+cv"), "CCCV")
        self.assertEqual(parse_mode("Constant power"), "CP")
        self.assertIsNone(parse_mode("pulse"))

    def test_cutoff_edits(self):
        self.assertEqual(parse_optional_limit("", True, "5.0", "Voltage cutoff"), (True, "5.0", None))
        self.assertEqual(parse_optional_limit("off", True, "5.0", "Voltage cutoff"), (False, "5.0", None))
        self.assertEqual(parse_optional_limit("40", False, "5.0", "Voltage cutoff"), (True, "40", None))
        _on, _text, error = parse_optional_limit("0", True, "5", "Voltage cutoff")
        self.assertIsNotNone(error)
        _on, _text, error = parse_optional_limit("abc", True, "5", "Voltage cutoff")
        self.assertIn("number", error)

    def test_bounds(self):
        value, error = parse_bounded("80", "Power", None, 200)
        self.assertEqual(value, 80.0)
        self.assertIsNone(error)
        _value, error = parse_bounded("250", "Power", None, 200)
        self.assertIn("200", error)
        _value, error = parse_bounded("1", "Sample interval", 2, None)
        self.assertIn("2", error)
        _value, error = parse_bounded("-4", "Current")
        self.assertIn("greater", error)
        _value, error = parse_bounded("0.01", "Resistance", 0.05, 4500)
        self.assertIn("between", error)

    def test_commands(self):
        self.assertEqual(classify_command("14"), ("edit", 14))
        self.assertEqual(classify_command(" S "), ("start", None))
        self.assertEqual(classify_command(""), ("unknown", None))
        self.assertEqual(classify_command("ports"), ("ports", None))

    def test_menu_keys_build_a_command(self):
        buffer = ""
        for key in "14":
            buffer, finished = apply_menu_key(buffer, key)
            self.assertIsNone(finished)
        _buffer, finished = apply_menu_key(buffer, "\r")
        self.assertEqual(finished, "14")
        self.assertEqual(apply_menu_key("14", "\x7f"), ("1", None))
        self.assertEqual(apply_menu_key("", "\x7f"), ("", None))
        _buffer, finished = apply_menu_key("11", "\x1b")
        self.assertIsNone(finished)
        self.assertNotIn("\x1b", _buffer)

    def test_control_characters_are_rejected(self):
        self.assertIsNone(entry_problem("pack.csv"))
        self.assertIsNotNone(entry_problem("\x1bpack.csv"))
        self.assertIsNotNone(entry_problem("pack\x1b.csv"))
        self.assertIsNone(csv_path_problem("logs/pack.csv"))
        self.assertIn("folder", csv_path_problem("logs/"))
        self.assertIn("Control", csv_path_problem("\x1b"))
        self.assertIsNotNone(resource_problem("ASRL /dev/ttyUSB0::INSTR"))
        form = form_from_settings({"csv_path": "\x1bpack.csv", "resource": "bad\x1bpath"})
        self.assertEqual(form.csv_path, "capacity_log.csv")
        self.assertEqual(form.resource, "")
        args = build_parser().parse_args(["--csv", "\x1bpack.csv"])
        self.assertIn("Control", apply_overrides(form_from_settings({}), args))

    def test_flags_override_saved_values(self):
        args = build_parser().parse_args(
            ["--mode", "CP", "--power", "50", "--no-csv", "--cutoff-voltage", "40", "--no-cutoff-current"]
        )
        form = form_from_settings({"mode": "CC", "cutoff_current_on": True, "cutoff_current_a": "0.2"})
        self.assertIsNone(apply_overrides(form, args))
        self.assertEqual(form.mode, "CP")
        self.assertEqual(form.power_w, "50")
        self.assertFalse(form.csv_enabled)
        self.assertTrue(form.cutoff_voltage_on)
        self.assertEqual(form.cutoff_voltage_v, "40")
        self.assertFalse(form.cutoff_current_on)
        self.assertTrue(overrides_used(args))
        config, error = build_config(form)
        self.assertIsNone(error)
        self.assertEqual(config.mode, "CP")
        self.assertEqual(config.power_w, 50.0)
        self.assertIsNone(config.cutoff_current_a)
        self.assertIsNone(config.ovp_v)
        self.assertIsNone(config.ocp_a)

    def test_a_run_does_not_program_ranges_unless_they_were_changed(self):
        kept, error = build_config(form_from_settings({}), program_ranges=False)
        self.assertIsNone(error)
        self.assertFalse(kept.program_ranges)
        self.assertIsNone(kept.ovp_v)
        self.assertIsNone(kept.ocp_a)
        changed, error = build_config(form_from_settings({}), program_ranges=True)
        self.assertIsNone(error)
        self.assertTrue(changed.program_ranges)

    def test_conflicting_flags_are_rejected_before_a_change(self):
        form = form_from_settings({})
        args = build_parser().parse_args(["--csv", "pack.csv", "--no-csv"])
        error = apply_overrides(form, args)
        self.assertIn("--csv", error)
        self.assertTrue(form.csv_enabled)
        self.assertEqual(form.csv_path, "capacity_log.csv")

    def test_elapsed_clock(self):
        self.assertEqual(format_elapsed(3661), "1:01:01")
        self.assertEqual(format_elapsed(-5), "0:00:00")


class ScreenTests(unittest.TestCase):
    def test_rising_series_climbs_the_chart(self):
        rows = render_plot(
            [(0, 0.0), (1, 0.0), (2, 10.0), (3, 10.0)],
            width=8,
            height=4,
            y_min=0,
            y_max=10,
            t_min=0,
            t_max=3,
        )
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(len(row) == 8 for row in rows))
        self.assertEqual(rows[0][0], " ")
        self.assertIn(rows[0][-1], "▀▄█")
        self.assertIn(rows[-1][0], "▀▄█")
        self.assertEqual(rows[-1][-1], " ")
        self.assertTrue(any(ch in "▀▄█" for ch in "".join(rows)))

    def test_a_guide_is_drawn_when_it_is_on_scale(self):
        plain = render_plot([(0, 1.0), (1, 1.0)], 8, 4, 0, 2, t_min=0, t_max=1)
        guided = render_plot([(0, 1.0), (1, 1.0)], 8, 4, 0, 2, t_min=0, t_max=1, guide=0)
        self.assertNotEqual(plain, guided)

    def test_a_distant_cutoff_stays_off_the_chart_scale(self):
        _lo, _hi, visible = choose_limits([50.0, 54.0], guide=10.0)
        self.assertFalse(visible)
        lo, hi, visible = choose_limits([50.0, 54.0], guide=48.0)
        self.assertTrue(visible)
        self.assertLess(lo, 48.0)
        self.assertGreater(hi, 54.0)
        bottom, _top, _visible = choose_limits([1.0, 2.0], floor=0.0)
        self.assertEqual(bottom, 0.0)

    def test_bar_readout_follows_the_voltage(self):
        self.assertEqual(voltage_fraction(54, 54, 5), 1)
        self.assertEqual(voltage_fraction(5, 54, 5), 0)
        self.assertAlmostEqual(voltage_fraction(29.5, 54, 5), 0.5)
        self.assertIsNone(voltage_fraction(7, 5, 10))
        low = render_voltage_bar(54, 5, 5, 40)
        high = render_voltage_bar(54, 5, 54, 40)
        self.assertLess(low[1].index("5.000 V"), 8)
        self.assertGreater(high[1].index("54.000 V"), 20)
        self.assertIn("cutoff 5.000 V", low[0])
        self.assertIn("start 54.000 V", high[0])
        flat = render_voltage_bar(5, 10, 7, 40)
        self.assertEqual(flat[1], "7.000 V")

    def test_status_page_shows_the_readings_the_bar_and_the_charts(self):
        samples = [
            make_sample(elapsed_s=0, voltage_v=54.2, current_a=0, capacity_ah=0, energy_wh=0, load_on=False, source="resting"),
            make_sample(),
        ]
        page = render_status(make_view(samples), 80, 40)
        self.assertEqual(len(page.splitlines()), 40)
        self.assertIn("48.213", page)
        self.assertIn("2.001", page)
        self.assertIn("1.234", page)
        self.assertIn("59.4", page)
        self.assertIn("0:00:30", page)
        self.assertIn("cutoff 40.000 V", page)
        self.assertIn("start 54.200 V", page)
        self.assertIn("Voltage (V)", page)
        self.assertIn("Current (A)", page)
        self.assertIn("Discharge (Ah)", page)
        self.assertIn("0 Ah", page)
        self.assertIn("cutoff 10.000 Ah", page)
        self.assertTrue(any(ch in "▀▄█" for ch in page))
        self.assertIn("Logging to pack.csv", page)
        self.assertIn("Press q to stop", page)

    def test_each_enabled_cutoff_gets_a_bar(self):
        page = render_status(
            make_view(
                [make_sample()],
                start_current=2.0,
                cutoff_time_s=3600,
                cutoff_energy_wh=500,
                elapsed_s=30,
            ),
            80,
            48,
        )
        self.assertIn("cutoff 0.200 A", page)
        self.assertIn("start 2.000 A", page)
        self.assertIn("0 Ah", page)
        self.assertIn("cutoff 10.000 Ah", page)
        self.assertIn("0 Wh", page)
        self.assertIn("cutoff 500.0 Wh", page)
        self.assertIn("0:00:00", page)
        self.assertIn("cutoff 1:00:00", page)

    def test_a_short_terminal_keeps_the_bar_and_drops_the_charts(self):
        page = render_status(make_view([make_sample()]), 80, 16)
        self.assertEqual(len(page.splitlines()), 16)
        self.assertIn("48.213", page)
        self.assertIn("cutoff 40.000 V", page)
        self.assertNotIn("Voltage (V)", page)
        self.assertNotIn("Discharge (Ah)", page)

    def test_the_page_waits_before_the_first_sample(self):
        page = render_status(make_view([], elapsed_s=None, start_voltage=None), 80, 24)
        self.assertIn("Waiting for the first sample.", page)
        self.assertIn("—", page)

    def test_open_circuit_chart_names_the_loaded_trace(self):
        sample = make_sample(source="open_circuit", open_circuit_voltage_v=48.213)
        page = render_status(make_view([sample], ocv=True), 100, 40)
        self.assertIn("shaded line is loaded", page)
        self.assertIn("open circuit", page)

    def test_sample_line_matches_the_readings(self):
        line = format_sample_line(make_sample())
        self.assertIn("48.213 V", line)
        self.assertIn("2.001 A", line)
        self.assertIn("1.234 Ah", line)
        self.assertIn("59.4 Wh", line)
        self.assertTrue(line.startswith("0:00:30"))

    def test_setup_screen_marks_setpoints_the_mode_does_not_use(self):
        text = render_setup(DischargeForm(), Connection(), "")
        self.assertIn("Constant current", text)
        self.assertIn("5.0 V", text)
        self.assertNotIn("not used", text)
        self.assertIn("disconnected", text)
        positions = [line.index("Not In Use") for line in text.splitlines() if "Not In Use" in line]
        self.assertGreater(len(positions), 1)
        self.assertEqual(len(set(positions)), 1)


class MenuVoltageTests(unittest.TestCase):
    def test_the_menu_follows_a_new_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            app = CliApp(Path(directory) / "settings.json")
            load = FakeLoad()
            app.connection.load = load
            app.connection.identity = "ET54 test"
            app.connection.resting_v = 50.0
            self.assertFalse(app._refresh_menu_voltage())
            load.voltage = 49.5
            self.assertTrue(app._refresh_menu_voltage())
            self.assertEqual(app.connection.resting_v, 49.5)
            text = render_setup(app.form, app.connection, app.notice)
            self.assertIn("49.500 V", text)

    def test_a_failed_reading_drops_the_link(self):
        with tempfile.TemporaryDirectory() as directory:
            app = CliApp(Path(directory) / "settings.json")

            class DeadLoad:
                def read_voltage(self) -> float:
                    raise RuntimeError("timeout")

                def close(self) -> None:
                    return None

            app.connection.load = DeadLoad()
            app.connection.resting_v = 50.0
            self.assertTrue(app._refresh_menu_voltage())
            self.assertIsNone(app.connection.load)
            self.assertIn("Connection lost", app.notice)


class RunWiringTests(unittest.TestCase):
    def test_an_open_connection_logs_and_feeds_the_status_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = CliApp(root / "settings.json")
            app.form.resource = "ASRL/dev/ttyUSB0::INSTR"
            app.form.csv_path = str(root / "pack.csv")
            app.connection.load = FakeLoad()
            app.connection.resource = app.form.resource
            app.connection.identity = "ET54 test"
            config, error = build_config(app.form)
            self.assertIsNone(error)
            state = RunState()
            stop = threading.Event()
            stop.set()
            app._worker(state, stop, config, app.form.resource)
            snap = state.snapshot()
            self.assertIsNone(snap["failed"], snap["status"])
            self.assertEqual(snap["start_voltage"], 50.0)
            self.assertEqual(snap["samples"][0].source, "resting")
            logged = (root / "pack.csv").read_text(encoding="utf-8")
            self.assertIn("voltage_v", logged)
            self.assertIn("50.0000", logged)
            page = render_status(app._view(state, config, stopping=False), 80, 40)
            self.assertIn("50.000", page)
            self.assertIn("Voltage (V)", page)
            self.assertIn("STOPPED", page)


if __name__ == "__main__":
    unittest.main()
