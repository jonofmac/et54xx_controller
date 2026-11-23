"""State-machine tests for the pack capacity discharge. No load required."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from capacity import (
    CSV_HEADER,
    _scpi_float,
    CapacityTest,
    TestConfig,
    list_visa_resources,
    voltage_gate_commands,
    voltage_level_commands,
    voltage_gate_levels,
    load_samples,
    parse_range,
    load_saved_resource,
    save_saved_resource,
    validate_config,
)


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeLoad:
    def __init__(self, off_voltages, on_voltages, current=2.0):
        self.off_voltages = list(off_voltages)
        self.on_voltages = list(on_voltages)
        self.current = current
        self.currents = []
        self.on_flag = False
        self.commands = []
        self.mode = None
        self.vrange = None
        self.crange = None

    def setup_mode(self, config) -> None:
        self.mode = config.mode
        self.vrange = config.voltage_range
        self.crange = config.current_range
        self.commands.append(("setup", config.mode))
        self.on_flag = False

    def on(self) -> None:
        self.on_flag = True
        self.commands.append("on")

    def off(self) -> None:
        self.on_flag = False
        self.commands.append("off")

    def read_voltage(self) -> float:
        queue = self.on_voltages if self.on_flag else self.off_voltages
        voltage = queue.pop(0)
        self.commands.append(("V", voltage, self.on_flag))
        return voltage

    def read_current(self) -> float:
        if not self.on_flag:
            return 0.0
        if self.currents:
            return self.currents.pop(0)
        return self.current


def config(path: Path, **overrides) -> TestConfig:
    values = dict(
        mode="CC",
        current_a=2.0,
        voltage_set_v=None,
        resistance_ohm=None,
        power_w=None,
        sample_interval_s=10.0,
        ocv_enabled=False,
        ocv_wait_s=1.0,
        csv_path=str(path),
        csv_enabled=True,
        voltage_range="high",
        current_range="low",
        cutoff_voltage_v=60.0,
        cutoff_current_a=None,
        cutoff_time_s=None,
        cutoff_discharge_ah=None,
        cutoff_energy_wh=None,
        sync_voltage_gate=True,
        ovp_v=150.0,
        ocp_a=3.0,
    )
    values.update(overrides)
    return TestConfig(**values)


class CapacityTestCase(unittest.TestCase):
    def test_cutoff_integrates_on_time_and_writes_csv(self):
        clock = Clock()
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[70.0], on_voltages=[68.0, 64.0, 59.5])
            result = CapacityTest(
                load,
                config(path),
                threading.Event(),
                seen.append,
                now=clock.now,
                sleep=clock.sleep,
            ).run()
            with path.open(newline="") as handle:
                rows = list(csv.reader(handle))

        self.assertEqual(result.reason, "cutoff")
        self.assertEqual([sample.source for sample in seen], ["resting", "loaded", "loaded", "loaded"])
        self.assertEqual([sample.elapsed_s for sample in seen], [0.0, 10.0, 20.0, 30.0])
        self.assertAlmostEqual(seen[-1].capacity_ah, 2.0 * 30.0 / 3600.0)
        self.assertAlmostEqual(seen[-1].energy_wh, (68 * 2 * 10 + 64 * 2 * 10 + 59.5 * 2 * 10) / 3600.0)
        self.assertFalse(load.on_flag)
        self.assertEqual(load.vrange, "high")
        self.assertEqual(load.crange, "low")
        self.assertEqual(tuple(rows[0]), CSV_HEADER)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[-1][-1], "loaded")

    def test_open_circuit_cutoff_excludes_settle_time(self):
        clock = Clock()
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[70.0, 66.0, 60.0], on_voltages=[67.0, 63.0])
            result = CapacityTest(
                load,
                config(path, ocv_enabled=True, ocv_wait_s=1.0),
                threading.Event(),
                seen.append,
                now=clock.now,
                sleep=clock.sleep,
            ).run()

        self.assertEqual(result.reason, "cutoff")
        self.assertEqual([sample.source for sample in seen], ["resting", "open_circuit", "open_circuit"])
        self.assertEqual([sample.elapsed_s for sample in seen], [0.0, 10.0, 20.0])
        self.assertEqual(seen[1].open_circuit_voltage_v, 66.0)
        self.assertEqual(seen[1].loaded_voltage_v, 67.0)
        self.assertEqual(seen[-1].voltage_v, 60.0)
        self.assertAlmostEqual(seen[-1].capacity_ah, 2.0 * 9.0 * 2 / 3600.0)
        self.assertFalse(load.on_flag)

    def test_stop_after_a_sample_does_not_start_another_cycle(self):
        clock = Clock()
        stop = threading.Event()
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[72.0], on_voltages=[71.0, 70.0])

            def on_sample(sample):
                seen.append(sample)
                if sample.source == "loaded":
                    stop.set()

            result = CapacityTest(
                load,
                config(path, cutoff_voltage_v=50.0),
                stop,
                on_sample,
                now=clock.now,
                sleep=clock.sleep,
            ).run()

        self.assertEqual(result.reason, "stopped")
        self.assertEqual([sample.source for sample in seen], ["resting", "loaded"])
        self.assertAlmostEqual(seen[-1].capacity_ah, 2.0 * 10.0 / 3600.0)
        self.assertEqual(load.commands.count("on"), 1)
        self.assertFalse(load.on_flag)

    def test_stop_during_discharge_turns_off_without_another_read(self):
        clock = Clock()
        stop = threading.Event()
        seen = []

        def sleep(seconds):
            clock.sleep(seconds)
            if clock.t >= 3:
                stop.set()

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[70.0], on_voltages=[68.0])
            result = CapacityTest(
                load,
                config(path, cutoff_voltage_v=40.0),
                stop,
                seen.append,
                now=clock.now,
                sleep=sleep,
            ).run()

        self.assertEqual(result.reason, "stopped")
        self.assertEqual(load.on_voltages, [68.0])
        self.assertEqual(seen[-1].source, "loaded")
        self.assertAlmostEqual(seen[-1].elapsed_s, 3.0, delta=0.051)
        self.assertAlmostEqual(seen[-1].capacity_ah, 2.0 * seen[-1].elapsed_s / 3600.0)
        self.assertFalse(load.on_flag)

    def test_stop_during_settle_keeps_the_on_interval(self):
        clock = Clock()
        stop = threading.Event()
        seen = []

        def sleep(seconds):
            clock.sleep(seconds)
            if clock.t >= 9:
                stop.set()

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[70.0, 69.0], on_voltages=[68.0])
            result = CapacityTest(
                load,
                config(path, ocv_enabled=True, ocv_wait_s=2.0, cutoff_voltage_v=40.0),
                stop,
                seen.append,
                now=clock.now,
                sleep=sleep,
            ).run()

        self.assertEqual(result.reason, "stopped")
        self.assertEqual(seen[-1].source, "loaded")
        self.assertAlmostEqual(seen[-1].capacity_ah, 2.0 * 8.0 / 3600.0)
        self.assertFalse(load.on_flag)

    def test_power_limit_never_turns_the_load_on(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[80.0], on_voltages=[79.0])
            result = CapacityTest(
                load,
                config(path, current_a=3.0, cutoff_voltage_v=50.0, current_range="high"),
                threading.Event(),
                lambda _sample: None,
                now=clock.now,
                sleep=clock.sleep,
            ).run()
        self.assertEqual(result.reason, "power_limit")
        self.assertNotIn("on", load.commands)
        self.assertFalse(path.exists())
        self.assertFalse(load.on_flag)

    def test_high_current_range_when_power_allows(self):
        clock = Clock()
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[10.0], on_voltages=[9.0, 4.0], current=5.0)
            result = CapacityTest(
                load,
                config(path, current_a=5.0, cutoff_voltage_v=5.0, current_range="high"),
                threading.Event(),
                seen.append,
                now=clock.now,
                sleep=clock.sleep,
            ).run()
        self.assertEqual(result.reason, "cutoff")
        self.assertEqual(load.vrange, "high")
        self.assertEqual(load.crange, "high")
        self.assertAlmostEqual(seen[-1].capacity_ah, 5.0 * 20.0 / 3600.0)

    def test_sample_interval_must_be_at_least_two_seconds(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            message = validate_config(config(path, sample_interval_s=1.0))
        self.assertIn("at least 2 s", message)

    def test_validate_open_circuit_window(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            message = validate_config(
                config(path, ocv_enabled=True, ocv_wait_s=2.0, sample_interval_s=2.0)
            )
        self.assertIsNotNone(message)
        self.assertIn("2.50", message)

    def test_low_voltage_range_leaves_a_pack_off(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[72.0], on_voltages=[70.0])
            result = CapacityTest(
                load,
                config(path, voltage_range="low", cutoff_voltage_v=12.0, ovp_v=20.0),
                threading.Event(),
                lambda _sample: None,
                now=clock.now,
                sleep=clock.sleep,
            ).run()
        self.assertEqual(result.reason, "voltage_range")
        self.assertNotIn("on", load.commands)
        self.assertEqual(load.vrange, "low")
        self.assertFalse(load.on_flag)

    def test_selected_high_current_range_is_used_below_3_amps(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[12.0], on_voltages=[11.0, 9.0])
            result = CapacityTest(
                load,
                config(path, current_a=1.5, cutoff_voltage_v=10.0, voltage_range="low", current_range="high", ovp_v=20.0),
                threading.Event(),
                lambda _sample: None,
                now=clock.now,
                sleep=clock.sleep,
            ).run()
        self.assertEqual(result.reason, "cutoff")
        self.assertEqual((load.vrange, load.crange), ("low", "high"))

    def test_validate_range_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            current_message = validate_config(config(path, current_a=3.0, current_range="low"))
            voltage_message = validate_config(config(path, cutoff_voltage_v=60.0, voltage_range="low", ovp_v=20.0))
        self.assertIn("2.9", current_message)
        self.assertIn("20", voltage_message)

    def test_discharge_ah_cutoff(self):
        clock = Clock()
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[70.0], on_voltages=[68.0, 64.0, 60.0])
            result = CapacityTest(
                load,
                config(
                    path,
                    cutoff_voltage_v=None,
                    cutoff_discharge_ah=2.0 * 15.0 / 3600.0,
                ),
                threading.Event(),
                seen.append,
                now=clock.now,
                sleep=clock.sleep,
            ).run()
        self.assertEqual(result.reason, "cutoff")
        self.assertIn("discharge Ah", result.message)
        self.assertEqual([sample.source for sample in seen], ["resting", "loaded", "loaded"])
        self.assertAlmostEqual(seen[-1].capacity_ah, 2.0 * 20.0 / 3600.0)

    def test_time_cutoff_shortens_the_sample(self):
        clock = Clock()
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[70.0], on_voltages=[68.0])
            result = CapacityTest(
                load,
                config(path, cutoff_voltage_v=None, cutoff_time_s=4.0),
                threading.Event(),
                seen.append,
                now=clock.now,
                sleep=clock.sleep,
            ).run()
        self.assertEqual(result.reason, "cutoff")
        self.assertIn("time", result.message)
        self.assertAlmostEqual(seen[-1].elapsed_s, 4.0, delta=0.051)
        self.assertFalse(load.on_flag)

    def test_energy_cutoff(self):
        clock = Clock()
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[70.0], on_voltages=[50.0])
            result = CapacityTest(
                load,
                config(path, cutoff_voltage_v=None, cutoff_energy_wh=50.0 * 2.0 * 10.0 / 3600.0),
                threading.Event(),
                seen.append,
                now=clock.now,
                sleep=clock.sleep,
            ).run()
        self.assertEqual(result.reason, "cutoff")
        self.assertIn("energy Wh", result.message)
        self.assertEqual(len(seen), 2)

    def test_current_cutoff_uses_loaded_current(self):
        clock = Clock()
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[70.0], on_voltages=[68.0, 66.0])
            load.currents = [2.0, 0.3]
            result = CapacityTest(
                load,
                config(path, cutoff_voltage_v=None, cutoff_current_a=0.5),
                threading.Event(),
                seen.append,
                now=clock.now,
                sleep=clock.sleep,
            ).run()
        self.assertEqual(result.reason, "cutoff")
        self.assertIn("current", result.message)
        self.assertEqual([sample.source for sample in seen], ["resting", "loaded", "loaded"])
        self.assertEqual(seen[-1].current_a, 0.3)

    def test_cv_mode_is_programmed(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[72.0], on_voltages=[60.0])
            result = CapacityTest(
                load,
                config(
                    path,
                    mode="CV",
                    current_a=None,
                    voltage_set_v=60.0,
                    cutoff_voltage_v=None,
                    cutoff_time_s=5.0,
                ),
                threading.Event(),
                lambda _sample: None,
                now=clock.now,
                sleep=clock.sleep,
            ).run()
        self.assertEqual(load.mode, "CV")
        self.assertEqual(result.reason, "cutoff")
        self.assertIn("time", result.message)

    def test_constant_power_above_rating_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            message = validate_config(config(path, mode="CP", current_a=None, power_w=250.0))
        self.assertIn("200", message)

    def test_constant_resistance_over_power_stays_off(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[70.0], on_voltages=[69.0])
            result = CapacityTest(
                load,
                config(
                    path,
                    mode="CR",
                    current_a=None,
                    resistance_ohm=10.0,
                    cutoff_voltage_v=50.0,
                ),
                threading.Event(),
                lambda _sample: None,
                now=clock.now,
                sleep=clock.sleep,
            ).run()
        self.assertEqual(result.reason, "power_limit")
        self.assertEqual(load.mode, "CR")
        self.assertNotIn("on", load.commands)
        self.assertFalse(load.on_flag)

    def test_load_samples_round_trip(self):
        clock = Clock()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            load = FakeLoad(off_voltages=[70.0, 66.0, 60.0], on_voltages=[67.0, 63.0])
            CapacityTest(
                load,
                config(path, ocv_enabled=True, ocv_wait_s=1.0),
                threading.Event(),
                lambda _sample: None,
                now=clock.now,
                sleep=clock.sleep,
            ).run()
            samples = load_samples(path)

        self.assertEqual([sample.source for sample in samples], ["resting", "open_circuit", "open_circuit"])
        self.assertAlmostEqual(samples[1].open_circuit_voltage_v, 66.0)
        self.assertAlmostEqual(samples[1].loaded_voltage_v, 67.0)
        self.assertAlmostEqual(samples[-1].capacity_ah, 2.0 * 9.0 * 2 / 3600.0)
        self.assertFalse(samples[1].load_on)

    def test_load_samples_rejects_a_foreign_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "other.csv"
            path.write_text("time,volts\n1,3.7\n", encoding="utf-8")
            with self.assertRaises(ValueError) as raised:
                load_samples(path)
        self.assertIn("missing columns", str(raised.exception))

    def test_load_samples_rejects_a_bad_row(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.csv"
            path.write_text(
                ",".join(CSV_HEADER) + "\n"
                "2026-01-01T00:00:00.000,0,3.8,3.8,,1,3.8,0,0,0,resting\n"
                "not-a-time,10,3.7,3.7,,1,3.7,0.01,0.01,1,loaded\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as raised:
                load_samples(path)
        self.assertIn("row 3", str(raised.exception))

    def test_list_visa_resources_puts_serial_ports_first(self):
        manager = mock.Mock()
        manager.list_resources.return_value = (
            "TCPIP0::192.168.0.5::INSTR",
            "ASRL/dev/ttyUSB0::INSTR",
            "ASRL/dev/ttyUSB0::INSTR",
            "ASRL3::INSTR",
        )
        pyvisa = types.ModuleType("pyvisa")
        pyvisa.ResourceManager = mock.Mock(return_value=manager)
        with mock.patch.dict(sys.modules, {"pyvisa": pyvisa}):
            names = list_visa_resources()
        self.assertEqual(names, ["ASRL/dev/ttyUSB0::INSTR", "ASRL3::INSTR"])
        pyvisa.ResourceManager.assert_called_once_with("@py")
        manager.list_resources.assert_called_once_with("ASRL?*::INSTR")
        manager.close.assert_called_once()

    def test_list_visa_resources_uses_the_default_backend_when_pyvisa_py_is_missing(self):
        manager = mock.Mock()
        manager.list_resources.return_value = ("ASRL5::INSTR",)

        def resource_manager(*args):
            if args == ("@py",):
                raise OSError("pyvisa-py is not installed")
            return manager

        pyvisa = types.ModuleType("pyvisa")
        pyvisa.ResourceManager = resource_manager
        with mock.patch.dict(sys.modules, {"pyvisa": pyvisa}):
            names = list_visa_resources()
        self.assertEqual(names, ["ASRL5::INSTR"])

    def test_voltage_gate_matches_the_cutoff_and_keeps_on_above_off(self):
        self.assertEqual(voltage_gate_levels(60.0), (60.0, 60.0))
        self.assertEqual(voltage_gate_levels(None), (0.1, 0.1))
        self.assertEqual(voltage_gate_levels(0.02), (0.1, 0.1))
        raising = voltage_gate_commands(60.0, current_on=0.1, current_off=0.1)
        self.assertEqual(raising, ["VOLT1:ON 60.000", "VOLT1:OFF 60.000"])
        lowering = voltage_gate_commands(None, current_on=60.0, current_off=60.0)
        self.assertEqual(lowering, ["VOLT1:OFF 0.100", "VOLT1:ON 0.100"])
        self.assertEqual(
            voltage_level_commands(65.0, 60.0, current_on=0.1, current_off=0.1),
            ["VOLT1:ON 65.000", "VOLT1:OFF 60.000"],
        )
        self.assertEqual(_scpi_float("R3.30"), 3.3)
        self.assertEqual(parse_range("HIGH"), "high")
        self.assertEqual(parse_range("RLOW"), "low")
        self.assertEqual(_scpi_float("60.000"), 60.0)

    def test_saved_resource_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "settings.json"
            self.assertIsNone(load_saved_resource(path))
            save_saved_resource(path, "  ASRL/dev/ttyUSB0::INSTR  ")
            self.assertEqual(load_saved_resource(path), "ASRL/dev/ttyUSB0::INSTR")
            path.write_text('{"other": 1}\n', encoding="utf-8")
            save_saved_resource(path, "ASRL3::INSTR")
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["resource"], "ASRL3::INSTR")
            self.assertEqual(stored["other"], 1)
            path.write_text("{", encoding="utf-8")
            self.assertIsNone(load_saved_resource(path))

    def test_list_visa_resources_reports_a_missing_library(self):
        real_import = __import__

        def blocked(name, *args, **kwargs):
            if name == "pyvisa":
                raise ImportError("no pyvisa")
            return real_import(name, *args, **kwargs)

        with mock.patch.dict(sys.modules, {"pyvisa": None}):
            sys.modules.pop("pyvisa", None)
            with mock.patch("builtins.__import__", side_effect=blocked):
                with self.assertRaises(RuntimeError) as raised:
                    list_visa_resources()
        self.assertIn("PyVISA is not installed", str(raised.exception))

    def test_over_voltage_protection_respects_the_voltage_range(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            message = validate_config(config(path, voltage_range="low", ovp_v=150.0))
            self.assertIn("Over-voltage protection", message)
            self.assertIn("21", message)
            current_message = validate_config(config(path, current_range="low", ocp_a=3.2))
            self.assertIn("Over-current protection", current_message)
            self.assertIn("3.1", current_message)
            self.assertIsNone(validate_config(config(path, voltage_range="low", ovp_v=21.0, cutoff_voltage_v=12.0)))
            self.assertIsNone(validate_config(config(path, current_range="low", ocp_a=3.1, current_a=2.0)))
            high_message = validate_config(config(path, voltage_range="high", ovp_v=186.0))
            self.assertIn("185", high_message)
            self.assertIsNone(validate_config(config(path, voltage_range="high", ovp_v=185.0)))

    def test_csv_can_be_left_off(self):
        clock = Clock()
        seen = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            CapacityTest(
                FakeLoad(off_voltages=[70.0], on_voltages=[68.0, 59.0]),
                config(path, csv_enabled=False),
                threading.Event(),
                seen.append,
                now=clock.now,
                sleep=clock.sleep,
            ).run()
            self.assertFalse(path.exists())
        self.assertGreaterEqual(len(seen), 2)
        self.assertGreater(seen[-1].capacity_ah, 0)

    def test_requires_a_cutoff(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "log.csv"
            message = validate_config(config(path, cutoff_voltage_v=None))
        self.assertIn("at least one cutoff", message)


if __name__ == "__main__":
    unittest.main()
