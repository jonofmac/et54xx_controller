"""Capacity discharge for an ET54-series electronic load.

Modes are constant current, constant voltage, CC+CV, constant resistance,
and constant power. The test stops when any enabled cutoff is reached:
voltage, current, time, discharge amp-hours, or energy watt-hours.
"""

from __future__ import annotations

import csv
import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

POWER_MAX_W = 200.0
VOLTAGE_MAX_V = 180.0
HIGH_OVP_MAX_V = 185.0
CURRENT_MAX_A = 30.0
LOW_CURRENT_RANGE_MAX_A = 2.9
LOW_VOLTAGE_RANGE_MAX_V = 20.0
LOW_OVP_MAX_V = 21.0
LOW_OCP_MAX_A = 3.1
RANGES = ("low", "high")
MODES = ("CC", "CV", "CCCV", "CR", "CP")
CR_MIN_OHM = 0.05
CR_MAX_OHM = 4500.0
MIN_ON_S = 0.5
MIN_SAMPLE_S = 2.0
VOLTAGE_GATE_MIN_V = 0.1
OCV_WAITS_S = (0.25, 0.5, 1.0, 2.0)

CSV_HEADER = (
    "timestamp",
    "elapsed_s",
    "voltage_v",
    "loaded_voltage_v",
    "open_circuit_voltage_v",
    "current_a",
    "power_w",
    "discharge_ah",
    "energy_wh",
    "load_on",
    "source",
)


@dataclass(frozen=True)
class TestConfig:
    mode: str
    current_a: float | None
    voltage_set_v: float | None
    resistance_ohm: float | None
    power_w: float | None
    sample_interval_s: float
    ocv_enabled: bool
    ocv_wait_s: float
    csv_path: str
    csv_enabled: bool
    voltage_range: str
    current_range: str
    cutoff_voltage_v: float | None
    cutoff_current_a: float | None
    cutoff_time_s: float | None
    cutoff_discharge_ah: float | None
    cutoff_energy_wh: float | None
    sync_voltage_gate: bool
    ovp_v: float | None
    ocp_a: float | None
    program_ranges: bool = True


@dataclass(frozen=True)
class Sample:
    timestamp: datetime
    elapsed_s: float
    voltage_v: float
    loaded_voltage_v: float
    open_circuit_voltage_v: float | None
    current_a: float
    power_w: float
    capacity_ah: float
    energy_wh: float
    load_on: bool
    source: str


def list_visa_resources() -> list[str]:
    """Return serial PyVISA resource names visible on this computer.

    The ET54 load is a serial instrument. Asking only for ``ASRL`` resources
    skips the TCP/IP scan, which otherwise warns about optional packages.
    The pyvisa-py backend is tried before any other installed VISA backend.
    """
    import warnings

    try:
        import pyvisa
    except ImportError as exc:
        raise RuntimeError("PyVISA is not installed, so instruments cannot be listed.") from exc
    last_error = "no VISA backend"
    for backend in ("@py", None):
        try:
            manager = pyvisa.ResourceManager() if backend is None else pyvisa.ResourceManager(backend)
        except Exception as exc:
            last_error = str(exc)
            continue
        found: list[str] | None
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=UserWarning, module=r"pyvisa_py\..*")
                found = [str(name) for name in manager.list_resources("ASRL?*::INSTR")]
        except Exception as exc:
            last_error = str(exc)
            found = None
        finally:
            try:
                manager.close()
            except Exception:
                pass
        if found is None:
            continue
        serial = list(dict.fromkeys(name for name in found if name.upper().startswith("ASRL")))
        serial.sort()
        return serial
    raise RuntimeError(f"Could not list instruments: {last_error}")


def voltage_gate_levels(cutoff_voltage_v: float | None) -> tuple[float, float]:
    """Return the load's own start and turn-off voltages.

    Both match the software voltage cutoff. The instrument then stops sinking
    current at that voltage even if this program is no longer connected. With
    no voltage cutoff, both sit at the lowest setting the load accepts.
    """
    if cutoff_voltage_v is None:
        level = VOLTAGE_GATE_MIN_V
    else:
        level = max(float(cutoff_voltage_v), VOLTAGE_GATE_MIN_V)
    return level, level


def voltage_level_commands(
    on_v: float, off_v: float, current_on: float, current_off: float
) -> list[str]:
    """SCPI commands that move the hardware gate without crossing on and off."""
    on_command = f"VOLT1:ON {on_v:.3f}"
    off_command = f"VOLT1:OFF {off_v:.3f}"
    if on_v >= current_off:
        return [on_command, off_command]
    return [off_command, on_command]


def voltage_gate_commands(
    cutoff_voltage_v: float | None, current_on: float, current_off: float
) -> list[str]:
    """SCPI commands that copy a software voltage cutoff into the load."""
    on_v, off_v = voltage_gate_levels(cutoff_voltage_v)
    return voltage_level_commands(on_v, off_v, current_on, current_off)


def _hardware_gate_note(cutoff_voltage_v: float | None) -> str:
    if cutoff_voltage_v is None:
        return ""
    level, _off = voltage_gate_levels(cutoff_voltage_v)
    return f" Load turns itself off at {level:.3f} V."


def parse_range(value) -> str:
    """Normalize a HIGH/LOW range reply, including the ET54 R prefix."""
    text = str(value).strip()
    if text[:1].upper() == "R" and text[1:].strip().lower() in ("high", "low"):
        text = text[1:]
    text = text.strip().lower()
    if text not in ("high", "low"):
        raise ValueError(f"Unexpected range reply: {value}")
    return text


def _scpi_float(value) -> float:
    """Parse a numeric ET54 reply. Measured values are prefixed with R."""
    text = str(value).strip()
    if text.startswith("R"):
        text = text[1:]
    return float(text.split()[0])


def load_settings(path: str | Path) -> dict:
    """Return the saved settings, or an empty dict when the file is missing."""
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_settings(path: str | Path, **updates) -> None:
    """Update keys in the settings file and keep the rest."""
    data = load_settings(path)
    data.update(updates)
    Path(path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_saved_resource(path: str | Path) -> str | None:
    """Return the resource stored in a settings file, if it has one."""
    resource = load_settings(path).get("resource")
    if not isinstance(resource, str):
        return None
    resource = resource.strip()
    return resource or None


def save_saved_resource(path: str | Path, resource: str) -> None:
    """Remember a resource path, keeping any other settings in the file."""
    save_settings(path, resource=resource.strip())


def load_samples(path: str | Path) -> list[Sample]:
    """Read a discharge log written by this program."""
    file_path = Path(path).expanduser()
    try:
        handle = file_path.open(newline="")
    except OSError as exc:
        raise ValueError(f"Could not read {file_path}: {exc.strerror or exc}") from exc
    with handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{file_path.name} has no header.")
        fields = {(name or "").strip() for name in reader.fieldnames}
        missing = [name for name in CSV_HEADER if name not in fields and name != "discharge_ah"]
        if "discharge_ah" not in fields and "capacity_ah" not in fields:
            missing.append("discharge_ah")
        if missing:
            raise ValueError(f"{file_path.name} is missing columns: {', '.join(missing)}.")
        samples: list[Sample] = []
        for index, raw in enumerate(reader, start=2):
            row = {(key or "").strip(): (value or "").strip() for key, value in raw.items()}
            if not any(row.values()):
                continue
            try:
                samples.append(_sample_from_row(row))
            except ValueError as exc:
                raise ValueError(f"{file_path.name} row {index} {exc}") from exc
    if not samples:
        raise ValueError(f"{file_path.name} has no samples.")
    return samples


def _number(row: dict[str, str], name: str) -> float:
    text = row.get(name, "")
    if text == "":
        raise ValueError(f"is missing {name}.")
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"has a non-numeric {name}.") from None


def _sample_from_row(row: dict[str, str]) -> Sample:
    ocv_text = row.get("open_circuit_voltage_v", "")
    if ocv_text:
        try:
            open_circuit = float(ocv_text)
        except ValueError:
            raise ValueError("has a non-numeric open_circuit_voltage_v.") from None
    else:
        open_circuit = None
    ah_text = row.get("discharge_ah", "") or row.get("capacity_ah", "")
    if ah_text == "":
        raise ValueError("is missing discharge_ah.")
    try:
        capacity_ah = float(ah_text)
    except ValueError:
        raise ValueError("has a non-numeric discharge_ah.") from None
    try:
        timestamp = datetime.fromisoformat(row.get("timestamp", ""))
    except ValueError:
        raise ValueError("has an unreadable timestamp.") from None
    source = row.get("source", "")
    if not source:
        raise ValueError("is missing source.")
    return Sample(
        timestamp=timestamp,
        elapsed_s=_number(row, "elapsed_s"),
        voltage_v=_number(row, "voltage_v"),
        loaded_voltage_v=_number(row, "loaded_voltage_v"),
        open_circuit_voltage_v=open_circuit,
        current_a=_number(row, "current_a"),
        power_w=_number(row, "power_w"),
        capacity_ah=capacity_ah,
        energy_wh=_number(row, "energy_wh"),
        load_on=row.get("load_on", "") == "1",
        source=source,
    )


@dataclass(frozen=True)
class TestResult:
    reason: str
    message: str


def _mode_text(config: TestConfig) -> str:
    if config.mode == "CC":
        return f"CC {config.current_a:.3f} A."
    if config.mode == "CV":
        return f"CV {config.voltage_set_v:.3f} V."
    if config.mode == "CCCV":
        return f"CC+CV {config.current_a:.3f} A, {config.voltage_set_v:.3f} V."
    if config.mode == "CR":
        return f"CR {config.resistance_ohm:.3f} Ω."
    return f"CP {config.power_w:.2f} W."


def _estimated_current(config: TestConfig, voltage: float) -> float:
    if config.mode in ("CC", "CCCV") and config.current_a is not None:
        return config.current_a
    if config.mode == "CP" and config.power_w is not None and voltage > 0:
        return config.power_w / voltage
    if config.mode == "CR" and config.resistance_ohm:
        return voltage / config.resistance_ohm
    return 0.0


def _expected_power_w(config: TestConfig, resting_v: float) -> float | None:
    if config.mode in ("CC", "CCCV") and config.current_a is not None:
        return resting_v * config.current_a
    if config.mode == "CP":
        return config.power_w
    if config.mode == "CR" and config.resistance_ohm:
        return (resting_v * resting_v) / config.resistance_ohm
    return None


def _power_phrase(config: TestConfig, resting_v: float, watts: float) -> str:
    if config.mode == "CP":
        return f"Power setpoint {watts:.0f} W is"
    if config.mode == "CR":
        return f"{resting_v:.2f} V across {config.resistance_ohm:.3f} Ω is {watts:.0f} W,"
    return f"{resting_v:.2f} V at {config.current_a:.2f} A is {watts:.0f} W,"


def _in_range(value: float | None, maximum: float) -> bool:
    return value is not None and math.isfinite(value) and 0 < value <= maximum


def _above_zero(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def validate_config(config: TestConfig) -> str | None:
    if config.mode not in MODES:
        return "Discharge mode must be CC, CV, CC+CV, CR, or CP."
    if config.voltage_range not in RANGES:
        return "Voltage range must be low or high."
    if config.current_range not in RANGES:
        return "Current range must be low or high."

    current_limit = LOW_CURRENT_RANGE_MAX_A if config.current_range == "low" else CURRENT_MAX_A
    voltage_limit = LOW_VOLTAGE_RANGE_MAX_V if config.voltage_range == "low" else VOLTAGE_MAX_V
    ovp_limit = LOW_OVP_MAX_V if config.voltage_range == "low" else HIGH_OVP_MAX_V
    ocp_limit = LOW_OCP_MAX_A if config.current_range == "low" else CURRENT_MAX_A
    if config.ovp_v is not None and not _in_range(config.ovp_v, ovp_limit):
        return (
            f"Over-voltage protection must be greater than 0 and at most {ovp_limit:.0f} V "
            f"on the {config.voltage_range} voltage range."
        )
    if config.ocp_a is not None and not _in_range(config.ocp_a, ocp_limit):
        return (
            f"Over-current protection must be greater than 0 and at most {ocp_limit:.1f} A "
            f"on the {config.current_range} current range."
        )
    if config.mode in ("CC", "CCCV") and not _in_range(config.current_a, current_limit):
        return (
            f"Discharge current must be greater than 0 and at most {current_limit:.1f} A "
            f"on the {config.current_range} current range."
        )
    if config.mode in ("CV", "CCCV") and not _in_range(config.voltage_set_v, voltage_limit):
        return (
            f"CV voltage must be greater than 0 and at most {voltage_limit:.0f} V "
            f"on the {config.voltage_range} voltage range."
        )
    if config.mode == "CR" and (
        config.resistance_ohm is None
        or not math.isfinite(config.resistance_ohm)
        or not CR_MIN_OHM <= config.resistance_ohm <= CR_MAX_OHM
    ):
        return f"Resistance must be between {CR_MIN_OHM:g} Ω and {CR_MAX_OHM:.0f} Ω."
    if config.mode == "CP" and not _in_range(config.power_w, POWER_MAX_W):
        return f"Power must be greater than 0 and at most {POWER_MAX_W:.0f} W."

    if (
        config.mode in ("CC", "CCCV")
        and config.cutoff_current_a is not None
        and config.current_a is not None
        and config.cutoff_current_a >= config.current_a
    ):
        return (
            "Current cutoff is at or above the discharge current, "
            "so the test would stop on the first sample."
        )

    enabled = 0
    if config.cutoff_voltage_v is not None:
        enabled += 1
        if not _in_range(config.cutoff_voltage_v, voltage_limit):
            return (
                f"Voltage cutoff must be greater than 0 and at most {voltage_limit:.0f} V "
                f"on the {config.voltage_range} voltage range."
            )
    if config.cutoff_current_a is not None:
        enabled += 1
        if not _in_range(config.cutoff_current_a, CURRENT_MAX_A):
            return f"Current cutoff must be greater than 0 and at most {CURRENT_MAX_A:.0f} A."
    if config.cutoff_time_s is not None:
        enabled += 1
        if not _above_zero(config.cutoff_time_s):
            return "Time cutoff must be greater than 0."
    if config.cutoff_discharge_ah is not None:
        enabled += 1
        if not _above_zero(config.cutoff_discharge_ah):
            return "Discharge cutoff must be greater than 0 Ah."
    if config.cutoff_energy_wh is not None:
        enabled += 1
        if not _above_zero(config.cutoff_energy_wh):
            return "Energy cutoff must be greater than 0 Wh."
    if enabled == 0:
        return "Enable at least one cutoff."

    if not math.isfinite(config.sample_interval_s) or config.sample_interval_s < MIN_SAMPLE_S:
        return f"Sample interval must be at least {MIN_SAMPLE_S:.0f} s."
    if config.ocv_enabled:
        if config.ocv_wait_s not in OCV_WAITS_S:
            choices = ", ".join(f"{wait:g}" for wait in OCV_WAITS_S)
            return f"Open-circuit settle time must be one of {choices} s."
        needed = config.ocv_wait_s + MIN_ON_S
        if config.sample_interval_s < needed:
            return (
                f"Sample interval must be at least {needed:.2f} s so the load "
                f"can stay on for {MIN_ON_S:.1f} s and then settle for {config.ocv_wait_s:g} s."
            )
    if config.csv_enabled:
        path = Path(config.csv_path).expanduser()
        if not config.csv_path.strip():
            return "Choose a CSV file for the log."
        if path.exists() and path.is_dir():
            return "The CSV path is a folder."
        parent = path.parent
        if not parent.exists():
            return f"Folder does not exist: {parent}"
    return None


class ET54Load:
    """Channel-1 adapter."""

    def __init__(self, resource: str, baudrate: int = 9600, model: str | None = None):
        from ET54 import ET54

        kwargs: dict = {"baudrate": baudrate}
        if model:
            kwargs["model"] = model
        self._io = threading.RLock()
        self.el = ET54(resource, **kwargs)
        identity = self.el.idn
        serial = identity.get("SN") or ""
        self.identity = f"{identity.get('model', 'ET54')} {serial}".strip()

    def close(self) -> None:
        with self._io:
            self.el.close()

    def ping(self) -> None:
        """Confirm the instrument still answers."""
        with self._io:
            self.el.query("*IDN?")

    def read_ranges(self) -> tuple[str, str]:
        """Return the voltage and current ranges currently selected on the load."""
        with self._io:
            channel = self.el.ch1
            return parse_range(channel.Vrange), parse_range(channel.Crange)

    def apply_ranges(self, voltage_range: str, current_range: str) -> None:
        with self._io:
            channel = self.el.ch1
            channel.off()
            channel.Vrange = voltage_range
            channel.Crange = current_range

    def setup_mode(self, config: TestConfig) -> None:
        # Changing range makes the load replace its own protection presets.
        # Leave the range alone when this run is not asking for a new one,
        # so a start does not overwrite limits the operator has not edited.
        if config.program_ranges:
            self.apply_ranges(config.voltage_range, config.current_range)
        else:
            self.off()
        with self._io:
            self._program_mode(config)

    def _program_mode(self, config: TestConfig) -> None:
        channel = self.el.ch1
        if config.ovp_v is not None:
            channel.OVP = config.ovp_v
        channel.OPP = POWER_MAX_W
        if config.ocp_a is not None:
            channel.OCP = config.ocp_a
        if config.sync_voltage_gate:
            self._apply_voltage_gate(channel, config.cutoff_voltage_v)
        if config.mode == "CC":
            channel.CC_mode(config.current_a)
        elif config.mode == "CV":
            channel.CV_mode(config.voltage_set_v)
        elif config.mode == "CCCV":
            channel.CCCV_mode(config.current_a, config.voltage_set_v)
        elif config.mode == "CR":
            channel.CR_mode(config.resistance_ohm)
        elif config.mode == "CP":
            channel.CP_mode(config.power_w)
        channel.off()

    def _apply_voltage_gate(self, channel, cutoff_voltage_v: float | None) -> None:
        current_on = _scpi_float(channel.query("VOLT1:ON?"))
        current_off = _scpi_float(channel.query("VOLT1:OFF?"))
        for command in voltage_gate_commands(cutoff_voltage_v, current_on, current_off):
            channel.write(command)

    def read_protections(self) -> tuple[float, float]:
        """Return the load's over-voltage and over-current limits."""
        with self._io:
            channel = self.el.ch1
            return float(channel.OVP), float(channel.OCP)

    def write_protections(self, ovp_v: float, ocp_a: float) -> tuple[float, float]:
        """Store over-voltage and over-current limits and return what the load kept."""
        with self._io:
            channel = self.el.ch1
            channel.OVP = ovp_v
            channel.OCP = ocp_a
            return float(channel.OVP), float(channel.OCP)

    def write_over_voltage(self, ovp_v: float) -> float:
        """Store over-voltage protection and return what the load kept."""
        with self._io:
            channel = self.el.ch1
            channel.OVP = ovp_v
            return float(channel.OVP)

    def write_over_current(self, ocp_a: float) -> float:
        """Store over-current protection and return what the load kept."""
        with self._io:
            channel = self.el.ch1
            channel.OCP = ocp_a
            return float(channel.OCP)

    def read_voltage_gate(self) -> tuple[float, float]:
        """Return the load's start voltage and turn-off voltage."""
        with self._io:
            channel = self.el.ch1
            return (
                _scpi_float(channel.query("VOLT1:ON?")),
                _scpi_float(channel.query("VOLT1:OFF?")),
            )

    def write_voltage_gate(self, cutoff_voltage_v: float | None) -> tuple[float, float]:
        """Store the voltage cutoff in the load and return the values it kept."""
        on_v, off_v = voltage_gate_levels(cutoff_voltage_v)
        return self.write_voltage_levels(on_v, off_v)

    def write_voltage_levels(self, on_v: float, off_v: float) -> tuple[float, float]:
        """Store a start voltage and turn-off voltage and return what the load kept."""
        with self._io:
            channel = self.el.ch1
            current_on = _scpi_float(channel.query("VOLT1:ON?"))
            current_off = _scpi_float(channel.query("VOLT1:OFF?"))
            for command in voltage_level_commands(on_v, off_v, current_on, current_off):
                channel.write(command)
            return (
                _scpi_float(channel.query("VOLT1:ON?")),
                _scpi_float(channel.query("VOLT1:OFF?")),
            )

    def on(self) -> None:
        with self._io:
            self.el.ch1.on()

    def off(self) -> None:
        with self._io:
            self.el.ch1.off()

    def read_voltage(self) -> float:
        with self._io:
            return float(self.el.ch1.read_voltage())

    def read_current(self) -> float:
        with self._io:
            return float(self.el.ch1.read_current())


class CapacityTest:
    def __init__(
        self,
        load,
        config: TestConfig,
        stop_event: threading.Event,
        on_sample,
        on_status=None,
        now=None,
        sleep=None,
    ):
        self.load = load
        self.config = config
        self.stop_event = stop_event
        self.on_sample = on_sample
        self.on_status = on_status or (lambda _message: None)
        self.now = now or time.monotonic
        self.sleep = sleep or time.sleep
        self.off_error: Exception | None = None
        self.capacity_ah = 0.0
        self.energy_wh = 0.0
        self.t0 = 0.0
        self.last_loaded_v = 0.0
        self.last_loaded_i = 0.0
        self.stop_limits: list[str] = []
        self._mark: float | None = None
        self._file = None
        self._writer: csv.writer | None = None
        self.last: Sample | None = None

    def run(self) -> TestResult:
        try:
            return self._run()
        finally:
            self._shutdown()

    def _run(self) -> TestResult:
        error = validate_config(self.config)
        if error:
            raise ValueError(error)

        self.load.off()
        self.load.setup_mode(self.config)
        self.on_status(
            f"{_mode_text(self.config)} "
            f"{self.config.voltage_range.capitalize()} voltage range, "
            f"{self.config.current_range} current range."
            + (
                _hardware_gate_note(self.config.cutoff_voltage_v)
                if self.config.sync_voltage_gate
                else ""
            )
        )
        resting = float(self.load.read_voltage())
        if self.config.voltage_range == "low" and resting > LOW_VOLTAGE_RANGE_MAX_V:
            return TestResult(
                "voltage_range",
                (
                    f"Resting voltage {resting:.2f} V is above the low voltage range "
                    f"({LOW_VOLTAGE_RANGE_MAX_V:.0f} V). Switch the voltage range to high. "
                    "Load left off."
                ),
            )
        watts = _expected_power_w(self.config, resting)
        if watts is not None and watts > POWER_MAX_W:
            return TestResult(
                "power_limit",
                (
                    f"{_power_phrase(self.config, resting, watts)} "
                    f"above the {POWER_MAX_W:.0f} W rating. Load left off."
                ),
            )

        self.t0 = self.now()
        self.last_loaded_v = resting
        self.last_loaded_i = _estimated_current(self.config, resting)
        self._emit(
            voltage=resting,
            loaded_voltage=resting,
            open_circuit=resting,
            current=0.0,
            load_on=False,
            source="resting",
        )
        if (
            self.config.cutoff_voltage_v is not None
            and resting <= self.config.cutoff_voltage_v
        ):
            return TestResult(
                "already_at_cutoff",
                (
                    f"Resting voltage {resting:.3f} V is already at or below "
                    f"cutoff {self.config.cutoff_voltage_v:.3f} V. Load left off."
                ),
            )

        reason = "stopped"
        sample_number = 0
        while not self.stop_event.is_set():
            sample_number += 1
            target = self.t0 + sample_number * self.config.sample_interval_s
            settle = self.config.ocv_wait_s if self.config.ocv_enabled else 0.0
            self._set_load(True)
            remain = (target - settle) - self.now()
            time_left = None
            if self.config.cutoff_time_s is not None:
                time_left = self.config.cutoff_time_s - (self.now() - self.t0)
                remain = min(remain, max(0.0, time_left))
            elif remain < MIN_ON_S:
                remain = MIN_ON_S
            if not self._wait(remain):
                self._integrate_estimated()
                self._set_load(False)
                self._emit(
                    voltage=self.last_loaded_v,
                    loaded_voltage=self.last_loaded_v,
                    open_circuit=None,
                    current=self.last_loaded_i,
                    load_on=False,
                    source="loaded",
                )
                reason = "stopped"
                break

            loaded_v, loaded_i = self._integrate_loaded()
            if self.config.ocv_enabled:
                self._set_load(False)
                if not self._wait(settle):
                    self._emit(
                        voltage=loaded_v,
                        loaded_voltage=loaded_v,
                        open_circuit=None,
                        current=loaded_i,
                        load_on=False,
                        source="loaded",
                    )
                    reason = "stopped"
                    break
                open_v = float(self.load.read_voltage())
                self._emit(
                    voltage=open_v,
                    loaded_voltage=loaded_v,
                    open_circuit=open_v,
                    current=loaded_i,
                    load_on=False,
                    source="open_circuit",
                )
                decision = open_v
            else:
                self._emit(
                    voltage=loaded_v,
                    loaded_voltage=loaded_v,
                    open_circuit=None,
                    current=loaded_i,
                    load_on=True,
                    source="loaded",
                )
                decision = loaded_v

            matched = self._matched_cutoffs(decision, loaded_i, self.last.elapsed_s if self.last else 0.0)
            if matched:
                self._set_load(False)
                self.stop_limits = matched
                reason = "cutoff"
                break

        return self._summarize(reason)

    def _set_load(self, enabled: bool) -> None:
        if enabled:
            self.load.on()
            if self._mark is None:
                self._mark = self.now()
        else:
            self.load.off()
            self._mark = None

    def _integrate_loaded(self) -> tuple[float, float]:
        loaded_v = float(self.load.read_voltage())
        loaded_i = float(self.load.read_current())
        self.last_loaded_v = loaded_v
        self.last_loaded_i = loaded_i
        self._add_charge(loaded_v, loaded_i)
        return loaded_v, loaded_i

    def _matched_cutoffs(self, voltage: float, current: float, elapsed: float) -> list[str]:
        matched: list[str] = []
        if self.config.cutoff_voltage_v is not None and voltage <= self.config.cutoff_voltage_v:
            matched.append("voltage")
        if self.config.cutoff_current_a is not None and current <= self.config.cutoff_current_a:
            matched.append("current")
        if self.config.cutoff_time_s is not None and elapsed >= self.config.cutoff_time_s:
            matched.append("time")
        if self.config.cutoff_discharge_ah is not None and self.capacity_ah >= self.config.cutoff_discharge_ah:
            matched.append("discharge Ah")
        if self.config.cutoff_energy_wh is not None and self.energy_wh >= self.config.cutoff_energy_wh:
            matched.append("energy Wh")
        return matched

    def _integrate_estimated(self) -> None:
        """Account for on-time already elapsed, without another meter read."""
        self._add_charge(self.last_loaded_v, self.last_loaded_i)

    def _add_charge(self, voltage: float, current: float) -> None:
        if self._mark is None:
            return
        dt = max(0.0, self.now() - self._mark)
        amps = max(0.0, current)
        self.capacity_ah += amps * dt / 3600.0
        self.energy_wh += max(0.0, voltage) * amps * dt / 3600.0
        self._mark = self.now()

    def _emit(
        self,
        voltage: float,
        loaded_voltage: float,
        open_circuit: float | None,
        current: float,
        load_on: bool,
        source: str,
    ) -> Sample:
        elapsed = max(0.0, self.now() - self.t0)
        sample = Sample(
            timestamp=datetime.now(),
            elapsed_s=elapsed,
            voltage_v=voltage,
            loaded_voltage_v=loaded_voltage,
            open_circuit_voltage_v=open_circuit,
            current_a=current,
            power_w=max(0.0, loaded_voltage) * max(0.0, current),
            capacity_ah=self.capacity_ah,
            energy_wh=self.energy_wh,
            load_on=load_on,
            source=source,
        )
        self.last = sample
        self._write(sample)
        self.on_sample(sample)
        return sample

    def _write(self, sample: Sample) -> None:
        if not self.config.csv_enabled:
            return
        if self._writer is None:
            path = Path(self.config.csv_path).expanduser()
            self._file = path.open("w", newline="", encoding="utf-8")
            self._writer = csv.writer(self._file)
            self._writer.writerow(CSV_HEADER)
        open_text = "" if sample.open_circuit_voltage_v is None else f"{sample.open_circuit_voltage_v:.4f}"
        self._writer.writerow(
            [
                sample.timestamp.isoformat(timespec="milliseconds"),
                f"{sample.elapsed_s:.3f}",
                f"{sample.voltage_v:.4f}",
                f"{sample.loaded_voltage_v:.4f}",
                open_text,
                f"{sample.current_a:.4f}",
                f"{sample.power_w:.4f}",
                f"{sample.capacity_ah:.6f}",
                f"{sample.energy_wh:.4f}",
                "1" if sample.load_on else "0",
                sample.source,
            ]
        )
        self._file.flush()

    def _summarize(self, reason: str) -> TestResult:
        if self.last is None:
            detail = ""
            voltage = ""
        else:
            detail = (
                f" Discharge {self.last.capacity_ah:.3f} Ah, "
                f"energy {self.last.energy_wh:.1f} Wh."
            )
        if reason == "cutoff":
            names = ", ".join(self.stop_limits) or "cutoff"
            return TestResult("cutoff", f"Cutoff reached ({names}).{detail}")
        return TestResult("stopped", f"Stopped.{detail}")

    def _wait(self, seconds: float) -> bool:
        end = self.now() + max(0.0, seconds)
        while self.now() < end:
            if self.stop_event.is_set():
                return False
            self.sleep(min(0.05, end - self.now()))
        return True

    def _shutdown(self) -> None:
        try:
            self.load.off()
        except Exception as exc:
            self.off_error = exc
        if self._file is not None:
            try:
                self._file.flush()
                self._file.close()
            finally:
                self._file = None
                self._writer = None
