#!/usr/bin/env python3
"""Command-line setup and discharge for an ET54 electronic load.

The menu edits the same settings file as the windowed app, then starts a
discharge. During the run the terminal shows voltage, current, discharge
amp-hours, energy, and elapsed time. It also draws the voltage, current, and
discharge charts, and a bar from the voltage cutoff up to the voltage measured
at the start of the run.

    python et54xx_cli.py
    python et54xx_cli.py --start
    python et54xx_cli.py --mode CC --current 2 --cutoff-voltage 40 --csv pack.csv --start
"""

from __future__ import annotations

import argparse
import math
import os
import signal
import sys
import textwrap
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from capacity import (
    CURRENT_MAX_A,
    HIGH_OVP_MAX_V,
    LOW_OCP_MAX_A,
    LOW_OVP_MAX_V,
    MIN_SAMPLE_S,
    OCV_WAITS_S,
    POWER_MAX_W,
    CapacityTest,
    ET54Load,
    Sample,
    TestConfig,
    list_visa_resources,
    load_settings,
    save_settings,
    validate_config,
)

RESOURCE_PROMPT = "Click Select or enter resource path"
MODE_LABELS = {
    "CC": "Constant current",
    "CV": "Constant voltage",
    "CCCV": "CC + CV",
    "CR": "Constant resistance",
    "CP": "Constant power",
}
MODE_WORDS = {
    "cc": "CC",
    "constant current": "CC",
    "cv": "CV",
    "constant voltage": "CV",
    "cccv": "CCCV",
    "cc+cv": "CCCV",
    "cc + cv": "CCCV",
    "cr": "CR",
    "constant resistance": "CR",
    "cp": "CP",
    "constant power": "CP",
}
ERROR_REASONS = {"power_limit", "already_at_cutoff", "voltage_range"}
# Half blocks. A terminal that draws braille as a speckle makes the trace
# unreadable, and these same blocks already render solidly in the cutoff bars.
UPPER_BLOCK = "▀"
LOWER_BLOCK = "▄"
FULL_BLOCK = "█"
GUIDE_MARK = "·"
LOADED_MARK = "░"
FIELDS = (
    (1, "Resource", "resource"),
    (2, "Mode", "mode"),
    (3, "Current", "current"),
    (4, "CV voltage", "voltage"),
    (5, "Resistance", "resistance"),
    (6, "Power", "power"),
    (7, "Voltage range", "voltage_range"),
    (8, "Current range", "current_range"),
    (9, "Sample interval", "interval"),
    (10, "Write CSV", "csv_enabled"),
    (11, "CSV file", "csv_path"),
    (12, "Open-circuit voltage", "ocv"),
    (13, "Settle time", "ocv_wait"),
    (14, "Voltage cutoff", "cutoff_voltage"),
    (15, "Current cutoff", "cutoff_current"),
    (16, "Time cutoff", "cutoff_time"),
    (17, "Discharge cutoff", "cutoff_ah"),
    (18, "Energy cutoff", "cutoff_wh"),
    (19, "Match voltage cutoff", "sync"),
    (20, "Device limits", "limits"),
)
FIELD_BY_NUMBER = {number: kind for number, _label, kind in FIELDS}
COMMANDS = {
    "q": "quit",
    "quit": "quit",
    "exit": "quit",
    "s": "start",
    "start": "start",
    "r": "ports",
    "ports": "ports",
    "list": "ports",
    "c": "connect",
    "connect": "connect",
    "d": "disconnect",
    "disconnect": "disconnect",
    "a": "apply",
    "apply": "apply",
    "?": "help",
    "help": "help",
}
HELP = "Enter a number to change a setting.  Esc cancels.  r ports   c connect   d disconnect   a apply ranges   s start   q quit"
MENU_VOLTAGE_S = 1.0
STATUS_REDRAW_S = 1.0


@dataclass
class DischargeForm:
    """Settings edited by the menu. Numeric fields stay text, as in the window."""

    resource: str = ""
    mode: str = "CC"
    current_a: str = "2.0"
    voltage_set_v: str = "60.0"
    resistance_ohm: str = "10"
    power_w: str = "100"
    sample_interval_s: str = "5"
    csv_enabled: bool = True
    csv_path: str = "capacity_log.csv"
    ocv_enabled: bool = False
    ocv_wait_s: str = "1"
    voltage_range: str = "high"
    current_range: str = "low"
    cutoff_voltage_on: bool = True
    cutoff_voltage_v: str = "5.0"
    cutoff_current_on: bool = False
    cutoff_current_a: str = "0.20"
    cutoff_time_on: bool = False
    cutoff_time_s: str = "3600"
    cutoff_ah_on: bool = False
    cutoff_ah: str = "10"
    cutoff_wh_on: bool = False
    cutoff_wh: str = "500"
    sync_voltage_gate: bool = False

    def to_updates(self) -> dict:
        """Values written into settings.json. Types match what the window reads."""
        return {
            "resource": self.resource.strip(),
            "mode": self.mode,
            "current_a": self.current_a.strip(),
            "voltage_set_v": self.voltage_set_v.strip(),
            "resistance_ohm": self.resistance_ohm.strip(),
            "power_w": self.power_w.strip(),
            "sample_interval_s": self.sample_interval_s.strip(),
            "csv_enabled": bool(self.csv_enabled),
            "csv_path": self.csv_path.strip(),
            "ocv_enabled": bool(self.ocv_enabled),
            "ocv_wait_s": self.ocv_wait_s.strip(),
            "voltage_range": self.voltage_range,
            "current_range": self.current_range,
            "cutoff_voltage_on": bool(self.cutoff_voltage_on),
            "cutoff_voltage_v": self.cutoff_voltage_v.strip(),
            "cutoff_current_on": bool(self.cutoff_current_on),
            "cutoff_current_a": self.cutoff_current_a.strip(),
            "cutoff_time_on": bool(self.cutoff_time_on),
            "cutoff_time_s": self.cutoff_time_s.strip(),
            "cutoff_ah_on": bool(self.cutoff_ah_on),
            "cutoff_ah": self.cutoff_ah.strip(),
            "cutoff_wh_on": bool(self.cutoff_wh_on),
            "cutoff_wh": self.cutoff_wh.strip(),
            "sync_voltage_gate": bool(self.sync_voltage_gate),
        }


@dataclass
class Connection:
    load: object = None
    resource: str = ""
    identity: str = ""
    resting_v: float | None = None
    limits: tuple[float, float, float, float] | None = None


@dataclass
class StatusView:
    mode_text: str
    identity: str
    ranges: str
    phase: str
    elapsed_s: float | None
    samples: list[Sample]
    cutoff_voltage: float | None
    cutoff_current: float | None
    cutoff_ah: float | None
    start_voltage: float | None
    ocv: bool
    status: str
    error: bool
    csv_note: str
    cutoff_time_s: float | None = None
    cutoff_energy_wh: float | None = None
    start_current: float | None = None


class RunState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.samples: list[Sample] = []
        self.status = "Starting."
        self.error = False
        self.result = None
        self.failed: str | None = None
        self.start_voltage: float | None = None
        self.start_current: float | None = None
        self.identity = ""
        self.anchor_elapsed = 0.0
        self.anchor_wall = time.monotonic()
        self.done = threading.Event()

    def add_sample(self, sample: Sample) -> None:
        with self.lock:
            self.samples.append(sample)
            if self.start_voltage is None:
                self.start_voltage = sample.voltage_v
            if self.start_current is None and sample.source != "resting":
                self.start_current = sample.current_a
            self.anchor_elapsed = sample.elapsed_s
            self.anchor_wall = time.monotonic()

    def set_status(self, text: str) -> None:
        with self.lock:
            self.status = text

    def set_identity(self, identity: str) -> None:
        with self.lock:
            self.identity = identity

    def finish(self, result, failed: str | None) -> None:
        with self.lock:
            self.result = result
            self.failed = failed
            if failed:
                self.status = failed
                self.error = True
            elif result is not None:
                self.status = result.message
                self.error = result.reason in ERROR_REASONS
            self.done.set()

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "samples": list(self.samples),
                "status": self.status,
                "error": self.error,
                "result": self.result,
                "failed": self.failed,
                "start_voltage": self.start_voltage,
                "start_current": self.start_current,
                "identity": self.identity,
                "anchor_elapsed": self.anchor_elapsed,
                "anchor_wall": self.anchor_wall,
                "done": self.done.is_set(),
            }


def format_number(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


def compact(value: float) -> str:
    abs_value = abs(value)
    if abs_value >= 1000:
        text = f"{value:.0f}"
    elif abs_value >= 100:
        text = f"{value:.1f}"
    elif abs_value >= 10:
        text = f"{value:.2f}"
    else:
        text = f"{value:.3f}"
    return text[:7]


def parse_mode(text: str) -> str | None:
    return MODE_WORDS.get(text.strip().lower())


def parse_range_choice(text: str) -> str | None:
    word = text.strip().lower()
    if word in ("low", "high"):
        return word
    return None


def parse_ocv_wait(text: str) -> float | None:
    try:
        value = float(text.strip().split()[0])
    except (ValueError, IndexError):
        return None
    if value in OCV_WAITS_S:
        return value
    return None


def parse_optional_limit(text: str, enabled: bool, previous: str, label: str):
    """Return the new enabled flag, stored text, and an error.

    A blank entry keeps the previous cutoff. ``off`` disables it.
    """
    raw = text.strip()
    if raw == "":
        return enabled, previous, None
    if raw.lower() in {"off", "none", "-", "disable", "disabled"}:
        return False, previous, None
    try:
        value = float(raw)
    except ValueError:
        return enabled, previous, f"{label} must be a number, or off."
    if not math.isfinite(value) or value <= 0:
        return enabled, previous, f"{label} must be greater than 0, or off."
    return True, format_number(value), None


def parse_bounded(text: str, label: str, low: float | None = None, high: float | None = None):
    """Parse a setpoint. ``low`` and ``high`` are inclusive when given."""
    try:
        value = float(text)
    except ValueError:
        return None, f"{label} must be a number."
    if not math.isfinite(value):
        return None, f"{label} must be a number."
    if low is not None and high is not None:
        if not low <= value <= high:
            return None, f"{label} must be between {low:g} and {high:g}."
        return value, None
    if high is not None:
        if not 0 < value <= high:
            return None, f"{label} must be greater than 0 and at most {high:g}."
        return value, None
    if low is not None and value < low:
        return None, f"{label} must be at least {low:g}."
    if value <= 0:
        return None, f"{label} must be greater than 0."
    return value, None


def entry_problem(text: str) -> str | None:
    """Reject keystrokes that are not part of a value, such as Escape."""
    if any(unicodedata.category(character).startswith("C") for character in text):
        return "Control characters are not allowed. The value was left unchanged."
    return None


def csv_path_problem(text: str) -> str | None:
    problem = entry_problem(text)
    if problem:
        return problem
    path = text.strip()
    if not path:
        return None
    if path.endswith(("/", "\\")):
        return "CSV file must be a file, not a folder."
    name = Path(path).name
    if not name or name in {".", ".."}:
        return "CSV file needs a file name."
    if any(character in name for character in '<>:"|?*'):
        return "CSV file name contains a character the file system rejects."
    return None


def resource_problem(text: str) -> str | None:
    problem = entry_problem(text)
    if problem:
        return problem
    if any(character.isspace() for character in text.strip()):
        return "Resource path cannot contain spaces."
    return None


def apply_menu_key(buffer: str, key: str) -> tuple[str, str | None]:
    """Apply one typed key. A finished line is returned as the second value."""
    if key in ("\r", "\n"):
        return "", buffer
    if key in ("\x7f", "\b"):
        return buffer[:-1], None
    if key == "\x03":
        raise KeyboardInterrupt
    if key == "\x04" and not buffer:
        raise EOFError
    if key.isprintable():
        return buffer + key, None
    return buffer, None


def classify_command(text: str) -> tuple[str, int | None]:
    word = text.strip().lower()
    if not word:
        return "unknown", None
    if word.isdigit():
        return "edit", int(word)
    action = COMMANDS.get(word)
    if action is None:
        return "unknown", None
    return action, None


def _flag(data: dict, key: str, default: bool) -> bool:
    value = data.get(key)
    if isinstance(value, bool):
        return value
    return default


def _text(data: dict, key: str, default: str) -> str:
    value = data.get(key)
    if isinstance(value, str) and value.strip() and entry_problem(value) is None:
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return format_number(float(value))
    return default


def form_from_settings(data: dict | None) -> DischargeForm:
    """Build a form from settings.json. Missing keys use the window's defaults."""
    loaded = data if isinstance(data, dict) else {}
    mode = parse_mode(str(loaded.get("mode", ""))) or "CC"
    voltage_range = str(loaded.get("voltage_range", "")).strip().lower()
    current_range = str(loaded.get("current_range", "")).strip().lower()
    if voltage_range not in ("low", "high"):
        voltage_range = "high"
    if current_range not in ("low", "high"):
        current_range = "low"
    wait = parse_ocv_wait(str(loaded.get("ocv_wait_s", "1")))
    resource = loaded.get("resource")
    if not isinstance(resource, str):
        resource = ""
    resource = resource.strip()
    if resource == RESOURCE_PROMPT or entry_problem(resource):
        resource = ""
    return DischargeForm(
        resource=resource,
        mode=mode,
        current_a=_text(loaded, "current_a", "2.0"),
        voltage_set_v=_text(loaded, "voltage_set_v", "60.0"),
        resistance_ohm=_text(loaded, "resistance_ohm", "10"),
        power_w=_text(loaded, "power_w", "100"),
        sample_interval_s=_text(loaded, "sample_interval_s", "5"),
        csv_enabled=_flag(loaded, "csv_enabled", True),
        csv_path=_text(loaded, "csv_path", "capacity_log.csv"),
        ocv_enabled=_flag(loaded, "ocv_enabled", False),
        ocv_wait_s=format_number(wait) if wait is not None else "1",
        voltage_range=voltage_range,
        current_range=current_range,
        cutoff_voltage_on=_flag(loaded, "cutoff_voltage_on", True),
        cutoff_voltage_v=_text(loaded, "cutoff_voltage_v", "5.0"),
        cutoff_current_on=_flag(loaded, "cutoff_current_on", False),
        cutoff_current_a=_text(loaded, "cutoff_current_a", "0.20"),
        cutoff_time_on=_flag(loaded, "cutoff_time_on", False),
        cutoff_time_s=_text(loaded, "cutoff_time_s", "3600"),
        cutoff_ah_on=_flag(loaded, "cutoff_ah_on", False),
        cutoff_ah=_text(loaded, "cutoff_ah", "10"),
        cutoff_wh_on=_flag(loaded, "cutoff_wh_on", False),
        cutoff_wh=_text(loaded, "cutoff_wh", "500"),
        sync_voltage_gate=_flag(loaded, "sync_voltage_gate", False),
    )


def _enabled_float(enabled: bool, text: str) -> float | None:
    if not enabled:
        return None
    return float(text)


def build_config(
    form: DischargeForm,
    ovp_v: float | None = None,
    ocp_a: float | None = None,
    program_ranges: bool = True,
):
    """Return a tested configuration, or a message describing what to fix."""
    try:
        interval = float(form.sample_interval_s)
        current = float(form.current_a) if form.mode in ("CC", "CCCV") else None
        voltage_set = float(form.voltage_set_v) if form.mode in ("CV", "CCCV") else None
        resistance = float(form.resistance_ohm) if form.mode == "CR" else None
        power = float(form.power_w) if form.mode == "CP" else None
        cutoff_voltage = _enabled_float(form.cutoff_voltage_on, form.cutoff_voltage_v)
        cutoff_current = _enabled_float(form.cutoff_current_on, form.cutoff_current_a)
        cutoff_time = _enabled_float(form.cutoff_time_on, form.cutoff_time_s)
        cutoff_ah = _enabled_float(form.cutoff_ah_on, form.cutoff_ah)
        cutoff_wh = _enabled_float(form.cutoff_wh_on, form.cutoff_wh)
        wait = float(form.ocv_wait_s)
    except ValueError:
        return None, "Setpoints, cutoffs, and the sample interval must be numbers."
    config = TestConfig(
        mode=form.mode,
        current_a=current,
        voltage_set_v=voltage_set,
        resistance_ohm=resistance,
        power_w=power,
        sample_interval_s=interval,
        ocv_enabled=bool(form.ocv_enabled),
        ocv_wait_s=wait,
        csv_path=form.csv_path.strip(),
        csv_enabled=bool(form.csv_enabled),
        voltage_range=form.voltage_range,
        current_range=form.current_range,
        cutoff_voltage_v=cutoff_voltage,
        cutoff_current_a=cutoff_current,
        cutoff_time_s=cutoff_time,
        cutoff_discharge_ah=cutoff_ah,
        cutoff_energy_wh=cutoff_wh,
        sync_voltage_gate=bool(form.sync_voltage_gate),
        ovp_v=ovp_v,
        ocp_a=ocp_a,
        program_ranges=program_ranges,
    )
    error = validate_config(config)
    if error:
        return None, error
    return config, None


def mode_summary(config: TestConfig) -> str:
    if config.mode == "CC":
        return f"CC {config.current_a:.3f} A"
    if config.mode == "CV":
        return f"CV {config.voltage_set_v:.3f} V"
    if config.mode == "CCCV":
        return f"CC+CV {config.current_a:.3f} A, {config.voltage_set_v:.3f} V"
    if config.mode == "CR":
        return f"CR {config.resistance_ohm:.3f} ohm"
    return f"CP {config.power_w:.2f} W"


def cutoff_phrase(config: TestConfig) -> str:
    parts = []
    if config.cutoff_voltage_v is not None:
        parts.append(f"voltage ≤ {config.cutoff_voltage_v:.3f} V")
    if config.cutoff_current_a is not None:
        parts.append(f"current ≤ {config.cutoff_current_a:.3f} A")
    if config.cutoff_time_s is not None:
        parts.append(f"time ≥ {format_elapsed(config.cutoff_time_s)}")
    if config.cutoff_discharge_ah is not None:
        parts.append(f"discharge ≥ {config.cutoff_discharge_ah:.3f} Ah")
    if config.cutoff_energy_wh is not None:
        parts.append(f"energy ≥ {config.cutoff_energy_wh:.3f} Wh")
    return ", ".join(parts)


def power_hint(form: DischargeForm, resting_v: float | None) -> str:
    rating = f"Rating {POWER_MAX_W:.0f} W."
    if resting_v is None:
        return rating
    where = f"resting {resting_v:.2f} V"
    try:
        if form.mode in ("CC", "CCCV"):
            amps = float(form.current_a)
            watts = resting_v * amps
            text = f"At {where}, {amps:.2f} A is {watts:.0f} W. {rating}"
        elif form.mode == "CP":
            watts = float(form.power_w)
            text = f"Power setpoint {watts:.0f} W. {rating}"
        elif form.mode == "CR":
            ohms = float(form.resistance_ohm)
            watts = (resting_v * resting_v / ohms) if ohms else float("inf")
            text = f"At {where}, {ohms:.3g} ohm is {watts:.0f} W. {rating}"
        else:
            return f"CV holds the setpoint voltage. {rating}"
    except ValueError:
        return rating
    if math.isfinite(watts) and watts > POWER_MAX_W:
        return text + " Above the rating."
    return text


def protection_error(form: DischargeForm, ovp_v: float | None, ocp_a: float | None) -> str | None:
    ovp_limit = LOW_OVP_MAX_V if form.voltage_range == "low" else HIGH_OVP_MAX_V
    ocp_limit = LOW_OCP_MAX_A if form.current_range == "low" else CURRENT_MAX_A
    if ovp_v is not None and not (math.isfinite(ovp_v) and 0 < ovp_v <= ovp_limit):
        return (
            f"Over-voltage protection must be greater than 0 and at most {ovp_limit:.0f} V "
            f"on the {form.voltage_range} voltage range."
        )
    if ocp_a is not None and not (math.isfinite(ocp_a) and 0 < ocp_a <= ocp_limit):
        return (
            f"Over-current protection must be greater than 0 and at most {ocp_limit:.1f} A "
            f"on the {form.current_range} current range."
        )
    return None


def voltage_fraction(current_v: float, start_v: float, cutoff_v: float) -> float | None:
    """Where the present voltage sits between the cutoff and the start voltage.

    0 is the cutoff and 1 is the voltage recorded at the start of the run.
    """
    span = start_v - cutoff_v
    if span <= 0:
        return None
    return min(1.0, max(0.0, (current_v - cutoff_v) / span))


def render_meter(left: str, right: str, readout: str, fraction: float | None, width: int) -> list[str]:
    """Bar from ``left`` to ``right``. The readout sits on the present value."""
    gap = width - len(left) - len(right)
    if gap < 1:
        header = f"{left}  {right}"
    else:
        header = left + (" " * gap) + right
    header = header[:width]
    if fraction is None or width < len(readout) + 2:
        return [header[:width] if width > 0 else header, readout]
    fraction = min(1.0, max(0.0, fraction))
    chars = ["█" if index < int(round(fraction * width)) else "░" for index in range(width)]
    start_col = int(round(fraction * (width - 1))) - len(readout) // 2
    start_col = min(max(0, start_col), width - len(readout))
    for offset, character in enumerate(readout):
        chars[start_col + offset] = character
    return [header, "".join(chars)]


def render_voltage_bar(start_v: float, cutoff_v: float, current_v: float, width: int) -> list[str]:
    """Bar with the cutoff on the left and the starting voltage on the right."""
    return render_meter(
        f"cutoff {cutoff_v:.3f} V",
        f"start {start_v:.3f} V",
        f"{current_v:.3f} V",
        voltage_fraction(current_v, start_v, cutoff_v),
        width,
    )


def progress_fraction(value: float, limit: float | None) -> float | None:
    """How far ``value`` is from 0 toward a cutoff. 1 means the cutoff is reached."""
    if limit is None or not math.isfinite(limit) or limit <= 0 or not math.isfinite(value):
        return None
    return min(1.0, max(0.0, value / limit))


def choose_limits(values: list[float], guide: float | None = None, floor: float | None = None):
    """Return y limits and whether a cutoff guide fits on that scale.

    A cutoff far from the samples is left off the chart. The voltage bar still
    shows it, and stretching the chart down to a distant cutoff would hide the
    sag the chart is there to show.
    """
    if not values:
        return 0.0, 1.0, False
    lo = min(values)
    hi = max(values)
    span = hi - lo
    visible = False
    if guide is not None and math.isfinite(guide):
        reach = max(span, abs(hi) * 0.05, 0.5)
        if (lo - reach) <= guide <= (hi + reach):
            lo = min(lo, guide)
            hi = max(hi, guide)
            visible = True
    if floor is not None:
        lo = min(lo, floor)
    span = hi - lo
    if span == 0:
        pad = max(abs(hi) * 0.05, 0.05)
        bottom = floor if floor is not None and lo <= floor else lo - pad
        return bottom, hi + pad, visible
    pad = max(span * 0.1, abs(hi) * 0.02, 0.05)
    bottom = floor if floor is not None and lo <= floor else lo - pad
    return bottom, hi + pad, visible


def _y_index(value: float, y_min: float, y_max: float, rows: int) -> int:
    if rows <= 1 or y_max == y_min:
        return 0
    fraction = (value - y_min) / (y_max - y_min)
    fraction = min(1.0, max(0.0, fraction))
    return int(round((1.0 - fraction) * (rows - 1)))


def _line(x0: int, y0: int, x1: int, y1: int):
    points = []
    dx = abs(x1 - x0)
    dy = -abs(y1 - y0)
    sx = 1 if x0 < x1 else -1
    sy = 1 if y0 < y1 else -1
    err = dx + dy
    x, y = x0, y0
    while True:
        points.append((x, y))
        if x == x1 and y == y1:
            break
        doubled = 2 * err
        if doubled >= dy:
            err += dy
            x += sx
        if doubled <= dx:
            err += dx
            y += sy
    return points


def _paint_series(pixels, points, y_min, y_max, t_min, t_max, connect: bool) -> None:
    rows = len(pixels)
    cols = len(pixels[0]) if rows else 0
    if not points or cols == 0 or rows == 0:
        return

    def mark(x: int, y: int) -> None:
        if 0 <= x < cols and 0 <= y < rows:
            pixels[y][x] = True

    located = []
    for instant, value in points:
        if t_max <= t_min:
            x = 0
        else:
            x = int(round((instant - t_min) / (t_max - t_min) * (cols - 1)))
        y = _y_index(value, y_min, y_max, rows)
        located.append((x, y))
    if not connect:
        for x, y in located:
            mark(x - (x % 2), y)
        return
    buckets: dict[int, tuple[int, int]] = {}
    for x, y in located:
        current = buckets.get(x)
        buckets[x] = (y, y) if current is None else (min(current[0], y), max(current[1], y))
    centers = [(x, (span[0] + span[1]) // 2) for x, span in sorted(buckets.items())]
    for (x0, y0), (x1, y1) in zip(centers, centers[1:]):
        for x, y in _line(x0, y0, x1, y1):
            mark(x, y)
    for x, (y0, y1) in buckets.items():
        for y in range(y0, y1 + 1):
            mark(x, y)


def _cell(top: str, bottom: str) -> str:
    """One character from the upper and lower half of a plot row."""
    if top == "trace" and bottom == "trace":
        return FULL_BLOCK
    if top == "trace":
        return UPPER_BLOCK
    if bottom == "trace":
        return LOWER_BLOCK
    if "loaded" in (top, bottom):
        return LOADED_MARK
    if "guide" in (top, bottom):
        return GUIDE_MARK
    return " "


def render_plot(
    points,
    width: int,
    height: int,
    y_min: float,
    y_max: float,
    t_min: float = 0.0,
    t_max: float | None = None,
    guide: float | None = None,
    dotted=None,
) -> list[str]:
    """Draw a time series with half-block characters, two levels per row."""
    if width < 1 or height < 1:
        return []
    rows = height * 2
    trace = [[False] * width for _ in range(rows)]
    loaded = [[False] * width for _ in range(rows)]
    guides = [[False] * width for _ in range(rows)]
    end = points[-1][0] if t_max is None and points else 0.0
    if t_max is not None:
        end = t_max
    _paint_series(trace, points, y_min, y_max, t_min, end, connect=True)
    if dotted:
        _paint_series(loaded, dotted, y_min, y_max, t_min, end, connect=False)
    if guide is not None and y_max != y_min and y_min <= guide <= y_max:
        guide_row = _y_index(guide, y_min, y_max, rows)
        for column in range(0, width, 2):
            guides[guide_row][column] = True
    lines = []
    for cell_y in range(height):
        chars = []
        for column in range(width):
            def layer(level: int) -> str:
                if trace[level][column]:
                    return "trace"
                if loaded[level][column]:
                    return "loaded"
                if guides[level][column]:
                    return "guide"
                return ""

            chars.append(_cell(layer(cell_y * 2), layer(cell_y * 2 + 1)))
        lines.append("".join(chars))
    return lines


def series_title(name: str, guide: float | None, visible: bool, extra: str = "") -> str:
    parts = [name]
    if extra:
        parts.append(extra)
    if guide is not None:
        note = f"cutoff {guide:.3f}"
        if not visible:
            note += " off scale"
        parts.append(note)
    return "   ".join(parts)


def render_chart(
    name: str,
    points,
    width: int,
    plot_height: int,
    t_max: float,
    guide: float | None = None,
    floor: float | None = None,
    dotted=None,
    extra: str = "",
) -> list[str]:
    values = [value for _instant, value in points]
    if dotted:
        values.extend(value for _instant, value in dotted)
    y_min, y_max, visible = choose_limits(values, guide, floor)
    gutter = 8
    plot_width = max(4, width - gutter)
    rows = render_plot(
        points,
        plot_width,
        plot_height,
        y_min,
        y_max,
        t_min=0.0,
        t_max=t_max,
        guide=guide if visible else None,
        dotted=dotted,
    )
    lines = [series_title(name, guide, visible, extra)[:width]]
    top = compact(y_max)
    bottom = compact(y_min)
    for index, row in enumerate(rows):
        if index == 0:
            label = top
        elif index == len(rows) - 1:
            label = bottom
        else:
            label = ""
        lines.append(f"{label:>7} {row}")
    start = format_elapsed(0)
    end = format_elapsed(t_max)
    if len(start) + 1 + len(end) <= plot_width:
        axis = start + (" " * (plot_width - len(start) - len(end))) + end
    else:
        axis = end[-plot_width:]
    lines.append(f"{'':8}{axis}")
    return lines


def _voltage_points(samples: list[Sample]):
    return [(sample.elapsed_s, sample.voltage_v) for sample in samples]


def _loaded_points(samples: list[Sample]):
    return [(sample.elapsed_s, sample.loaded_voltage_v) for sample in samples]


def _current_points(samples: list[Sample]):
    chosen = [sample for sample in samples if sample.source != "resting"] or list(samples)
    return [(sample.elapsed_s, sample.current_a) for sample in chosen]


def _charge_points(samples: list[Sample]):
    return [(sample.elapsed_s, sample.capacity_ah) for sample in samples]


def chart_block(view: StatusView, width: int, plot_rows: int) -> list[str]:
    elapsed = view.samples[-1].elapsed_s if view.samples else 0.0
    t_max = max(elapsed, view.elapsed_s or 0.0, 1.0)
    dotted = _loaded_points(view.samples) if view.ocv else None
    lines = render_chart(
        "Voltage (V)",
        _voltage_points(view.samples),
        width,
        plot_rows,
        t_max,
        guide=view.cutoff_voltage,
        dotted=dotted,
        extra="shaded line is loaded" if view.ocv else "",
    )
    lines.append("")
    lines.extend(
        render_chart(
            "Current (A)",
            _current_points(view.samples),
            width,
            plot_rows,
            t_max,
            guide=view.cutoff_current,
        )
    )
    lines.append("")
    lines.extend(
        render_chart(
            "Discharge (Ah)",
            _charge_points(view.samples),
            width,
            plot_rows,
            t_max,
            guide=view.cutoff_ah,
            floor=0.0,
        )
    )
    return lines


def meter_lines(view: StatusView, width: int) -> list[str]:
    """One bar for each cutoff that is turned on."""
    sample = view.samples[-1] if view.samples else None
    lines: list[str] = []
    if sample is not None and view.start_voltage is not None and view.cutoff_voltage is not None:
        lines.extend(
            render_voltage_bar(view.start_voltage, view.cutoff_voltage, sample.voltage_v, width)
        )
    if sample is not None and view.cutoff_current is not None and view.start_current is not None:
        lines.extend(
            render_meter(
                f"cutoff {view.cutoff_current:.3f} A",
                f"start {view.start_current:.3f} A",
                f"{sample.current_a:.3f} A",
                voltage_fraction(sample.current_a, view.start_current, view.cutoff_current),
                width,
            )
        )
    if sample is not None and progress_fraction(sample.capacity_ah, view.cutoff_ah) is not None:
        lines.extend(
            render_meter(
                "0 Ah",
                f"cutoff {view.cutoff_ah:.3f} Ah",
                f"{sample.capacity_ah:.3f} Ah",
                progress_fraction(sample.capacity_ah, view.cutoff_ah),
                width,
            )
        )
    if sample is not None and progress_fraction(sample.energy_wh, view.cutoff_energy_wh) is not None:
        lines.extend(
            render_meter(
                "0 Wh",
                f"cutoff {view.cutoff_energy_wh:.1f} Wh",
                f"{sample.energy_wh:.1f} Wh",
                progress_fraction(sample.energy_wh, view.cutoff_energy_wh),
                width,
            )
        )
    if view.elapsed_s is not None and progress_fraction(view.elapsed_s, view.cutoff_time_s) is not None:
        lines.extend(
            render_meter(
                "0:00:00",
                f"cutoff {format_elapsed(view.cutoff_time_s or 0)}",
                format_elapsed(view.elapsed_s),
                progress_fraction(view.elapsed_s, view.cutoff_time_s),
                width,
            )
        )
    return lines


def readout_lines(view: StatusView, width: int) -> list[str]:
    sample = view.samples[-1] if view.samples else None
    elapsed = "—" if view.elapsed_s is None else format_elapsed(view.elapsed_s)
    cells = (
        ("VOLTAGE V", "—" if sample is None else f"{sample.voltage_v:.3f}"),
        ("CURRENT A", "—" if sample is None else f"{sample.current_a:.3f}"),
        ("DISCHARGE Ah", "—" if sample is None else f"{sample.capacity_ah:.3f}"),
        ("ENERGY Wh", "—" if sample is None else f"{sample.energy_wh:.1f}"),
        ("ELAPSED", elapsed),
    )
    if width < 68:
        return [f"{name:<14}{value}"[:width] for name, value in cells]
    column = max(12, width // 5)
    labels = "".join(f"{name:<{column}}"[:column] for name, _value in cells)
    values = "".join(f"{value:<{column}}"[:column] for _name, value in cells)
    return [labels.rstrip(), values.rstrip()]


def header_lines(view: StatusView, width: int) -> list[str]:
    phase = view.phase.upper()
    left = "ET54 discharge"
    if view.identity:
        left = f"{left}  {view.identity}"
    gap = width - len(left) - len(phase)
    if gap < 1:
        keep = max(0, width - len(phase) - 1)
        line = f"{left[:keep]} {phase}"
    else:
        line = left + (" " * gap) + phase
    detail = view.mode_text
    if view.ranges:
        detail = f"{detail}  ·  {view.ranges}"
    if view.samples:
        sample = view.samples[-1]
        if sample.source == "open_circuit":
            detail += "  ·  open circuit"
        elif sample.load_on:
            detail += "  ·  load on"
        else:
            detail += "  ·  load off"
    return [line[:width], detail[:width]]


def footer_lines(view: StatusView, width: int) -> list[str]:
    status = view.status
    if view.error and status and not status.startswith("ERROR"):
        status = f"ERROR {status}"
    wrapped = textwrap.wrap(status, width) or [""]
    lines = wrapped[:2]
    if view.csv_note and view.csv_note not in view.status:
        lines.append(view.csv_note[:width])
    if view.phase in {"finished", "stopped", "error"}:
        lines.append("Press any key to close")
    elif view.phase == "stopping":
        lines.append("Waiting for the load to turn off")
    else:
        lines.append("Press q to stop")
    return lines


def render_status(view: StatusView, width: int, height: int) -> str:
    """Fit the status page to the terminal, dropping the charts before the bar."""
    width = max(20, width)
    height = max(8, height)
    footer = footer_lines(view, width)
    budget = max(1, height - len(footer))
    body: list[str] = []

    def add(block: list[str]) -> bool:
        if len(body) + len(block) > budget:
            return False
        body.extend(block)
        return True

    add(header_lines(view, width))
    add([""])
    add(readout_lines(view, width))
    meters = meter_lines(view, width)
    if meters:
        add([""])
        add(meters)
    remaining = budget - len(body)
    if view.samples and remaining >= 12:
        gaps = 2 if remaining >= 18 else 0
        plot_rows = max(2, min(6, (remaining - gaps) // 3 - 2))
        while plot_rows >= 2:
            block = chart_block(view, width, plot_rows)
            if gaps == 0:
                block = [line for line in block if line != ""]
            else:
                block = [""] + block
            if add(block):
                break
            plot_rows -= 1
    elif not view.samples:
        add([""])
        add(["Waiting for the first sample."])
    if len(body) < budget:
        body.extend([""] * (budget - len(body)))
    body = body[:budget]
    return "\n".join(body + footer)


def _used_fields(form: DischargeForm) -> set[str]:
    used = {"resource", "mode", "voltage_range", "current_range", "interval", "limits", "sync"}
    if form.csv_enabled:
        used.add("csv_path")
    used.add("csv_enabled")
    if form.ocv_enabled:
        used.add("ocv_wait")
    used.add("ocv")
    used.update({"cutoff_voltage", "cutoff_current", "cutoff_time", "cutoff_ah", "cutoff_wh"})
    if form.mode in ("CC", "CCCV"):
        used.add("current")
    if form.mode in ("CV", "CCCV"):
        used.add("voltage")
    if form.mode == "CR":
        used.add("resistance")
    if form.mode == "CP":
        used.add("power")
    return used


def _field_value(form: DischargeForm, connection: Connection, kind: str) -> str:
    if kind == "resource":
        return form.resource or "(not set)"
    if kind == "mode":
        return MODE_LABELS.get(form.mode, form.mode)
    if kind == "current":
        return f"{form.current_a} A"
    if kind == "voltage":
        return f"{form.voltage_set_v} V"
    if kind == "resistance":
        return f"{form.resistance_ohm} Ω"
    if kind == "power":
        return f"{form.power_w} W"
    if kind == "voltage_range":
        return form.voltage_range
    if kind == "current_range":
        return form.current_range
    if kind == "interval":
        return f"{form.sample_interval_s} s"
    if kind == "csv_enabled":
        return "on" if form.csv_enabled else "off"
    if kind == "csv_path":
        return form.csv_path or "(not set)"
    if kind == "ocv":
        return "on" if form.ocv_enabled else "off"
    if kind == "ocv_wait":
        return f"{form.ocv_wait_s} s"
    if kind == "cutoff_voltage":
        return f"≤ {form.cutoff_voltage_v} V" if form.cutoff_voltage_on else "off"
    if kind == "cutoff_current":
        return f"≤ {form.cutoff_current_a} A" if form.cutoff_current_on else "off"
    if kind == "cutoff_time":
        return f"≥ {form.cutoff_time_s} s" if form.cutoff_time_on else "off"
    if kind == "cutoff_ah":
        return f"≥ {form.cutoff_ah} Ah" if form.cutoff_ah_on else "off"
    if kind == "cutoff_wh":
        return f"≥ {form.cutoff_wh} Wh" if form.cutoff_wh_on else "off"
    if kind == "sync":
        return "on" if form.sync_voltage_gate else "off"
    if connection.limits is None:
        return "connect to read the load"
    _on, _off, ovp, ocp = connection.limits
    return f"OVP {ovp:.3f} V, OCP {ocp:.3f} A"


def render_setup(form: DischargeForm, connection: Connection, notice: str, width: int = 88) -> str:
    width = max(40, width)
    lines = ["ET54 discharge", ""]
    if connection.load is None:
        lines.append("Link                  disconnected")
    else:
        resting = ""
        if connection.resting_v is not None:
            resting = f"  resting {connection.resting_v:.3f} V"
        lines.append(f"Link                  connected  {connection.identity}{resting}")
        if connection.limits is not None:
            on_v, off_v, ovp, ocp = connection.limits
            lines.append(
                f"                      start {on_v:.3f} V  off {off_v:.3f} V  "
                f"OVP {ovp:.3f} V  OCP {ocp:.3f} A"
            )
    lines.append(power_hint(form, connection.resting_v))
    lines.append("")
    used = _used_fields(form)
    rows = [(number, label, kind, _field_value(form, connection, kind)) for number, label, kind in FIELDS]
    value_width = max(len(value) for _number, _label, _kind, value in rows)
    for number, label, kind, value in rows:
        note = "" if kind in used else "Not In Use"
        spacer = "  " if note else ""
        lines.append(f"{number:3d}  {label:<22}{value:<{value_width}}{spacer}{note}")
    lines.append("")
    if notice:
        lines.extend(textwrap.wrap(notice, width) or [""])
        lines.append("")
    lines.append(HELP)
    return "\n".join(lines)


def format_sample_line(sample: Sample) -> str:
    return (
        f"{format_elapsed(sample.elapsed_s)}  "
        f"{sample.voltage_v:8.3f} V  "
        f"{sample.current_a:7.3f} A  "
        f"{sample.capacity_ah:8.3f} Ah  "
        f"{sample.energy_wh:8.1f} Wh"
    )


def _override_error(args) -> str | None:
    pairs = (
        (args.ocv, args.no_ocv, "--ocv", "--no-ocv"),
        (args.sync_voltage_gate, args.no_sync_voltage_gate, "--sync-voltage-gate", "--no-sync-voltage-gate"),
        (args.csv_path is not None, args.no_csv, "--csv", "--no-csv"),
        (args.cutoff_voltage is not None, args.no_cutoff_voltage, "--cutoff-voltage", "--no-cutoff-voltage"),
        (args.cutoff_current is not None, args.no_cutoff_current, "--cutoff-current", "--no-cutoff-current"),
        (args.cutoff_time is not None, args.no_cutoff_time, "--cutoff-time", "--no-cutoff-time"),
        (args.cutoff_ah is not None, args.no_cutoff_ah, "--cutoff-ah", "--no-cutoff-ah"),
        (args.cutoff_wh is not None, args.no_cutoff_wh, "--cutoff-wh", "--no-cutoff-wh"),
    )
    for left, right, left_name, right_name in pairs:
        if left and right:
            return f"Choose either {left_name} or {right_name}."
    if args.ocv_wait is not None and args.ocv_wait not in OCV_WAITS_S:
        choices = ", ".join(f"{wait:g}" for wait in OCV_WAITS_S)
        return f"Open-circuit settle time must be one of {choices} s."
    if args.resource is not None:
        problem = resource_problem(args.resource)
        if problem:
            return problem
    if args.csv_path is not None:
        problem = csv_path_problem(args.csv_path)
        if problem:
            return problem
    return None


def apply_overrides(form: DischargeForm, args) -> str | None:
    """Copy command-line options onto the form. Saved values stay where no option was given."""
    error = _override_error(args)
    if error:
        return error
    if args.resource is not None:
        form.resource = args.resource.strip()
    if args.mode is not None:
        form.mode = args.mode
    if args.current is not None:
        form.current_a = format_number(args.current)
    if args.cv_voltage is not None:
        form.voltage_set_v = format_number(args.cv_voltage)
    if args.resistance is not None:
        form.resistance_ohm = format_number(args.resistance)
    if args.power is not None:
        form.power_w = format_number(args.power)
    if args.interval is not None:
        form.sample_interval_s = format_number(args.interval)
    if args.voltage_range is not None:
        form.voltage_range = args.voltage_range
    if args.current_range is not None:
        form.current_range = args.current_range
    if args.no_csv:
        form.csv_enabled = False
    if args.csv_path is not None:
        form.csv_path = args.csv_path
        form.csv_enabled = True
    if args.ocv:
        form.ocv_enabled = True
    if args.no_ocv:
        form.ocv_enabled = False
    if args.ocv_wait is not None:
        form.ocv_wait_s = format_number(args.ocv_wait)
    if args.sync_voltage_gate:
        form.sync_voltage_gate = True
    if args.no_sync_voltage_gate:
        form.sync_voltage_gate = False
    cutoff_flags = (
        ("cutoff_voltage", "no_cutoff_voltage", "cutoff_voltage_on", "cutoff_voltage_v"),
        ("cutoff_current", "no_cutoff_current", "cutoff_current_on", "cutoff_current_a"),
        ("cutoff_time", "no_cutoff_time", "cutoff_time_on", "cutoff_time_s"),
        ("cutoff_ah", "no_cutoff_ah", "cutoff_ah_on", "cutoff_ah"),
        ("cutoff_wh", "no_cutoff_wh", "cutoff_wh_on", "cutoff_wh"),
    )
    for value_name, disable_name, on_attr, text_attr in cutoff_flags:
        if getattr(args, disable_name):
            setattr(form, on_attr, False)
        value = getattr(args, value_name)
        if value is not None:
            setattr(form, on_attr, True)
            setattr(form, text_attr, format_number(value))
    return None


def overrides_used(args) -> bool:
    names = (
        "resource",
        "mode",
        "current",
        "cv_voltage",
        "resistance",
        "power",
        "interval",
        "voltage_range",
        "current_range",
        "csv_path",
        "no_csv",
        "ocv",
        "no_ocv",
        "ocv_wait",
        "sync_voltage_gate",
        "no_sync_voltage_gate",
        "cutoff_voltage",
        "no_cutoff_voltage",
        "cutoff_current",
        "no_cutoff_current",
        "cutoff_time",
        "no_cutoff_time",
        "cutoff_ah",
        "no_cutoff_ah",
        "cutoff_wh",
        "no_cutoff_wh",
    )
    for name in names:
        value = getattr(args, name)
        if value not in (None, False):
            return True
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="et54xx_cli.py",
        description=(
            "Configure an ET54 discharge from the terminal and run it. "
            "Settings are saved in settings.json beside this program and shared with the windowed app. "
            "With no arguments, open the setup menu."
        ),
        epilog=(
            "examples:\n"
            "  python et54xx_cli.py\n"
            "  python et54xx_cli.py --start\n"
            "  python et54xx_cli.py --mode CC --current 2 --cutoff-voltage 40 --csv pack.csv --start\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--resource", help="PyVISA resource, such as ASRL/dev/ttyUSB0::INSTR")
    parser.add_argument("--mode", choices=("CC", "CV", "CCCV", "CR", "CP"), help="discharge mode")
    parser.add_argument("--current", type=float, help="constant-current setpoint in amps")
    parser.add_argument("--cv-voltage", type=float, help="constant-voltage setpoint in volts")
    parser.add_argument("--resistance", type=float, help="constant-resistance setpoint in ohms")
    parser.add_argument("--power", type=float, help="constant-power setpoint in watts")
    parser.add_argument("--interval", type=float, help=f"sample interval in seconds, at least {MIN_SAMPLE_S:.0f}")
    parser.add_argument("--voltage-range", choices=("low", "high"))
    parser.add_argument("--current-range", choices=("low", "high"))
    parser.add_argument("--csv", dest="csv_path", help="write samples to this CSV file")
    parser.add_argument("--no-csv", action="store_true", help="run without a CSV log")
    parser.add_argument("--ocv", action="store_true", help="check open-circuit voltage each sample")
    parser.add_argument("--no-ocv", action="store_true", help="use the loaded voltage for the voltage cutoff")
    parser.add_argument("--ocv-wait", type=float, help="open-circuit settle time: 0.25, 0.5, 1, or 2 seconds")
    parser.add_argument("--sync-voltage-gate", action="store_true", help="copy the voltage cutoff into the load")
    parser.add_argument("--no-sync-voltage-gate", action="store_true")
    parser.add_argument("--cutoff-voltage", type=float, help="stop at or below this voltage")
    parser.add_argument("--no-cutoff-voltage", action="store_true")
    parser.add_argument("--cutoff-current", type=float, help="stop at or below this current")
    parser.add_argument("--no-cutoff-current", action="store_true")
    parser.add_argument("--cutoff-time", type=float, help="stop at or after this many seconds")
    parser.add_argument("--no-cutoff-time", action="store_true")
    parser.add_argument("--cutoff-ah", type=float, help="stop at or after this many amp-hours")
    parser.add_argument("--no-cutoff-ah", action="store_true")
    parser.add_argument("--cutoff-wh", type=float, help="stop at or after this many watt-hours")
    parser.add_argument("--no-cutoff-wh", action="store_true")
    parser.add_argument("--list-ports", action="store_true", help="print serial resources and exit")
    parser.add_argument("--start", action="store_true", help="start the discharge without the menu")
    parser.add_argument("--yes", action="store_true", help="overwrite an existing CSV without asking")
    return parser


class KeyReader:
    """Read one key during the status page without waiting for Enter."""

    def __init__(self) -> None:
        self._fd = None
        self._previous = None

    def __enter__(self):
        if not sys.stdin.isatty():
            return self
        try:
            import termios
            import tty
        except ImportError:
            return self
        self._fd = sys.stdin.fileno()
        self._previous = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._previous is not None and self._fd is not None:
            import termios

            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._previous)
        return None

    def poll(self, timeout: float) -> str | None:
        if self._previous is not None:
            import select

            ready, _ignored, _errors = select.select([sys.stdin], [], [], timeout)
            if not ready:
                return None
            return sys.stdin.read(1)
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if msvcrt.kbhit():
                    return msvcrt.getwch()
                time.sleep(0.05)
            return None
        time.sleep(timeout)
        return None


def _lone_escape(keys) -> bool:
    """True when Escape was pressed by itself, not as the start of an arrow key."""
    if keys.poll(0.03) is None:
        return True
    keys.poll(0.03)
    return False


def _visible_len(text: str) -> int:
    total = 0
    index = 0
    while index < len(text):
        if text.startswith("\x1b[", index):
            end = text.find("m", index)
            if end == -1:
                break
            index = end + 1
            continue
        total += 1
        index += 1
    return total


def write_frame(frame: str, width: int, height: int) -> None:
    lines = frame.split("\n")
    if len(lines) < height:
        lines.extend([""] * (height - len(lines)))
    lines = lines[:height]
    padded = []
    for line in lines:
        extra = width - _visible_len(line)
        if extra > 0:
            line = line + (" " * extra)
        padded.append(line)
    sys.stdout.write("\x1b[H" + "\n".join(padded))
    sys.stdout.flush()


def _emphasize(frame: str, error: bool) -> str:
    lines = frame.split("\n")
    colored = []
    bold_next = False
    for line in lines:
        if bold_next:
            colored.append(f"\x1b[1m{line}\x1b[0m")
            bold_next = False
            continue
        if "VOLTAGE" in line and "ELAPSED" in line:
            colored.append(f"\x1b[2m{line}\x1b[0m")
            bold_next = True
            continue
        if error and line.strip().startswith("ERROR "):
            colored.append(f"\x1b[31m{line}\x1b[0m")
            continue
        colored.append(line)
    return "\n".join(colored)


def terminal_size() -> tuple[int, int]:
    size = os.get_terminal_size(sys.stdout.fileno()) if sys.stdout.isatty() else os.terminal_size((80, 24))
    width = min(120, max(40, size.columns))
    height = max(12, size.lines)
    return width, height


class CliApp:
    def __init__(self, settings_path: Path | None = None):
        self.settings_path = (
            Path(settings_path) if settings_path is not None else Path(__file__).resolve().with_name("settings.json")
        )
        self.form = form_from_settings(load_settings(self.settings_path))
        self.connection = Connection()
        self.notice = ""
        self.notice_error = False
        self.ranges_changed = False

    def save(self) -> None:
        try:
            save_settings(self.settings_path, **self.form.to_updates())
        except OSError as exc:
            self.notice = f"Could not save settings: {exc.strerror or exc}"
            self.notice_error = True

    def note(self, text: str, error: bool = False) -> None:
        self.notice = text
        self.notice_error = error

    def menu(self) -> int:
        while True:
            self._draw_setup()
            try:
                entered = self._read_menu_command()
            except EOFError:
                print()
                return 0
            except KeyboardInterrupt:
                print()
                return 130
            action, field = classify_command(entered)
            if action == "quit":
                return 0
            if action == "help" or action == "unknown":
                if action == "unknown" and entered.strip():
                    self.note(f"Unknown command {entered.strip()!r}. {HELP}", error=True)
                else:
                    self.note(HELP)
                continue
            if action == "edit":
                self._edit(field or 0)
                continue
            if action == "ports":
                self._choose_port()
                continue
            if action == "connect":
                self._connect()
                continue
            if action == "disconnect":
                self._disconnect()
                continue
            if action == "apply":
                self._apply_ranges()
                continue
            if action == "start":
                self.run_discharge(confirm=True, assume_yes=False)

    def run_discharge(self, confirm: bool, assume_yes: bool) -> int:
        resource = self.form.resource.strip()
        if not resource or resource == RESOURCE_PROMPT:
            self.note("Enter a resource path first.", error=True)
            print(self.notice, file=sys.stderr)
            return 2
        config, error = build_config(self.form, program_ranges=self.ranges_changed)
        if error or config is None:
            self.note(error or "Check the test settings.", error=True)
            print(self.notice, file=sys.stderr)
            return 2
        if config.csv_enabled:
            path = Path(config.csv_path).expanduser()
            if path.exists() and path.is_file():
                if assume_yes:
                    pass
                elif not sys.stdin.isatty():
                    self.note(f"{path} already exists. Pass --yes to overwrite it.", error=True)
                    print(self.notice, file=sys.stderr)
                    return 2
                elif not self._confirm(f"{path} already exists. Overwrite it? [y/N]"):
                    return 0
        if confirm:
            if not sys.stdin.isatty():
                self.note("Pass --start to run without the menu.", error=True)
                print(self.notice, file=sys.stderr)
                return 2
            log = f"Logging to {config.csv_path}." if config.csv_enabled else "CSV logging is off."
            prompt = (
                f"Start {mode_summary(config)} on {resource}, stop at {cutoff_phrase(config)}? "
                f"{log} The load will turn on. [y/N]"
            )
            if not self._confirm(prompt):
                return 0
        self.save()
        if self.notice_error and self.notice.startswith("Could not save settings"):
            print(self.notice, file=sys.stderr)
            return 2
        state = RunState()
        stop = threading.Event()
        worker = threading.Thread(target=self._worker, args=(state, stop, config, resource), daemon=False)
        worker.start()
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        try:
            if interactive:
                self._dashboard(worker, state, stop, config)
            else:
                self._stream(worker, state, stop)
        finally:
            stop.set()
            worker.join(timeout=15)
        self._refresh_after_run()
        code, message = self._outcome(state)
        self.note(message, error=code != 0)
        if not interactive or not confirm:
            print(message)
        return code

    def print_ports(self) -> int:
        try:
            names = list_visa_resources()
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not names:
            print("No instruments found.")
            return 0
        for name in names:
            print(name)
        return 0

    def _outcome(self, state: RunState) -> tuple[int, str]:
        snap = state.snapshot()
        if snap["failed"]:
            return 1, snap["failed"]
        result = snap["result"]
        if result is None:
            return 1, snap["status"] or "The discharge stopped."
        if result.reason in ERROR_REASONS:
            return 1, result.message
        return 0, result.message

    def _worker(self, state: RunState, stop: threading.Event, config: TestConfig, resource: str) -> None:
        try:
            load = self.connection.load
            if load is None or self.connection.resource != resource:
                state.set_status(f"Connecting to {resource}")
                if load is not None:
                    self._close_load(load)
                load = ET54Load(resource)
                self.connection.load = load
                self.connection.resource = resource
                self.connection.identity = load.identity
                state.set_identity(load.identity)
            else:
                state.set_identity(self.connection.identity)
            config = self._config_for_load(load, config)
            test = CapacityTest(
                load,
                config,
                stop,
                on_sample=state.add_sample,
                on_status=state.set_status,
            )
            result = test.run()
            if test.off_error is not None:
                state.finish(None, f"The load may still be on: {test.off_error}")
            else:
                state.finish(result, None)
        except Exception as exc:
            state.finish(None, str(exc) or exc.__class__.__name__)
        finally:
            if not state.done.is_set():
                state.finish(None, "The discharge stopped.")

    def _dashboard(self, worker: threading.Thread, state: RunState, stop: threading.Event, config: TestConfig) -> None:
        close = {"now": False}

        def on_signal(_signum, _frame) -> None:
            if state.done.is_set():
                close["now"] = True
            else:
                stop.set()
                state.set_status("Stopping. Turning the load off.")

        previous = signal.signal(signal.SIGINT, on_signal)
        color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        try:
            with KeyReader() as keys:
                sys.stdout.write("\x1b[?1049h\x1b[?25l")
                sys.stdout.flush()
                try:
                    next_draw = 0.0
                    showed_final = False
                    while True:
                        now = time.monotonic()
                        finished = state.done.is_set()
                        if now >= next_draw or (finished and not showed_final):
                            width, height = terminal_size()
                            view = self._view(state, config, stop.is_set())
                            frame = render_status(view, width, height)
                            if color:
                                frame = _emphasize(frame, view.error)
                            write_frame(frame, width, height)
                            next_draw = time.monotonic() + STATUS_REDRAW_S
                            showed_final = finished
                        try:
                            key = keys.poll(max(0.0, next_draw - time.monotonic()))
                        except InterruptedError:
                            key = None
                        if not worker.is_alive() and not state.done.is_set():
                            state.finish(None, "The discharge stopped.")
                        if close["now"] and state.done.is_set():
                            break
                        if state.done.is_set() and key:
                            break
                        if key in {"q", "Q"}:
                            stop.set()
                            state.set_status("Stopping. Turning the load off.")
                            next_draw = 0.0
                finally:
                    sys.stdout.write("\x1b[?25h\x1b[?1049l")
                    sys.stdout.flush()
        finally:
            signal.signal(signal.SIGINT, previous)

    def _stream(self, worker: threading.Thread, state: RunState, stop: threading.Event) -> None:
        seen = 0
        last_status = None
        try:
            while True:
                snap = state.snapshot()
                if snap["status"] != last_status and snap["status"]:
                    print(snap["status"], flush=True)
                    last_status = snap["status"]
                while seen < len(snap["samples"]):
                    print(format_sample_line(snap["samples"][seen]), flush=True)
                    seen += 1
                if snap["done"]:
                    break
                time.sleep(0.2)
        except KeyboardInterrupt:
            stop.set()
            print("Stopping. Turning the load off.", flush=True)
        worker.join(timeout=15)
        snap = state.snapshot()
        if snap["status"] and snap["status"] != last_status:
            print(snap["status"], flush=True)
        while seen < len(snap["samples"]):
            print(format_sample_line(snap["samples"][seen]), flush=True)
            seen += 1

    def _view(self, state: RunState, config: TestConfig, stopping: bool) -> StatusView:
        snap = state.snapshot()
        samples = snap["samples"]
        if not samples:
            elapsed = None
        elif snap["done"]:
            elapsed = samples[-1].elapsed_s
        else:
            elapsed = snap["anchor_elapsed"] + max(0.0, time.monotonic() - snap["anchor_wall"])
        if snap["failed"]:
            phase = "error"
        elif snap["done"]:
            result = snap["result"]
            if result is not None and result.reason in ERROR_REASONS:
                phase = "error"
            elif result is not None and result.reason == "stopped":
                phase = "stopped"
            else:
                phase = "finished"
        elif stopping:
            phase = "stopping"
        elif not samples:
            phase = "starting"
        else:
            phase = "running"
        csv_note = f"Logging to {config.csv_path}" if config.csv_enabled else "CSV logging is off."
        ranges = f"{config.voltage_range} voltage, {config.current_range} current"
        return StatusView(
            mode_text=mode_summary(config),
            identity=snap["identity"],
            ranges=ranges,
            phase=phase,
            elapsed_s=elapsed,
            samples=samples,
            cutoff_voltage=config.cutoff_voltage_v,
            cutoff_current=config.cutoff_current_a,
            cutoff_ah=config.cutoff_discharge_ah,
            start_voltage=snap["start_voltage"],
            cutoff_time_s=config.cutoff_time_s,
            cutoff_energy_wh=config.cutoff_energy_wh,
            start_current=snap["start_current"],
            ocv=config.ocv_enabled,
            status=snap["status"],
            error=phase == "error",
            csv_note=csv_note,
        )

    def _read_menu_command(self) -> str:
        """Read a menu command, and keep the measured voltage current while waiting."""
        if not sys.stdin.isatty():
            return input("> ")
        typed = ""
        sys.stdout.write("> ")
        sys.stdout.flush()
        next_voltage = time.monotonic() + MENU_VOLTAGE_S
        with KeyReader() as keys:
            while True:
                if self.connection.load is None:
                    timeout = 30.0
                else:
                    timeout = max(0.0, next_voltage - time.monotonic())
                key = keys.poll(timeout)
                if key is None:
                    if self.connection.load is not None and self._refresh_menu_voltage():
                        self._draw_setup()
                        sys.stdout.write("> " + typed)
                        sys.stdout.flush()
                    next_voltage = time.monotonic() + MENU_VOLTAGE_S
                    continue
                if key == "\x1b":
                    if _lone_escape(keys) and typed:
                        typed = ""
                        self._draw_setup()
                        sys.stdout.write("> ")
                        sys.stdout.flush()
                    continue
                updated, finished = apply_menu_key(typed, key)
                if finished is not None:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return finished
                if len(updated) < len(typed):
                    sys.stdout.write("\b \b")
                elif len(updated) > len(typed):
                    sys.stdout.write(updated[len(typed) :])
                typed = updated
                sys.stdout.flush()

    def _read_line(self, prompt: str) -> str | None:
        """Read one line. Escape returns None and does not include the key."""
        if not prompt.endswith(" "):
            prompt += " "
        if not sys.stdin.isatty():
            try:
                return input(prompt)
            except EOFError:
                return None
        typed = ""
        sys.stdout.write(prompt)
        sys.stdout.flush()
        with KeyReader() as keys:
            while True:
                key = keys.poll(30.0)
                if key is None:
                    continue
                if key == "\x1b":
                    if _lone_escape(keys):
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        return None
                    continue
                updated, finished = apply_menu_key(typed, key)
                if finished is not None:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return finished
                if len(updated) < len(typed):
                    sys.stdout.write("\b \b")
                elif len(updated) > len(typed):
                    sys.stdout.write(updated[len(typed) :])
                typed = updated
                sys.stdout.flush()

    def _ask(self, prompt: str) -> str | None:
        """Return cleaned text. None means cancelled or rejected, with a note set."""
        raw = self._read_line(prompt if "Esc cancels" in prompt else f"{prompt.rstrip()}  Esc cancels: ")
        if raw is None:
            self.note("Edit cancelled.")
            return None
        problem = entry_problem(raw)
        if problem:
            self.note(problem, error=True)
            return None
        return raw.strip()

    def _confirm(self, prompt: str) -> bool:
        answer = self._ask(prompt)
        if answer is None:
            if self.notice == "Edit cancelled.":
                self.note("Cancelled.")
            return False
        if answer.lower() in {"y", "yes"}:
            return True
        self.note("Cancelled.")
        return False

    def _refresh_menu_voltage(self) -> bool:
        """Read the load. Return whether the menu should be drawn again."""
        load = self.connection.load
        if load is None:
            return False
        try:
            voltage = float(load.read_voltage())
        except Exception as exc:
            self._drop_connection(load)
            self.note(f"Connection lost: {exc}", error=True)
            return True
        previous = None if self.connection.resting_v is None else f"{self.connection.resting_v:.3f}"
        self.connection.resting_v = voltage
        return f"{voltage:.3f}" != previous

    def _drop_connection(self, load) -> None:
        self.connection = Connection()
        try:
            load.close()
        except Exception:
            pass

    def _draw_setup(self) -> None:
        width = terminal_size()[0] if sys.stdout.isatty() else 88
        text = render_setup(self.form, self.connection, self.notice, width)
        if sys.stdout.isatty():
            sys.stdout.write("\x1b[H\x1b[2J")
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()

    def _edit(self, number: int) -> None:
        kind = FIELD_BY_NUMBER.get(number)
        if kind is None:
            self.note(f"No setting {number}. {HELP}", error=True)
            return
        try:
            getattr(self, f"_edit_{kind}")()
        except EOFError:
            self.note("Edit cancelled.")
        except KeyboardInterrupt:
            print()
            self.note("Edit cancelled.")

    def _edit_resource(self) -> None:
        current = self.form.resource or "(not set)"
        entered = self._ask(f"Resource [{current}] (blank keeps, - clears):")
        if entered is None:
            return
        if entered == "":
            self.note("Resource unchanged.")
            return
        if entered in {"-", "none", "clear"}:
            self.form.resource = ""
        else:
            problem = resource_problem(entered)
            if problem:
                self.note(problem, error=True)
                return
            self.form.resource = entered
        self.save()
        self.note("Resource updated." if self.form.resource else "Resource cleared.")

    def _edit_mode(self) -> None:
        entered = self._ask(f"Mode [{self.form.mode}] (CC, CV, CCCV, CR, CP):")
        if entered is None:
            return
        if entered == "":
            self.note("Mode unchanged.")
            return
        mode = parse_mode(entered)
        if mode is None:
            self.note("Mode must be CC, CV, CC+CV, CR, or CP.", error=True)
            return
        self.form.mode = mode
        self.save()
        self.note(f"Mode set to {MODE_LABELS[mode]}.")

    def _edit_setpoint(self, attr: str, label: str, unit: str, low: float | None, high: float | None) -> None:
        current = getattr(self.form, attr)
        entered = self._ask(f"{label} ({unit}) [{current}]:")
        if entered is None:
            return
        if entered == "":
            self.note(f"{label} unchanged.")
            return
        value, error = parse_bounded(entered, label, low, high)
        if error or value is None:
            self.note(error or f"{label} must be a number.", error=True)
            return
        setattr(self.form, attr, format_number(value))
        self.save()
        self.note(f"{label} set to {format_number(value)} {unit}.")

    def _edit_current(self) -> None:
        self._edit_setpoint("current_a", "Current", "A", None, None)

    def _edit_voltage(self) -> None:
        self._edit_setpoint("voltage_set_v", "CV voltage", "V", None, None)

    def _edit_resistance(self) -> None:
        self._edit_setpoint("resistance_ohm", "Resistance", "ohm", 0.05, 4500)

    def _edit_power(self) -> None:
        self._edit_setpoint("power_w", "Power", "W", None, POWER_MAX_W)

    def _edit_range(self, attr: str, label: str) -> None:
        current = getattr(self.form, attr)
        entered = self._ask(f"{label} [{current}] (low or high):")
        if entered is None:
            return
        if entered == "":
            self.note(f"{label} unchanged.")
            return
        choice = parse_range_choice(entered)
        if choice is None:
            self.note(f"{label} must be low or high.", error=True)
            return
        if choice == current:
            self.note(f"{label} unchanged.")
            return
        setattr(self.form, attr, choice)
        self.ranges_changed = True
        self.save()
        self.note(f"{label} set to {choice}. The load changes when you apply ranges or start.")

    def _edit_voltage_range(self) -> None:
        self._edit_range("voltage_range", "Voltage range")

    def _edit_current_range(self) -> None:
        self._edit_range("current_range", "Current range")

    def _edit_interval(self) -> None:
        entered = self._ask(f"Sample interval (s) [{self.form.sample_interval_s}]:")
        if entered is None:
            return
        if entered == "":
            self.note("Sample interval unchanged.")
            return
        value, error = parse_bounded(entered, "Sample interval", MIN_SAMPLE_S, None)
        if error or value is None:
            self.note(error or "Sample interval must be a number.", error=True)
            return
        self.form.sample_interval_s = format_number(value)
        self.save()
        self.note(f"Sample interval set to {format_number(value)} s.")

    def _edit_csv_enabled(self) -> None:
        self.form.csv_enabled = not self.form.csv_enabled
        self.save()
        self.note("CSV logging on." if self.form.csv_enabled else "CSV logging off.")

    def _edit_csv_path(self) -> None:
        entered = self._ask(f"CSV file [{self.form.csv_path}]:")
        if entered is None:
            return
        if entered == "":
            self.note("CSV file unchanged.")
            return
        problem = csv_path_problem(entered)
        if problem:
            self.note(problem, error=True)
            return
        self.form.csv_path = entered
        self.form.csv_enabled = True
        self.save()
        self.note(f"CSV file set to {entered}.")

    def _edit_ocv(self) -> None:
        self.form.ocv_enabled = not self.form.ocv_enabled
        self.save()
        if self.form.ocv_enabled:
            self.note("Open-circuit checks on. The voltage cutoff uses the rested reading.")
        else:
            self.note("Open-circuit checks off. The voltage cutoff uses the loaded reading.")

    def _edit_ocv_wait(self) -> None:
        choices = ", ".join(f"{wait:g}" for wait in OCV_WAITS_S)
        entered = self._ask(f"Settle time (s) [{self.form.ocv_wait_s}] ({choices}):")
        if entered is None:
            return
        if entered == "":
            self.note("Settle time unchanged.")
            return
        value = parse_ocv_wait(entered)
        if value is None:
            self.note(f"Open-circuit settle time must be one of {choices} s.", error=True)
            return
        self.form.ocv_wait_s = format_number(value)
        self.save()
        self.note(f"Settle time set to {format_number(value)} s.")

    def _edit_cutoff(self, on_attr: str, text_attr: str, label: str, unit: str, push: bool) -> None:
        enabled = getattr(self.form, on_attr)
        text = getattr(self.form, text_attr)
        shown = text if enabled else "off"
        entered = self._ask(f"{label} ({unit}) [{shown}] (blank keeps, off disables):")
        if entered is None:
            return
        new_on, new_text, error = parse_optional_limit(entered, enabled, text, label)
        if error:
            self.note(error, error=True)
            return
        if new_on == enabled and new_text == text:
            self.note(f"{label} unchanged.")
            return
        setattr(self.form, on_attr, new_on)
        setattr(self.form, text_attr, new_text)
        self.save()
        if new_on:
            self.note(f"{label} set to {new_text} {unit}.")
        else:
            self.note(f"{label} off.")
        if push and self.form.sync_voltage_gate:
            self._push_gate()

    def _edit_cutoff_voltage(self) -> None:
        self._edit_cutoff("cutoff_voltage_on", "cutoff_voltage_v", "Voltage cutoff", "V", True)

    def _edit_cutoff_current(self) -> None:
        self._edit_cutoff("cutoff_current_on", "cutoff_current_a", "Current cutoff", "A", False)

    def _edit_cutoff_time(self) -> None:
        self._edit_cutoff("cutoff_time_on", "cutoff_time_s", "Time cutoff", "s", False)

    def _edit_cutoff_ah(self) -> None:
        self._edit_cutoff("cutoff_ah_on", "cutoff_ah", "Discharge cutoff", "Ah", False)

    def _edit_cutoff_wh(self) -> None:
        self._edit_cutoff("cutoff_wh_on", "cutoff_wh", "Energy cutoff", "Wh", False)

    def _edit_sync(self) -> None:
        self.form.sync_voltage_gate = not self.form.sync_voltage_gate
        self.save()
        if self.form.sync_voltage_gate:
            self.note("Match voltage cutoff on. The load turns itself off at the voltage cutoff.")
            self._push_gate()
        else:
            self.note("Match voltage cutoff off. The load keeps its own start and turn-off voltages.")

    def _edit_limits(self) -> None:
        if self.connection.load is None or self.connection.limits is None:
            self.note(
                "Connect to the load before changing device limits. They are read from the instrument.",
                error=True,
            )
            return
        print("Blank leaves that limit as the load has it. A number is written now.")
        on_v, off_v, ovp_now, ocp_now = self.connection.limits
        ovp, error = self._ask_optional_float("Over voltage (V)", ovp_now)
        if error:
            if error != "stop":
                self.note(error, error=True)
            return
        ocp, error = self._ask_optional_float("Over current (A)", ocp_now)
        if error:
            if error != "stop":
                self.note(error, error=True)
            return
        new_on = None
        new_off = None
        if self.form.sync_voltage_gate:
            print("Start and turn-off follow the voltage cutoff.")
        else:
            new_on, error = self._ask_optional_float("Start voltage (V)", on_v)
            if error:
                if error != "stop":
                    self.note(error, error=True)
                return
            new_off, error = self._ask_optional_float("Turn off (V)", off_v)
            if error:
                if error != "stop":
                    self.note(error, error=True)
                return
        if ovp is None and ocp is None and new_on is None and new_off is None:
            self.note("Device limits unchanged.")
            return
        error = protection_error(self.form, ovp, ocp)
        if error:
            self.note(error, error=True)
            return
        start = on_v if new_on is None else new_on
        stop = off_v if new_off is None else new_off
        if new_on is not None or new_off is not None:
            if start < 0.1 or stop < 0.1:
                self.note("Start voltage and turn off must be at least 0.100 V.", error=True)
                return
            if start < stop:
                self.note("Start voltage must be at least the turn-off voltage.", error=True)
                return
        try:
            notes = []
            if ovp is not None:
                read = self.connection.load.write_over_voltage(ovp)
                notes.append(f"over voltage {read:.3f} V")
            if ocp is not None:
                read = self.connection.load.write_over_current(ocp)
                notes.append(f"over current {read:.3f} A")
            if new_on is not None or new_off is not None:
                read_on, read_off = self.connection.load.write_voltage_levels(start, stop)
                notes.append(f"start {read_on:.3f} V, turn off {read_off:.3f} V")
            self._refresh()
        except Exception as exc:
            self.note(f"Could not set the load limits: {exc}", error=True)
            return
        self.note("Load " + ", ".join(notes) + ".")

    def _ask_optional_float(self, label: str, current: float | None):
        shown = "unset" if current is None else f"{current:.3f}"
        entered = self._ask(f"{label} [{shown}] (blank keeps):")
        if entered is None:
            return None, "stop"
        if entered == "":
            return None, None
        try:
            value = float(entered)
        except ValueError:
            return None, f"{label} must be a number."
        if not math.isfinite(value):
            return None, f"{label} must be a number."
        return value, None

    def _push_gate(self) -> None:
        if self.connection.load is None or not self.form.sync_voltage_gate:
            return
        try:
            cutoff = float(self.form.cutoff_voltage_v) if self.form.cutoff_voltage_on else None
        except ValueError:
            self.note("Voltage cutoff must be a number before it can be stored in the load.", error=True)
            return
        try:
            on_v, off_v = self.connection.load.write_voltage_gate(cutoff)
        except Exception as exc:
            self.note(f"Could not set the load voltage limits: {exc}", error=True)
            return
        self._refresh()
        if cutoff is None:
            self.note(f"Load voltage off set to {off_v:.3f} V.")
        else:
            self.note(f"Load turns itself off at {off_v:.3f} V.")

    def _choose_port(self) -> None:
        print("Looking for instruments...")
        try:
            names = list_visa_resources()
        except Exception as exc:
            self.note(str(exc), error=True)
            return
        if not names:
            self.note("No instruments found.")
            return
        for index, name in enumerate(names, start=1):
            print(f"{index:3d}  {name}")
        entered = self._ask("Number (blank cancels):")
        if entered is None:
            if self.notice == "Edit cancelled.":
                self.note("Resource unchanged.")
            return
        if entered == "":
            self.note("Resource unchanged.")
            return
        if not entered.isdigit() or not 1 <= int(entered) <= len(names):
            self.note("That number is not in the list.", error=True)
            return
        self.form.resource = names[int(entered) - 1]
        self.save()
        self.note(f"Resource {self.form.resource}")

    def _connect(self) -> None:
        resource = self.form.resource.strip()
        if not resource:
            self.note("Enter a resource path first.", error=True)
            return
        print(f"Connecting to {resource}")
        load = None
        try:
            if self.connection.load is not None:
                self._close_load(self.connection.load)
                self.connection.load = None
            load = ET54Load(resource)
            voltage = float(load.read_voltage())
            voltage_range, current_range = load.read_ranges()
            gate = load.read_voltage_gate()
            protections = load.read_protections()
        except Exception as exc:
            if load is not None:
                self._close_load(load)
            self.note(f"Could not connect: {exc}", error=True)
            return
        self.connection.load = load
        self.connection.resource = resource
        self.connection.identity = load.identity
        self.connection.resting_v = voltage
        self.connection.limits = (float(gate[0]), float(gate[1]), float(protections[0]), float(protections[1]))
        self.form.voltage_range = voltage_range
        self.form.current_range = current_range
        self.ranges_changed = False
        self.save()
        self.note(
            f"Connected  {load.identity}  resting {voltage:.3f} V. "
            f"Ranges read from the load: voltage {voltage_range}, current {current_range}."
        )

    def _disconnect(self) -> None:
        if self.connection.load is None:
            self.note("Already disconnected.")
            return
        load = self.connection.load
        self.connection = Connection()
        error = self._close_load(load)
        if error:
            self.note(f"Could not disconnect: {error}", error=True)
            return
        self.note("Disconnected.")

    def _apply_ranges(self) -> None:
        if self.connection.load is None:
            self.note("Connect to the load before setting ranges.", error=True)
            return
        print(
            f"Setting voltage range {self.form.voltage_range}, "
            f"current range {self.form.current_range}."
        )
        try:
            self.connection.load.apply_ranges(self.form.voltage_range, self.form.current_range)
            self.ranges_changed = False
            self._refresh()
        except Exception as exc:
            self.note(f"Could not set ranges: {exc}", error=True)
            return
        self.note(
            f"Ranges set: voltage {self.form.voltage_range}, current {self.form.current_range}. "
            "The load reset over-voltage and over-current to that range's preset."
        )

    def _config_for_load(self, load, config: TestConfig) -> TestConfig:
        """Use the instrument's ranges and limits unless this session changed them.

        A range change is the only start-up write that touches device limits.
        Over-voltage, over-current, and the voltage gate stay as the load has
        them. Match voltage cutoff still copies the cutoff when that setting is on.
        """
        if self.ranges_changed:
            return replace(config, ovp_v=None, ocp_a=None, program_ranges=True)
        voltage_range, current_range = load.read_ranges()
        self.form.voltage_range = voltage_range
        self.form.current_range = current_range
        return replace(
            config,
            voltage_range=voltage_range,
            current_range=current_range,
            ovp_v=None,
            ocp_a=None,
            program_ranges=False,
        )

    def _refresh(self) -> None:
        if self.connection.load is None:
            return
        voltage = float(self.connection.load.read_voltage())
        gate = self.connection.load.read_voltage_gate()
        protections = self.connection.load.read_protections()
        self.connection.resting_v = voltage
        self.connection.limits = (float(gate[0]), float(gate[1]), float(protections[0]), float(protections[1]))

    def _refresh_after_run(self) -> None:
        if self.connection.load is None:
            return
        try:
            self._refresh()
        except Exception as exc:
            self.note(f"The run finished, but the latest reading failed: {exc}", error=True)

    @staticmethod
    def _close_load(load) -> str | None:
        try:
            load.off()
            load.close()
        except Exception as exc:
            return str(exc)
        return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = CliApp()
    error = apply_overrides(app.form, args)
    if error:
        print(error, file=sys.stderr)
        return 2
    if overrides_used(args):
        app.save()
        if app.notice_error:
            print(app.notice, file=sys.stderr)
            return 2
    if args.list_ports:
        return app.print_ports()
    if args.start:
        return app.run_discharge(confirm=False, assume_yes=args.yes)
    if not sys.stdin.isatty():
        print(render_setup(app.form, app.connection, app.notice))
        return 0
    return app.menu()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
