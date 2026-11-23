#!/usr/bin/env python3
"""Capacity test UI for an ET54 electronic load.

Logs voltage, current, discharge amp-hours, and energy to CSV on the sample
interval and plots those same points. An existing log can be opened onto the
charts. Discharge can be constant current, constant voltage, CC+CV, constant
resistance, or constant power. The test stops when any enabled cutoff is
reached. Open-circuit checks are optional.

    python et54xx_app.py
"""

from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

from capacity import (
    OCV_WAITS_S,
    POWER_MAX_W,
    CapacityTest,
    ET54Load,
    Sample,
    list_visa_resources,
    load_samples,
    load_saved_resource,
    load_settings,
    save_saved_resource,
    save_settings,
    TestConfig,
    TestResult,
    validate_config,
    voltage_gate_levels,
)

BG = "#e7ecea"
PANEL = "#fbfcfb"
INK = "#1c2421"
MUTED = "#5c6762"
LINE = "#d5dbd8"
TEAL = "#0e6b5c"
TEAL_ACTIVE = "#128072"
STOP = "#8f2d2d"
STOP_ACTIVE = "#a83838"
LOADED_COLOR = "#8aa39c"
VOLTAGE_COLOR = "#0e6b5c"
CURRENT_COLOR = "#b45309"
CHARGE_COLOR = "#1d4e89"
CUTOFF_COLOR = "#8f2d2d"
LINK_OK = "#1d7a46"
HEARTBEAT_MS = 3000
RESOURCE_PROMPT = "Click Select or enter resource path"
MODE_OPTIONS = (
    ("CC", "Constant current"),
    ("CV", "Constant voltage"),
    ("CCCV", "CC + CV"),
    ("CR", "Constant resistance"),
    ("CP", "Constant power"),
)


class CapacityApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ET54xx Control")
        self.geometry("1120x820")
        self.minsize(980, 680)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.queue: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.session: ET54Load | None = None
        self.worker: threading.Thread | None = None
        self.running = False
        self.connected = False
        self.resting_v: float | None = None
        self.samples: list[Sample] = []
        self.cutoff_voltage: float | None = 5.0
        self.cutoff_current: float | None = None
        self.cutoff_ah: float | None = None
        self.ocv_mode = False
        self.run_started: float | None = None
        self.finished_elapsed: float | None = None
        self._cutoff_lines: list = []
        self._voltage_line = None
        self._loaded_line = None
        self._current_line = None
        self._charge_line = None

        self._heartbeat_active = False
        self._listing_resources = False
        self._closing = False
        self._saving_form = False
        self._build_style()
        self._build()
        self.after(100, self._poll)
        self.after(HEARTBEAT_MS, self._heartbeat)

    def _build_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=INK, font=("DejaVu Sans", 10))
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=INK, font=("DejaVu Sans", 10))
        style.configure("Panel.TLabel", background=PANEL, foreground=INK, font=("DejaVu Sans", 10))
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=("DejaVu Sans", 9))
        style.configure("Hint.TLabel", background=PANEL, foreground=MUTED, font=("DejaVu Sans", 9))
        style.configure("TEntry", fieldbackground="#ffffff", padding=4)
        style.configure("TCheckbutton", background=PANEL, foreground=INK, font=("DejaVu Sans", 10))
        style.configure("TCombobox", padding=2, arrowsize=12)
        style.map(
            "TCombobox",
            fieldbackground=[
                ("disabled", "#e4e7e6"),
                ("readonly", "#ffffff"),
            ],
            foreground=[
                ("disabled", "#8d9692"),
                ("readonly", INK),
            ],
            selectbackground=[
                ("readonly", "focus", "#ffffff"),
                ("readonly", "#ffffff"),
            ],
            selectforeground=[
                ("readonly", "focus", INK),
                ("readonly", INK),
            ],
            arrowcolor=[("disabled", "#999999")],
        )
        style.configure("Start.TButton", background=TEAL, foreground="#ffffff", font=("DejaVu Sans", 10, "bold"), padding=(14, 8))
        style.map(
            "Start.TButton",
            background=[("disabled", "#b7c7c3"), ("pressed", "#0a5348"), ("active", TEAL_ACTIVE)],
            foreground=[("disabled", "#f4f7f6")],
        )
        style.configure("Stop.TButton", background=STOP, foreground="#ffffff", font=("DejaVu Sans", 10, "bold"), padding=(14, 8))
        style.map(
            "Stop.TButton",
            background=[("disabled", "#d7c4c4"), ("pressed", "#732424"), ("active", STOP_ACTIVE)],
            foreground=[("disabled", "#f7f3f3")],
        )
        style.configure("TButton", padding=(10, 6), font=("DejaVu Sans", 10))

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        connection = ttk.Frame(self, padding=(14, 12, 14, 4))
        connection.grid(row=0, column=0, sticky="ew")
        connection.columnconfigure(1, weight=1)

        ttk.Label(connection, text="Resource").grid(row=0, column=0, sticky="w")
        self._settings_path = Path(__file__).resolve().with_name("settings.json")
        stored = load_settings(self._settings_path)
        saved_resource = stored.get("resource")
        if not isinstance(saved_resource, str) or not saved_resource.strip() or saved_resource.strip() == RESOURCE_PROMPT:
            saved_resource = RESOURCE_PROMPT
        else:
            saved_resource = saved_resource.strip()
        self.resource_var = tk.StringVar(value=saved_resource)
        self._stored = stored
        self.sync_gate_var = tk.BooleanVar(value=self._saved_bool("sync_voltage_gate", False))
        self.resource_entry = ttk.Entry(connection, textvariable=self.resource_var)
        self.resource_entry.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        self.select_resource_button = ttk.Button(
            connection, text="Select", width=8, command=self._select_resource
        )
        self.select_resource_button.grid(row=0, column=2, padx=(0, 12))

        link_font = tkfont.Font(self, family="DejaVu Sans", size=10, weight="bold")
        link_texts = ("●  Disconnected", "●  Connecting", "●  Connected", "●  No response")
        link_holder = tk.Frame(
            connection,
            width=max(link_font.measure(text) for text in link_texts) + 16,
            height=link_font.metrics("linespace") + 4,
            bg=BG,
        )
        link_holder.pack_propagate(False)
        link_holder.grid(row=0, column=3, sticky="w", padx=(0, 12))
        self.link = tk.Label(
            link_holder,
            text="●  Disconnected",
            anchor="w",
            bg=BG,
            fg=MUTED,
            font=link_font,
        )
        self.link.pack(fill="both")

        self.connect_button = ttk.Button(connection, text="Connect", width=10, command=self._connect)
        self.connect_button.grid(row=0, column=4, padx=(0, 6))
        self.disconnect_button = ttk.Button(
            connection, text="Disconnect", width=12, command=self._disconnect
        )
        self.disconnect_button.grid(row=0, column=5)

        body = ttk.Frame(self, padding=(14, 8, 14, 8))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        self._build_settings(body)

        display = ttk.Frame(body)
        display.grid(row=0, column=1, sticky="nsew")
        display.columnconfigure(0, weight=1)
        display.rowconfigure(1, weight=1)

        readouts = tk.Frame(display, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=8, pady=8)
        readouts.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for column in range(5):
            readouts.columnconfigure(column, weight=1)
        self.readout_values = {}
        for column, (name, unit) in enumerate(
            (("Voltage", "V"), ("Current", "A"), ("Discharge", "Ah"), ("Energy", "Wh"), ("Elapsed", "h:m:s"))
        ):
            cell = tk.Frame(readouts, bg=PANEL)
            cell.grid(row=0, column=column, sticky="ew", padx=6)
            tk.Label(cell, text=f"{name}  {unit}".upper(), bg=PANEL, fg=MUTED, font=("DejaVu Sans", 8)).pack(anchor="w")
            value = tk.Label(cell, text="—", bg=PANEL, fg=INK, font=("DejaVu Sans", 22))
            value.pack(anchor="w")
            self.readout_values[name] = value

        chart_frame = tk.Frame(display, bg=BG)
        chart_frame.grid(row=1, column=0, sticky="nsew")
        chart_frame.columnconfigure(0, weight=1)
        chart_frame.rowconfigure(0, weight=1)
        self._build_charts(chart_frame)

        self.status = tk.Label(
            self,
            text="Disconnected",
            anchor="w",
            bg=BG,
            fg=INK,
            font=("DejaVu Sans", 10),
            padx=14,
            pady=8,
        )
        self.status.grid(row=2, column=0, sticky="ew")
        self._sync_buttons()
        self.after_idle(lambda: self.csv_entry.xview_moveto(1))
        self.after_idle(lambda: self.resource_entry.xview_moveto(1))

    def _build_settings(self, body: ttk.Frame) -> None:
        panel = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        panel.grid(row=0, column=0, sticky="ns", padx=(0, 12))
        panel.rowconfigure(0, weight=1)
        panel.columnconfigure(0, weight=1)

        self.settings_canvas = tk.Canvas(
            panel, bg=PANEL, highlightthickness=0, width=360, yscrollincrement=1
        )
        scrollbar = ttk.Scrollbar(panel, orient="vertical", command=self.settings_canvas.yview)
        self.settings_canvas.configure(yscrollcommand=scrollbar.set)
        self.settings_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        inner = ttk.Frame(self.settings_canvas, style="Panel.TFrame", padding=(12, 10, 12, 4))
        self._settings_inner = inner
        self._settings_window = self.settings_canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _event: self._refresh_settings_scroll())
        self.settings_canvas.bind("<Configure>", lambda event: self.settings_canvas.itemconfigure(self._settings_window, width=event.width))
        inner.columnconfigure(0, weight=1)

        self.mode_var = tk.StringVar(value=self._saved_mode_label())
        self.current_var = tk.StringVar(value=self._saved_text("current_a", "2.0"))
        self.voltage_set_var = tk.StringVar(value=self._saved_text("voltage_set_v", "60.0"))
        self.resistance_var = tk.StringVar(value=self._saved_text("resistance_ohm", "10"))
        self.power_var = tk.StringVar(value=self._saved_text("power_w", "100"))
        self.interval_var = tk.StringVar(value="5")
        self.csv_var = tk.StringVar(value="capacity_log.csv")
        self.ocv_var = tk.BooleanVar(value=False)
        self.wait_var = tk.StringVar(value="1 s")
        self.voltage_range_var = tk.StringVar(value="High")
        self.current_range_var = tk.StringVar(value="Low")
        self.cutoff_voltage_on = tk.BooleanVar(value=self._saved_bool("cutoff_voltage_on", True))
        self.cutoff_current_on = tk.BooleanVar(value=self._saved_bool("cutoff_current_on", False))
        self.cutoff_time_on = tk.BooleanVar(value=self._saved_bool("cutoff_time_on", False))
        self.cutoff_ah_on = tk.BooleanVar(value=self._saved_bool("cutoff_ah_on", False))
        self.cutoff_wh_on = tk.BooleanVar(value=self._saved_bool("cutoff_wh_on", False))
        self.cutoff_voltage_var = tk.StringVar(value=self._saved_text("cutoff_voltage_v", "5.0"))
        self.cutoff_current_var = tk.StringVar(value=self._saved_text("cutoff_current_a", "0.20"))
        self.cutoff_time_var = tk.StringVar(value=self._saved_text("cutoff_time_s", "3600"))
        self.cutoff_ah_var = tk.StringVar(value=self._saved_text("cutoff_ah", "10"))
        self.cutoff_wh_var = tk.StringVar(value=self._saved_text("cutoff_wh", "500"))

        ttk.Label(inner, text="Mode", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.mode_combo = ttk.Combobox(
            inner,
            textvariable=self.mode_var,
            values=[label for _code, label in MODE_OPTIONS],
            width=22,
            state="readonly",
        )
        self.mode_combo.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        self.mode_combo.bind("<<ComboboxSelected>>", self._combo_chosen)

        setpoints = ttk.Frame(inner, style="Panel.TFrame")
        setpoints.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        setpoints.columnconfigure(0, weight=1)
        self.current_row, self.current_entry = self._value_row(setpoints, 0, "Current", self.current_var, "A")
        self.voltage_row, self.voltage_set_entry = self._value_row(setpoints, 1, "CV voltage", self.voltage_set_var, "V")
        self.resistance_row, self.resistance_entry = self._value_row(setpoints, 2, "Resistance", self.resistance_var, "Ω")
        self.power_row, self.power_entry = self._value_row(setpoints, 3, "Power", self.power_var, "W")
        for entry in (self.current_entry, self.voltage_set_entry, self.resistance_entry, self.power_entry):
            entry.bind("<FocusOut>", lambda _event: self._save_form_settings())
            entry.bind("<Return>", lambda _event: self._save_form_settings())

        ttk.Label(inner, text="Sample interval", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=(10, 0))
        interval_row = ttk.Frame(inner, style="Panel.TFrame")
        interval_row.grid(row=4, column=0, sticky="ew", pady=(3, 0))
        self.interval_entry = ttk.Entry(interval_row, textvariable=self.interval_var, width=12)
        self.interval_entry.grid(row=0, column=0, sticky="w")
        ttk.Label(interval_row, text="s", style="Muted.TLabel").grid(row=0, column=1, padx=(8, 0))

        self.csv_enabled_var = tk.BooleanVar(value=self._saved_bool("csv_enabled", True))
        self.csv_enabled_check = ttk.Checkbutton(
            inner,
            text="Write CSV",
            variable=self.csv_enabled_var,
            command=self._csv_toggled,
        )
        self.csv_enabled_check.grid(row=5, column=0, sticky="w", pady=(10, 0))
        csv_row = ttk.Frame(inner, style="Panel.TFrame")
        csv_row.grid(row=6, column=0, sticky="ew", pady=(3, 0))
        csv_row.columnconfigure(0, weight=1)
        self.csv_entry = ttk.Entry(csv_row, textvariable=self.csv_var)
        self.csv_entry.grid(row=0, column=0, sticky="ew")
        self.browse_button = ttk.Button(csv_row, text="Browse", command=self._browse)
        self.browse_button.grid(row=0, column=1, padx=(8, 0))
        self.open_button = ttk.Button(csv_row, text="Open log", command=self._open_log)
        self.open_button.grid(row=1, column=0, sticky="w", pady=(6, 0))

        self.ocv_check = ttk.Checkbutton(
            inner,
            text="Check open-circuit voltage",
            variable=self.ocv_var,
            command=self._sync_ocv,
            style="TCheckbutton",
        )
        self.ocv_check.grid(row=7, column=0, sticky="w", pady=(12, 0))
        wait_row = ttk.Frame(inner, style="Panel.TFrame")
        wait_row.grid(row=8, column=0, sticky="w", pady=(6, 0))
        ttk.Label(wait_row, text="Settle time", style="Panel.TLabel").grid(row=0, column=0)
        self.wait_combo = ttk.Combobox(
            wait_row,
            textvariable=self.wait_var,
            values=[f"{wait:g} s" for wait in OCV_WAITS_S],
            width=8,
            state="disabled",
        )
        self.wait_combo.grid(row=0, column=1, padx=(8, 0))
        self.wait_combo.bind("<<ComboboxSelected>>", self._combo_released)
        self.mode_hint = ttk.Label(
            inner,
            text="The voltage cutoff uses the reading taken while the load is sinking current.",
            style="Hint.TLabel",
            wraplength=300,
            justify="left",
        )
        self.mode_hint.grid(row=9, column=0, sticky="w", pady=(8, 0))

        range_row = ttk.Frame(inner, style="Panel.TFrame")
        range_row.grid(row=10, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(range_row, text="Voltage range", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.voltage_range_combo = ttk.Combobox(
            range_row,
            textvariable=self.voltage_range_var,
            values=("Low", "High"),
            width=8,
            state="readonly",
        )
        self.voltage_range_combo.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.voltage_range_combo.bind("<<ComboboxSelected>>", self._combo_released)
        ttk.Label(range_row, text="Current range", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.current_range_combo = ttk.Combobox(
            range_row,
            textvariable=self.current_range_var,
            values=("Low", "High"),
            width=8,
            state="readonly",
        )
        self.current_range_combo.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        self.current_range_combo.bind("<<ComboboxSelected>>", self._combo_released)
        self.set_ranges_button = ttk.Button(range_row, text="Set Now", command=self._set_ranges_now)
        self.set_ranges_button.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.power_hint = ttk.Label(inner, text="", style="Hint.TLabel", wraplength=320, justify="left")
        self.power_hint.grid(row=11, column=0, sticky="w", pady=(8, 0))
        for variable in (self.current_var, self.voltage_set_var, self.resistance_var, self.power_var):
            variable.trace_add("write", lambda *_args: self._update_power_hint())

        ttk.Label(inner, text="Stop when", style="Panel.TLabel").grid(row=12, column=0, sticky="w", pady=(12, 0))
        cutoffs = ttk.Frame(inner, style="Panel.TFrame")
        cutoffs.grid(row=13, column=0, sticky="ew", pady=(4, 0))
        self.cutoff_voltage_check, self.cutoff_voltage_entry = self._cutoff_row(
            cutoffs, 0, "Voltage ≤", self.cutoff_voltage_on, self.cutoff_voltage_var, "V"
        )
        self.cutoff_current_check, self.cutoff_current_entry = self._cutoff_row(
            cutoffs, 1, "Current ≤", self.cutoff_current_on, self.cutoff_current_var, "A"
        )
        self.cutoff_time_check, self.cutoff_time_entry = self._cutoff_row(
            cutoffs, 2, "Time ≥", self.cutoff_time_on, self.cutoff_time_var, "s"
        )
        self.cutoff_ah_check, self.cutoff_ah_entry = self._cutoff_row(
            cutoffs, 3, "Discharge ≥", self.cutoff_ah_on, self.cutoff_ah_var, "Ah"
        )
        self.cutoff_wh_check, self.cutoff_wh_entry = self._cutoff_row(
            cutoffs, 4, "Energy ≥", self.cutoff_wh_on, self.cutoff_wh_var, "Wh"
        )
        self.cutoff_voltage_entry.bind("<FocusOut>", lambda _event: self._cutoff_edited())
        self.cutoff_voltage_entry.bind("<Return>", lambda _event: self._cutoff_edited())
        for entry in (
            self.cutoff_current_entry,
            self.cutoff_time_entry,
            self.cutoff_ah_entry,
            self.cutoff_wh_entry,
        ):
            entry.bind("<FocusOut>", lambda _event: self._save_form_settings())
            entry.bind("<Return>", lambda _event: self._save_form_settings())
        self.cutoff_voltage_check.configure(command=self._voltage_cutoff_toggled)
        ttk.Label(
            inner,
            text="Voltage and current stop at or below the limit. Time, discharge, and energy stop at or above it.",
            style="Hint.TLabel",
            wraplength=300,
            justify="left",
        ).grid(row=14, column=0, sticky="w", pady=(6, 4))

        ttk.Label(inner, text="ET54xx Device Limits", style="Panel.TLabel").grid(row=15, column=0, sticky="w", pady=(12, 0))
        self.sync_gate_check = ttk.Checkbutton(
            inner,
            text="Match voltage cutoff",
            variable=self.sync_gate_var,
            command=self._sync_gate_toggled,
        )
        self.sync_gate_check.grid(row=16, column=0, sticky="w", pady=(6, 0))
        self.load_on_var = tk.StringVar(value="")
        self.load_off_var = tk.StringVar(value="")
        gate_row = ttk.Frame(inner, style="Panel.TFrame")
        gate_row.grid(row=17, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(gate_row, text="Start voltage", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.load_on_entry = ttk.Entry(gate_row, textvariable=self.load_on_var, width=8)
        self.load_on_entry.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(gate_row, text="V", style="Muted.TLabel").grid(row=0, column=2, padx=(6, 0))
        ttk.Label(gate_row, text="Turn off", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.load_off_entry = ttk.Entry(gate_row, textvariable=self.load_off_var, width=8)
        self.load_off_entry.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(gate_row, text="V", style="Muted.TLabel").grid(row=1, column=2, padx=(6, 0), pady=(6, 0))
        self.ovp_var = tk.StringVar(value="")
        self.ocp_var = tk.StringVar(value="")
        ttk.Label(gate_row, text="Over voltage", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.ovp_entry = ttk.Entry(gate_row, textvariable=self.ovp_var, width=8)
        self.ovp_entry.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(gate_row, text="V", style="Muted.TLabel").grid(row=2, column=2, padx=(6, 0), pady=(6, 0))
        ttk.Label(gate_row, text="Over current", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=(6, 0))
        self.ocp_entry = ttk.Entry(gate_row, textvariable=self.ocp_var, width=8)
        self.ocp_entry.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(gate_row, text="A", style="Muted.TLabel").grid(row=3, column=2, padx=(6, 0), pady=(6, 0))
        self.send_gate_button = ttk.Button(gate_row, text="Send", command=self._send_voltage_levels)
        self.send_gate_button.grid(row=0, column=3, rowspan=4, sticky="w", padx=(16, 0))
        ttk.Label(
            inner,
            text="Start voltage lets the load conduct. Turn off makes the load stop by itself at that voltage. Over voltage and over current trip the load's own protection. Send writes these boxes to the load.",
            style="Hint.TLabel",
            wraplength=300,
            justify="left",
        ).grid(row=18, column=0, sticky="w", pady=(6, 4))
        self._sync_mode()
        self._bind_settings_wheel(self.settings_canvas)
        self._bind_settings_wheel(inner)

        buttons = ttk.Frame(panel, style="Panel.TFrame", padding=(12, 8, 12, 12))
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.start_button = ttk.Button(buttons, text="Start", style="Start.TButton", command=self._start)
        self.start_button.grid(row=0, column=0, padx=(0, 8))
        self.stop_button = ttk.Button(buttons, text="Stop", style="Stop.TButton", command=self._stop)
        self.stop_button.grid(row=0, column=1)

    def _value_row(self, parent: ttk.Frame, row: int, label: str, variable: tk.StringVar, unit: str):
        frame = ttk.Frame(parent, style="Panel.TFrame")
        frame.grid(row=row, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(frame, text=label, style="Panel.TLabel", width=12).grid(row=0, column=0, sticky="w")
        entry = ttk.Entry(frame, textvariable=variable, width=10)
        entry.grid(row=0, column=1, sticky="w")
        ttk.Label(frame, text=unit, style="Muted.TLabel").grid(row=0, column=2, padx=(8, 0))
        return frame, entry

    def _cutoff_row(self, parent: ttk.Frame, row: int, label: str, enabled: tk.BooleanVar, variable: tk.StringVar, unit: str):
        check = ttk.Checkbutton(parent, text=label, variable=enabled, command=self._cutoffs_changed, style="TCheckbutton")
        check.grid(row=row, column=0, sticky="w", pady=(3, 0))
        entry = ttk.Entry(parent, textvariable=variable, width=8)
        entry.grid(row=row, column=1, sticky="w", padx=(6, 0), pady=(3, 0))
        ttk.Label(parent, text=unit, style="Muted.TLabel").grid(row=row, column=2, sticky="w", padx=(6, 0))
        return check, entry

    def _bind_settings_wheel(self, widget) -> None:
        widget.bind("<Button-4>", self._settings_wheel_up, add="+")
        widget.bind("<Button-5>", self._settings_wheel_down, add="+")
        widget.bind("<MouseWheel>", self._settings_wheel_mouse, add="+")
        for child in widget.winfo_children():
            self._bind_settings_wheel(child)

    def _settings_wheel_up(self, _event):
        self.settings_canvas.yview_scroll(-24, "units")
        return "break"

    def _settings_wheel_down(self, _event):
        self.settings_canvas.yview_scroll(24, "units")
        return "break"

    def _settings_wheel_mouse(self, event):
        steps = int(-event.delta / 120) or (-1 if event.delta > 0 else 1)
        self.settings_canvas.yview_scroll(steps * 24, "units")
        return "break"

    def _refresh_settings_scroll(self) -> None:
        if getattr(self, "_refreshing_scroll", False):
            return
        self._refreshing_scroll = True
        try:
            canvas = self.settings_canvas
            top = canvas.canvasy(0)
            self.update_idletasks()
            bbox = canvas.bbox("all")
            if not bbox:
                return
            canvas.configure(scrollregion=bbox)
            height = max(bbox[3] - bbox[1], 1)
            visible = canvas.winfo_height()
            if height <= visible:
                canvas.yview_moveto(0)
                return
            canvas.yview_moveto(min(max(top, 0), height - visible) / height)
        finally:
            self._refreshing_scroll = False

    def _mode_code(self) -> str:
        label = self.mode_var.get()
        for code, text in MODE_OPTIONS:
            if text == label:
                return code
        return "CC"

    def _sync_mode(self) -> None:
        mode = self._mode_code()
        self._show_row(self.current_row, mode in ("CC", "CCCV"))
        self._show_row(self.voltage_row, mode in ("CV", "CCCV"))
        self._show_row(self.resistance_row, mode == "CR")
        self._show_row(self.power_row, mode == "CP")
        self._update_power_hint()
        self._refresh_settings_scroll()

    def _show_row(self, frame: ttk.Frame, visible: bool) -> None:
        if visible:
            frame.grid()
        else:
            frame.grid_remove()

    def _sync_cutoffs(self) -> None:
        running = self.running
        pairs = (
            (self.cutoff_voltage_on, self.cutoff_voltage_entry),
            (self.cutoff_current_on, self.cutoff_current_entry),
            (self.cutoff_time_on, self.cutoff_time_entry),
            (self.cutoff_ah_on, self.cutoff_ah_entry),
            (self.cutoff_wh_on, self.cutoff_wh_entry),
        )
        for flag, entry in pairs:
            entry.configure(state="disabled" if running or not flag.get() else "normal")

    def _build_charts(self, parent) -> None:
        import matplotlib

        matplotlib.rcParams["font.family"] = "DejaVu Sans"
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.figure import Figure

        self.figure = Figure(figsize=(7.4, 5.6), dpi=100, facecolor=BG)
        self.figure.subplots_adjust(left=0.10, right=0.98, top=0.97, bottom=0.09, hspace=0.28)
        self.ax_v, self.ax_i, self.ax_q = self.figure.subplots(3, 1, sharex=True)
        series = (
            (self.ax_v, "Voltage (V)", VOLTAGE_COLOR),
            (self.ax_i, "Current (A)", CURRENT_COLOR),
            (self.ax_q, "Discharge (Ah)", CHARGE_COLOR),
        )
        for axis, label, color in series:
            axis.set_facecolor("#ffffff")
            axis.set_ylabel(label, fontsize=9)
            axis.tick_params(colors=INK, labelsize=8)
            axis.grid(True, color="#e4e9e7", linewidth=0.6)
            for side in ("top", "right"):
                axis.spines[side].set_visible(False)
            axis.spines["left"].set_color("#9aa39f")
            axis.spines["bottom"].set_color("#9aa39f")
            axis.yaxis.label.set_color(INK)
            line, = axis.plot([], [], color=color, linewidth=1.6)
            if axis is self.ax_v:
                self._voltage_line = line
                self._loaded_line, = axis.plot([], [], color=LOADED_COLOR, linewidth=1.1)
            elif axis is self.ax_i:
                self._current_line = line
            else:
                self._charge_line = line
        self.ax_q.set_xlabel("Elapsed (min)", fontsize=9)
        self.ax_q.xaxis.label.set_color(INK)
        self._loaded_line.set_visible(False)
        self._x_full = (0.0, 1.0)
        self._x_view = (0.0, 1.0)
        self._x_follow = True

        self.canvas = FigureCanvasTkAgg(self.figure, master=parent)
        self.canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        chart_widget = self.canvas.get_tk_widget()
        for sequence in ("<Shift-Button-4>", "<Shift-Button-5>", "<Shift-MouseWheel>"):
            chart_widget.bind(sequence, self._shift_scroll_time)

        time_row = tk.Frame(parent, bg=BG)
        time_row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        time_row.columnconfigure(0, weight=1)
        self.x_start_label = tk.Label(time_row, text="0:00:00", bg=BG, fg=MUTED, font=("DejaVu Sans", 9))
        self.x_start_label.grid(row=0, column=0, sticky="w")
        self.x_end_label = tk.Label(time_row, text="0:00:00", bg=BG, fg=MUTED, font=("DejaVu Sans", 9))
        self.x_end_label.grid(row=0, column=1, sticky="e")
        self.time_bar = TimeRangeBar(time_row, command=self._on_time_bar)
        self.time_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self._reset_chart_limits()

    def _sync_ocv(self) -> None:
        enabled = bool(self.ocv_var.get()) and not self.running
        self._set_combo_enabled(self.wait_combo, enabled)
        if self.ocv_var.get():
            text = "At each sample the load turns off, waits, then records rested voltage. The voltage cutoff uses that reading."
        else:
            text = "The voltage cutoff uses the reading taken while the load is sinking current."
        self.mode_hint.configure(text=text)

    def _update_power_hint(self) -> None:
        mode = self._mode_code()
        volts = self.resting_v if self.resting_v is not None else 69.0
        where = f"resting {volts:.2f} V" if self.resting_v is not None else "69 V"
        try:
            if mode in ("CC", "CCCV"):
                amps = float(self.current_var.get())
                watts = volts * amps
                text = f"At {where}, {amps:.2f} A is {watts:.0f} W. Rating {POWER_MAX_W:.0f} W."
                over = watts > POWER_MAX_W
            elif mode == "CP":
                watts = float(self.power_var.get())
                text = f"Power setpoint {watts:.0f} W. Rating {POWER_MAX_W:.0f} W."
                over = watts > POWER_MAX_W
            elif mode == "CR":
                ohms = float(self.resistance_var.get())
                watts = (volts * volts / ohms) if ohms else float("inf")
                text = f"At {where}, {ohms:.3g} Ω is {watts:.0f} W. Rating {POWER_MAX_W:.0f} W."
                over = watts > POWER_MAX_W
            else:
                text = f"CV holds the setpoint voltage and is limited to {POWER_MAX_W:.0f} W."
                over = False
        except ValueError:
            text = f"Load rating {POWER_MAX_W:.0f} W."
            over = False
        self.power_hint.configure(text=text, foreground=STOP if over else MUTED)

    def _sync_buttons(self) -> None:
        busy = self.worker is not None and self.worker.is_alive()
        self.start_button.configure(state="disabled" if self.running or busy else "normal")
        self.stop_button.configure(state="normal" if self.running else "disabled")
        self.connect_button.configure(state="disabled" if self.connected or self.running or busy else "normal")
        self.disconnect_button.configure(state="normal" if self.connected and not self.running else "disabled")
        self.set_ranges_button.configure(
            state="normal" if self.connected and not self.running and not busy else "disabled"
        )
        self.select_resource_button.configure(
            state="disabled" if self.connected or self.running or self._listing_resources else "normal"
        )
        entry_state = "disabled" if self.running else "normal"
        for entry in (
            self.resource_entry,
            self.current_entry,
            self.voltage_set_entry,
            self.resistance_entry,
            self.power_entry,
            self.interval_entry,
        ):
            entry.configure(state=entry_state)
        csv_state = "disabled" if self.running or not self.csv_enabled_var.get() else "normal"
        self.csv_entry.configure(state=csv_state)
        self.browse_button.configure(state=csv_state)
        self.csv_enabled_check.configure(state=entry_state)
        self.open_button.configure(state=entry_state)
        self.ocv_check.configure(state=entry_state)
        for check in (
            self.cutoff_voltage_check,
            self.cutoff_current_check,
            self.cutoff_time_check,
            self.cutoff_ah_check,
            self.cutoff_wh_check,
            self.sync_gate_check,
        ):
            check.configure(state=entry_state)
        limits_open = self.connected and not self.running
        self.ovp_entry.configure(state="normal" if limits_open else "disabled")
        self.ocp_entry.configure(state="normal" if limits_open else "disabled")
        gate_state = "normal" if limits_open and not self.sync_gate_var.get() else "disabled"
        self.load_on_entry.configure(state=gate_state)
        self.load_off_entry.configure(state=gate_state)
        self.send_gate_button.configure(
            state="normal" if self.connected and not self.running and not busy else "disabled"
        )
        editable = not self.running
        for combo in (self.mode_combo, self.voltage_range_combo, self.current_range_combo):
            self._set_combo_enabled(combo, editable)
        self._sync_cutoffs()
        self._sync_ocv()

    def _set_combo_enabled(self, combo: ttk.Combobox, enabled: bool) -> None:
        # configure(state="readonly") leaves the pressed flag set, which keeps
        # the dropdown from opening again. Adjust the state flags directly.
        if enabled:
            combo.state(["!disabled", "!pressed", "readonly"])
            try:
                combo.selection_clear()
            except tk.TclError:
                pass
        else:
            combo.state(["disabled"])

    def _combo_chosen(self, event) -> None:
        self._combo_released(event)
        self._sync_mode()
        self._save_form_settings()

    def _saved_text(self, key: str, default: str) -> str:
        value = self._stored.get(key)
        if isinstance(value, str) and value.strip():
            return value
        return default

    def _saved_bool(self, key: str, default: bool) -> bool:
        value = self._stored.get(key)
        if isinstance(value, bool):
            return value
        return default

    def _saved_mode_label(self) -> str:
        saved = self._stored.get("mode")
        if isinstance(saved, str):
            for code, label in MODE_OPTIONS:
                if code == saved:
                    return label
        return MODE_OPTIONS[0][1]

    def _save_form_settings(self) -> None:
        if getattr(self, "_saving_form", False) or self._closing:
            return
        self._saving_form = True
        try:
            save_settings(
                self._settings_path,
                mode=self._mode_code(),
                current_a=self.current_var.get().strip(),
                voltage_set_v=self.voltage_set_var.get().strip(),
                resistance_ohm=self.resistance_var.get().strip(),
                power_w=self.power_var.get().strip(),
                cutoff_voltage_on=bool(self.cutoff_voltage_on.get()),
                cutoff_voltage_v=self.cutoff_voltage_var.get().strip(),
                cutoff_current_on=bool(self.cutoff_current_on.get()),
                cutoff_current_a=self.cutoff_current_var.get().strip(),
                cutoff_time_on=bool(self.cutoff_time_on.get()),
                cutoff_time_s=self.cutoff_time_var.get().strip(),
                cutoff_ah_on=bool(self.cutoff_ah_on.get()),
                cutoff_ah=self.cutoff_ah_var.get().strip(),
                cutoff_wh_on=bool(self.cutoff_wh_on.get()),
                cutoff_wh=self.cutoff_wh_var.get().strip(),
                sync_voltage_gate=bool(self.sync_gate_var.get()),
                csv_enabled=bool(self.csv_enabled_var.get()),
            )
        except OSError as exc:
            self._set_status(f"Could not save settings: {exc.strerror or exc}", error=True)
        finally:
            self._saving_form = False

    def _csv_toggled(self) -> None:
        self._save_form_settings()
        self._sync_buttons()

    def _cutoffs_changed(self) -> None:
        self._sync_cutoffs()
        self._save_form_settings()

    def _combo_released(self, event) -> None:
        self._set_combo_enabled(event.widget, not self.running)

    def _set_status(self, text: str, error: bool = False) -> None:
        self.status.configure(text=text, fg=STOP if error else INK)

    def _retire_session(self) -> None:
        session = self.session
        self.session = None
        if session is None:
            return
        threading.Thread(target=self._close_quietly, args=(session,), daemon=True).start()

    @staticmethod
    def _close_quietly(session) -> None:
        try:
            session.close()
        except Exception:
            pass

    def _set_link(self, state: str) -> None:
        if state == "connected":
            self.link.configure(text="●  Connected", fg=LINK_OK)
        elif state == "connecting":
            self.link.configure(text="●  Connecting", fg=MUTED)
        elif state == "lost":
            self.link.configure(text="●  No response", fg=STOP)
        else:
            self.link.configure(text="●  Disconnected", fg=MUTED)

    def _heartbeat(self) -> None:
        if not self._closing:
            self.after(HEARTBEAT_MS, self._heartbeat)
        if (
            self._closing
            or not self.connected
            or self.session is None
            or self._heartbeat_active
            or self.running
            or self._busy()
        ):
            return
        self._heartbeat_active = True
        threading.Thread(target=self._heartbeat_worker, daemon=True).start()

    def _heartbeat_worker(self) -> None:
        session = self.session
        try:
            if session is None:
                return
            voltage = session.read_voltage()
        except Exception as exc:
            if self.session is session and self.connected and not self.running and not self._busy():
                self.queue.put(("heartbeat_lost", str(exc)))
            return
        finally:
            self._heartbeat_active = False
        if self.session is session and self.connected and not self.running:
            self.queue.put(("voltage", voltage))

    def _open_log(self) -> None:
        if self.running or self._busy():
            return
        current = Path(self.csv_var.get().strip()).expanduser()
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Open discharge log",
            defaultextension=".csv",
            filetypes=(("CSV files", "*.csv"), ("All files", "*.*")),
            initialdir=str(current.parent) if current.parent.is_dir() else None,
            initialfile=current.name,
        )
        if not chosen:
            return
        try:
            samples = load_samples(chosen)
        except ValueError as exc:
            messagebox.showerror("Open log", str(exc), parent=self)
            return
        self._show_log(samples, Path(chosen))

    def _show_log(self, samples: list[Sample], path: Path) -> None:
        self.samples = samples
        self.cutoff_voltage = None
        self.cutoff_current = None
        self.cutoff_ah = None
        self.run_started = None
        self.finished_elapsed = samples[-1].elapsed_s
        self._clear_chart()
        self._apply_voltage_legend(any(sample.source == "open_circuit" for sample in samples))
        self._show_sample(samples[-1])
        self._tick_elapsed()
        self._redraw()
        self._set_status(f"Reviewing {path.name}  {len(samples)} samples")

    def _browse(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Capacity log",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=Path(self.csv_var.get()).name or "capacity_log.csv",
            initialdir=str(Path(self.csv_var.get()).expanduser().parent) if self.csv_var.get() else str(Path.cwd()),
        )
        if path:
            self.csv_var.set(path)
            self.csv_entry.xview_moveto(1)

    def _selected_ranges(self) -> tuple[str, str]:
        return self.voltage_range_var.get().strip().lower(), self.current_range_var.get().strip().lower()

    def _select_resource(self) -> None:
        if self.connected or self.running or self._listing_resources:
            return
        self._listing_resources = True
        self._sync_buttons()
        self._set_status("Looking for instruments...")
        threading.Thread(target=self._list_resources_worker, daemon=True).start()

    def _list_resources_worker(self) -> None:
        try:
            names = list_visa_resources()
        except Exception as exc:
            self.queue.put(("resources_failed", str(exc)))
            return
        self.queue.put(("resources", names))

    def _choose_resource(self, resource: str) -> None:
        self.resource_var.set(resource)
        self.resource_entry.xview_moveto(1)
        self._remember_resource(resource)
        self._set_status(f"Resource {resource}")

    def _gate_cutoff(self) -> float | None:
        if not self.cutoff_voltage_on.get():
            return None
        return float(self.cutoff_voltage_var.get())

    def _show_voltage_gate(self, on_v: float, off_v: float, ovp_v: float | None = None, ocp_a: float | None = None) -> None:
        self.load_on_var.set(f"{on_v:.3f}")
        self.load_off_var.set(f"{off_v:.3f}")
        if ovp_v is not None:
            self.ovp_var.set(f"{ovp_v:.3f}")
        if ocp_a is not None:
            self.ocp_var.set(f"{ocp_a:.3f}")

    def _clear_voltage_gate(self) -> None:
        self.load_on_var.set("")
        self.load_off_var.set("")
        self.ovp_var.set("")
        self.ocp_var.set("")

    def _fill_matched_gate(self) -> None:
        if not self.connected:
            return
        try:
            cutoff = self._gate_cutoff()
        except ValueError:
            return
        on_v, off_v = voltage_gate_levels(cutoff)
        self.load_on_var.set(f"{on_v:.3f}")
        self.load_off_var.set(f"{off_v:.3f}")

    def _sync_gate_toggled(self) -> None:
        self._save_form_settings()
        self._sync_buttons()
        if not self.connected:
            return
        if self.sync_gate_var.get():
            self._fill_matched_gate()
            self._push_voltage_gate()
        else:
            self._refresh_voltage_gate()

    def _cutoff_edited(self) -> None:
        self._save_form_settings()
        if self.connected and self.sync_gate_var.get():
            self._fill_matched_gate()
        self._push_voltage_gate()

    def _voltage_cutoff_toggled(self) -> None:
        self._sync_cutoffs()
        self._cutoff_edited()

    def _manual_voltage_levels(self) -> tuple[float, float, float, float]:
        on_v = float(self.load_on_var.get())
        off_v = float(self.load_off_var.get())
        ovp_v = float(self.ovp_var.get())
        ocp_a = float(self.ocp_var.get())
        if on_v < 0.1 or off_v < 0.1:
            raise ValueError("Start voltage and turn off must be at least 0.100 V.")
        if on_v < off_v:
            raise ValueError("Start voltage must be at least the turn-off voltage.")
        if ovp_v <= 0 or ocp_a <= 0:
            raise ValueError("Over-voltage and over-current protection must be greater than 0.")
        return on_v, off_v, ovp_v, ocp_a

    def _send_voltage_levels(self) -> None:
        if not self.connected or self.running or self._busy() or self.session is None:
            return
        try:
            on_v, off_v, ovp_v, ocp_a = self._manual_voltage_levels()
        except ValueError as exc:
            self._set_status(str(exc), error=True)
            return
        self._set_status(
            f"Sending start {on_v:.3f} V, turn off {off_v:.3f} V, "
            f"over voltage {ovp_v:.3f} V, over current {ocp_a:.3f} A."
        )
        self._spawn(self._write_levels_worker, on_v, off_v, ovp_v, ocp_a)

    def _write_levels_worker(self, on_v: float, off_v: float, ovp_v: float, ocp_a: float) -> None:
        try:
            if self.session is None:
                raise RuntimeError("not connected")
            read_on, read_off = self.session.write_voltage_levels(on_v, off_v)
            read_ovp, read_ocp = self.session.write_protections(ovp_v, ocp_a)
        except Exception as exc:
            self.queue.put(("failed", f"Could not set the load limits: {exc}"))
            return
        self.queue.put(("load_limits", read_on, read_off, read_ovp, read_ocp))
        self.queue.put(
            (
                "status",
                f"Load start {read_on:.3f} V, turn off {read_off:.3f} V, "
                f"over voltage {read_ovp:.3f} V, over current {read_ocp:.3f} A.",
            )
        )

    def _push_voltage_gate(self) -> None:
        if not self.sync_gate_var.get() or not self.connected or self.running or self._busy():
            return
        if self.session is None:
            return
        try:
            cutoff = self._gate_cutoff()
        except ValueError:
            self._set_status("Voltage cutoff must be a number before it can be stored in the load.", error=True)
            return
        self._set_status("Storing the voltage limit in the load.")
        self._spawn(self._write_gate_worker, cutoff)

    def _write_gate_worker(self, cutoff: float | None) -> None:
        try:
            if self.session is None:
                raise RuntimeError("not connected")
            on_v, off_v = self.session.write_voltage_gate(cutoff)
            ovp_v, ocp_a = self.session.read_protections()
        except Exception as exc:
            self.queue.put(("failed", f"Could not set the load voltage limits: {exc}"))
            return
        self.queue.put(("load_limits", on_v, off_v, ovp_v, ocp_a))
        if cutoff is None:
            self.queue.put(("status", "Load voltage off set to 0.100 V."))
        else:
            self.queue.put(("status", f"Load turns itself off at {max(cutoff, 0.1):.3f} V."))

    def _refresh_voltage_gate(self) -> None:
        if self.session is None or not self.connected or self.running:
            return
        threading.Thread(target=self._read_gate_worker, daemon=True).start()

    def _read_gate_worker(self) -> None:
        try:
            if self.session is None:
                return
            on_v, off_v = self.session.read_voltage_gate()
            ovp_v, ocp_a = self.session.read_protections()
        except Exception as exc:
            self.queue.put(("status", f"Could not read the load limits: {exc}"))
            return
        self.queue.put(("load_limits", on_v, off_v, ovp_v, ocp_a))

    def _remember_resource(self, resource: str) -> None:
        text = resource.strip()
        if not text or text == RESOURCE_PROMPT:
            return
        try:
            save_saved_resource(self._settings_path, text)
        except OSError as exc:
            self._set_status(f"Could not save the resource: {exc.strerror or exc}", error=True)

    def _show_resource_menu(self, names: list[str]) -> None:
        menu = tk.Menu(self, tearoff=0)
        for name in names:
            menu.add_command(label=name, command=lambda resource=name: self._choose_resource(resource))
        button = self.select_resource_button
        x = button.winfo_rootx()
        y = button.winfo_rooty() + button.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _connection_settings(self) -> str | None:
        resource = self.resource_var.get().strip()
        if not resource or resource == RESOURCE_PROMPT:
            self._set_status("Click Select or enter a resource path.", error=True)
            return None
        return resource

    def _connect(self) -> None:
        if self._busy():
            return
        resource = self._connection_settings()
        if resource is None:
            messagebox.showwarning(
                "Resource",
                "Click Select or enter a resource path.",
                parent=self,
            )
            return
        self._set_link("connecting")
        self._set_status(f"Connecting to {resource}")
        self._spawn(self._connect_worker, resource)

    def _connect_worker(self, resource: str) -> None:
        self._retire_session()
        session = None
        try:
            session = ET54Load(resource)
            voltage = session.read_voltage()
            voltage_range, current_range = session.read_ranges()
            gate = session.read_voltage_gate()
            protections = session.read_protections()
        except Exception as exc:
            if session is not None:
                self._close_quietly(session)
            self.queue.put(("failed", f"Could not connect: {exc}"))
            return
        self.session = session
        self.queue.put(("connected", session.identity, voltage))
        self.queue.put(("device_ranges", voltage_range, current_range))
        self.queue.put(("load_limits", gate[0], gate[1], protections[0], protections[1]))

    def _set_ranges_now(self) -> None:
        if self.session is None or not self.connected:
            self._set_status("Connect to the load before setting ranges.", error=True)
            return
        if self.running or self._busy():
            return
        voltage_range, current_range = self._selected_ranges()
        self._set_status(
            f"Setting voltage range {voltage_range.capitalize()}, "
            f"current range {current_range.capitalize()}."
        )
        self._spawn(self._set_ranges_worker, voltage_range, current_range)

    def _set_ranges_worker(self, voltage_range: str, current_range: str) -> None:
        try:
            if self.session is None:
                raise RuntimeError("not connected")
            self.session.apply_ranges(voltage_range, current_range)
            gate = self.session.read_voltage_gate()
            protections = self.session.read_protections()
        except Exception as exc:
            self.queue.put(("failed", f"Could not set ranges: {exc}"))
            return
        self.queue.put(("load_limits", gate[0], gate[1], protections[0], protections[1]))
        self.queue.put(
            (
                "status",
                f"Ranges set: voltage {voltage_range.capitalize()}, "
                f"current {current_range.capitalize()}. "
                "The load reset over-voltage and over-current to that range's preset.",
            )
        )

    def _disconnect(self) -> None:
        if self.running or self.session is None or self._busy():
            return
        self._spawn(self._disconnect_worker)

    def _disconnect_worker(self) -> None:
        session = self.session
        self.session = None
        try:
            if session is not None:
                session.off()
                session.close()
        except Exception as exc:
            self.queue.put(("failed", f"Could not disconnect: {exc}"))
            return
        self.queue.put(("disconnected",))

    def _enabled_float(self, enabled: tk.BooleanVar, variable: tk.StringVar) -> float | None:
        if not enabled.get():
            return None
        return float(variable.get())

    def _config_from_form(self) -> TestConfig | None:
        mode = self._mode_code()
        try:
            interval = float(self.interval_var.get())
            current = float(self.current_var.get()) if mode in ("CC", "CCCV") else None
            voltage_set = float(self.voltage_set_var.get()) if mode in ("CV", "CCCV") else None
            resistance = float(self.resistance_var.get()) if mode == "CR" else None
            power = float(self.power_var.get()) if mode == "CP" else None
            cutoff_voltage = self._enabled_float(self.cutoff_voltage_on, self.cutoff_voltage_var)
            cutoff_current = self._enabled_float(self.cutoff_current_on, self.cutoff_current_var)
            cutoff_time = self._enabled_float(self.cutoff_time_on, self.cutoff_time_var)
            cutoff_ah = self._enabled_float(self.cutoff_ah_on, self.cutoff_ah_var)
            cutoff_wh = self._enabled_float(self.cutoff_wh_on, self.cutoff_wh_var)
            ovp_text = self.ovp_var.get().strip()
            ocp_text = self.ocp_var.get().strip()
            ovp_v = float(ovp_text) if ovp_text else None
            ocp_a = float(ocp_text) if ocp_text else None
        except ValueError:
            messagebox.showerror(
                "Check the test settings",
                "Setpoints, cutoffs, protections, and the sample interval must be numbers.",
                parent=self,
            )
            return None
        wait = float(self.wait_var.get().split()[0])
        voltage_range, current_range = self._selected_ranges()
        config = TestConfig(
            mode=mode,
            current_a=current,
            voltage_set_v=voltage_set,
            resistance_ohm=resistance,
            power_w=power,
            sample_interval_s=interval,
            ocv_enabled=bool(self.ocv_var.get()),
            ocv_wait_s=wait,
            csv_path=self.csv_var.get().strip(),
            csv_enabled=bool(self.csv_enabled_var.get()),
            voltage_range=voltage_range,
            current_range=current_range,
            cutoff_voltage_v=cutoff_voltage,
            cutoff_current_a=cutoff_current,
            cutoff_time_s=cutoff_time,
            cutoff_discharge_ah=cutoff_ah,
            cutoff_energy_wh=cutoff_wh,
            sync_voltage_gate=bool(self.sync_gate_var.get()),
            ovp_v=ovp_v,
            ocp_a=ocp_a,
        )
        error = validate_config(config)
        if error:
            messagebox.showerror("Check the test settings", error, parent=self)
            return None
        path = Path(config.csv_path).expanduser()
        if config.csv_enabled and path.exists():
            overwrite = messagebox.askyesno(
                "Overwrite log",
                f"{path} already exists. Overwrite it?",
                parent=self,
            )
            if not overwrite:
                return None
        return config

    def _start(self) -> None:
        if self.running or self._busy():
            return
        config = self._config_from_form()
        if config is None:
            return
        self._save_form_settings()
        resource = self._connection_settings()
        if resource is None:
            return
        self.samples.clear()
        self._clear_chart()
        self.cutoff_voltage = config.cutoff_voltage_v
        self.cutoff_current = config.cutoff_current_a
        self.cutoff_ah = config.cutoff_discharge_ah
        self.finished_elapsed = None
        self._draw_cutoff()
        self._apply_voltage_legend(config.ocv_enabled)
        self.canvas.draw_idle()
        for name in self.readout_values:
            self.readout_values[name].configure(text="—")
        self.stop_event.clear()
        self.running = True
        self.run_started = time.perf_counter()
        self._sync_buttons()
        if config.csv_enabled:
            self._set_status(f"Starting. Logging to {config.csv_path}")
        else:
            self._set_status("Starting. CSV logging is off.")
        self._spawn(self._test_worker, resource, config)

    def _test_worker(self, resource: str, config: TestConfig) -> None:
        try:
            if self.session is None:
                self.session = ET54Load(resource)
                self.session.off()
                voltage = self.session.read_voltage()
                self.queue.put(("connected", self.session.identity, voltage))
            test = CapacityTest(
                self.session,
                config,
                self.stop_event,
                on_sample=lambda sample: self.queue.put(("sample", sample)),
                on_status=lambda text: self.queue.put(("status", text)),
            )
            result = test.run()
        except Exception as exc:
            self.queue.put(("failed", str(exc)))
            return
        if test.off_error is not None:
            self.queue.put(("failed", f"The load may still be on: {test.off_error}"))
            return
        self.queue.put(("finished", result))

    def _stop(self) -> None:
        if not self.running:
            return
        self.stop_event.set()
        self._set_status("Stopping. Turning the load off.")

    def _spawn(self, target, *args) -> None:
        self.worker = threading.Thread(target=target, args=args, daemon=False)
        self.worker.start()
        self._sync_buttons()

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _poll(self) -> None:
        added = False
        try:
            while True:
                message = self.queue.get_nowait()
                if message[0] == "sample":
                    self.samples.append(message[1])
                    added = True
                else:
                    self._handle(message)
        except queue.Empty:
            pass
        if added and self.samples:
            self._show_sample(self.samples[-1])
            self._redraw()
        self._tick_elapsed()
        self.after(100, self._poll)

    def _handle(self, message) -> None:
        kind = message[0]
        if kind == "connected":
            _kind, identity, voltage = message
            self.connected = True
            self.resting_v = float(voltage)
            self.readout_values["Voltage"].configure(text=f"{self.resting_v:.3f}")
            self._update_power_hint()
            self._set_link("connected")
            self._remember_resource(self.resource_var.get())
            self._set_status(f"Connected  {identity}  resting {self.resting_v:.3f} V")
            self._sync_buttons()
        elif kind == "disconnected":
            self.connected = False
            self.resting_v = None
            self._update_power_hint()
            self._set_link("disconnected")
            self._clear_voltage_gate()
            self._set_status("Disconnected")
            self._sync_buttons()
        elif kind == "voltage":
            if not self.connected or self.running:
                return
            self.resting_v = float(message[1])
            self.readout_values["Voltage"].configure(text=f"{self.resting_v:.3f}")
            self._update_power_hint()
            # A posted menu grabs input. Flush the redraw so the number paints
            # while that menu is still open.
            self.update_idletasks()
        elif kind == "heartbeat_lost":
            if not self.connected:
                return
            self.connected = False
            self._set_link("lost")
            self._clear_voltage_gate()
            self._set_status(f"Connection lost: {message[1]}", error=True)
            self._sync_buttons()
            if self.running:
                self.stop_event.set()
            else:
                self._retire_session()
        elif kind == "device_ranges":
            self.voltage_range_var.set(str(message[1]).capitalize())
            self.current_range_var.set(str(message[2]).capitalize())
        elif kind == "load_limits":
            self._show_voltage_gate(
                float(message[1]),
                float(message[2]),
                float(message[3]),
                float(message[4]),
            )
        elif kind == "resources":
            self._listing_resources = False
            self._sync_buttons()
            names = list(message[1])
            if not names:
                self._set_status("No instruments found.")
                return
            found = "instrument" if len(names) == 1 else "instruments"
            self._set_status(f"Found {len(names)} {found}.")
            if not self._closing:
                # Post after this poll schedules the next one. The menu's own
                # event loop then keeps the voltage heartbeat running.
                self.after_idle(lambda resources=list(names): self._show_resource_menu(resources))
        elif kind == "resources_failed":
            self._listing_resources = False
            self._sync_buttons()
            self._set_status(message[1], error=True)
        elif kind == "status":
            self._set_status(message[1])
            self._sync_buttons()
        elif kind == "finished":
            self._finish(message[1])
        elif kind == "failed":
            self.running = False
            self.run_started = None
            if self.link.cget("text").endswith("Connecting"):
                self._set_link("disconnected")
            if not self.connected:
                self._retire_session()
            self._set_status(message[1], error=True)
            self._sync_buttons()
            messagebox.showerror("Load", message[1], parent=self)

    def _finish(self, result: TestResult) -> None:
        self.running = False
        if self.samples:
            self.finished_elapsed = self.samples[-1].elapsed_s
        self.run_started = None
        self._tick_elapsed()
        error = result.reason in {"power_limit", "already_at_cutoff", "voltage_range"}
        if not self.connected:
            self._retire_session()
        self._set_status(result.message, error=error)
        self._sync_buttons()
        if error:
            messagebox.showwarning("Capacity test", result.message, parent=self)

    def _tick_elapsed(self) -> None:
        if self.run_started is not None:
            elapsed = time.perf_counter() - self.run_started
        elif self.finished_elapsed is not None:
            elapsed = self.finished_elapsed
        else:
            return
        self.readout_values["Elapsed"].configure(text=_format_elapsed(elapsed))

    def _show_sample(self, sample: Sample) -> None:
        self.readout_values["Voltage"].configure(text=f"{sample.voltage_v:.3f}")
        self.readout_values["Current"].configure(text=f"{sample.current_a:.3f}")
        self.readout_values["Discharge"].configure(text=f"{sample.capacity_ah:.3f}")
        self.readout_values["Energy"].configure(text=f"{sample.energy_wh:.1f}")
        self.finished_elapsed = sample.elapsed_s

    def _apply_voltage_legend(self, open_circuit: bool) -> None:
        self.ocv_mode = open_circuit
        self._loaded_line.set_visible(open_circuit)
        if open_circuit:
            self._voltage_line.set_label("Open circuit")
            self._loaded_line.set_label("Loaded")
            self.ax_v.legend(frameon=False, fontsize=8, loc="upper right")
        else:
            self._voltage_line.set_label("Voltage")
            legend = self.ax_v.get_legend()
            if legend is not None:
                legend.remove()

    def _reset_chart_limits(self) -> None:
        self._x_full = (0.0, 1.0)
        self._x_view = (0.0, 1.0)
        self._x_follow = True
        self.ax_v.set_xlim(0, 1)
        voltage_top = 5.0
        if getattr(self, "cutoff_voltage", None):
            voltage_top = max(voltage_top, float(self.cutoff_voltage) * 1.05)
        self.ax_v.set_ylim(0, voltage_top)
        self.ax_i.set_ylim(0, 1)
        self.ax_q.set_ylim(0, 1)
        if getattr(self, "time_bar", None) is not None:
            self.time_bar.set_bounds(0.0, 1.0, 0.0, 1.0)
            self._set_time_labels(0.0, 1.0)

    def _clear_chart(self) -> None:
        for line in (self._voltage_line, self._loaded_line, self._current_line, self._charge_line):
            line.set_data([], [])
        for line in self._cutoff_lines:
            line.remove()
        self._cutoff_lines = []
        self._reset_chart_limits()
        self.canvas.draw_idle()

    def _draw_cutoff(self) -> None:
        for line in self._cutoff_lines:
            line.remove()
        self._cutoff_lines = []
        guides = (
            (self.ax_v, self.cutoff_voltage, "Cutoff"),
            (self.ax_i, self.cutoff_current, None),
            (self.ax_q, self.cutoff_ah, None),
        )
        for axis, value, label in guides:
            if value is None:
                continue
            self._cutoff_lines.append(
                axis.axhline(
                    value,
                    color=CUTOFF_COLOR,
                    linestyle="--",
                    linewidth=0.9,
                    label=label or "_nolegend_",
                )
            )

    def _redraw(self) -> None:
        view = _display_samples(self.samples)
        minutes = [sample.elapsed_s / 60.0 for sample in view]
        self._voltage_line.set_data(minutes, [sample.voltage_v for sample in view])
        if self.ocv_mode:
            self._loaded_line.set_data(minutes, [sample.loaded_voltage_v for sample in view])
        current_points = [sample for sample in view if sample.source != "resting"] or view
        current_minutes = [sample.elapsed_s / 60.0 for sample in current_points]
        self._current_line.set_data(current_minutes, [sample.current_a for sample in current_points])
        self._charge_line.set_data(minutes, [sample.capacity_ah for sample in view])
        voltage_values = [sample.voltage_v for sample in view]
        current_values = [sample.current_a for sample in current_points]
        discharge_values = [sample.capacity_ah for sample in view]
        full_end = max(minutes) if minutes else 0.0
        self._update_time_extent(full_end)
        start, end = self._x_view
        voltage_window = _window_values(minutes, voltage_values, start, end)
        current_window = _window_values(current_minutes, current_values, start, end)
        discharge_window = _window_values(minutes, discharge_values, start, end)
        if self._shows_full_span():
            if self.cutoff_voltage is not None:
                voltage_window.append(self.cutoff_voltage)
            if self.cutoff_current is not None:
                current_window.append(self.cutoff_current)
            if self.cutoff_ah is not None:
                discharge_window.append(self.cutoff_ah)
        _fit_y(self.ax_v, voltage_window)
        _fit_y(self.ax_i, current_window)
        _fit_y(self.ax_q, discharge_window, floor=0.0)
        self._apply_time_view()

    def _pinned_to_end(self, end: float) -> bool:
        span = max(self._x_view[1] - self._x_view[0], 1.0 / 60.0)
        return self._x_full[1] - end <= max(span * 0.01, 1.0 / 60.0)

    def _shows_full_span(self) -> bool:
        return self._x_view[0] <= 1e-6 and self._pinned_to_end(self._x_view[1])

    def _update_time_extent(self, full_end: float) -> None:
        full_end = max(full_end, 1.0 / 60.0)
        old_end = self._x_full[1]
        start, end = self._x_view
        span = max(end - start, 1.0 / 60.0)
        if self._x_follow:
            covers_all = start <= 1e-6 and abs(end - old_end) <= max(old_end * 0.01, 1.0 / 60.0)
            if covers_all:
                start, end = 0.0, full_end
            else:
                end = full_end
                start = max(0.0, end - span)
        else:
            end = min(max(end, start + 1.0 / 60.0), full_end)
            start = min(max(0.0, start), end - 1.0 / 60.0)
        self._x_full = (0.0, full_end)
        self._x_view = (start, end)

    def _apply_time_view(self) -> None:
        start, end = self._x_view
        self.ax_v.set_xlim(start, end)
        self.time_bar.set_bounds(self._x_full[0], self._x_full[1], start, end)
        self._set_time_labels(start, end)
        self.canvas.draw_idle()

    def _set_time_labels(self, start: float, end: float) -> None:
        self.x_start_label.configure(text=_format_elapsed(start * 60.0))
        self.x_end_label.configure(text=_format_elapsed(end * 60.0))

    def _on_time_bar(self, start: float, end: float) -> None:
        self._x_view = (start, end)
        self._x_follow = self._pinned_to_end(end)
        self._refit_y_to_view()
        self.ax_v.set_xlim(start, end)
        self._set_time_labels(start, end)
        self.canvas.draw_idle()

    def _refit_y_to_view(self) -> None:
        if not self.samples:
            return
        view = _display_samples(self.samples)
        minutes = [sample.elapsed_s / 60.0 for sample in view]
        current_points = [sample for sample in view if sample.source != "resting"] or view
        current_minutes = [sample.elapsed_s / 60.0 for sample in current_points]
        start, end = self._x_view
        voltage_window = _window_values(minutes, [sample.voltage_v for sample in view], start, end)
        current_window = _window_values(current_minutes, [sample.current_a for sample in current_points], start, end)
        discharge_window = _window_values(minutes, [sample.capacity_ah for sample in view], start, end)
        if self._shows_full_span():
            if self.cutoff_voltage is not None:
                voltage_window.append(self.cutoff_voltage)
            if self.cutoff_current is not None:
                current_window.append(self.cutoff_current)
            if self.cutoff_ah is not None:
                discharge_window.append(self.cutoff_ah)
        _fit_y(self.ax_v, voltage_window)
        _fit_y(self.ax_i, current_window)
        _fit_y(self.ax_q, discharge_window, floor=0.0)

    def _shift_scroll_time(self, event) -> str:
        if event.num == 4 or getattr(event, "delta", 0) > 0:
            self._pan_time(-0.15)
        else:
            self._pan_time(0.15)
        return "break"

    def _pan_time(self, fraction: float) -> None:
        start, end = self._x_view
        full_lo, full_hi = self._x_full
        span = end - start
        if span >= (full_hi - full_lo) - 1e-9:
            return
        delta = fraction * span
        start += delta
        end += delta
        if start < full_lo:
            end += full_lo - start
            start = full_lo
        if end > full_hi:
            start -= end - full_hi
            end = full_hi
        self._on_time_bar(start, end)
        self.time_bar.set_bounds(full_lo, full_hi, start, end)

    def _on_close(self) -> None:
        if self.running:
            stop = messagebox.askokcancel(
                "Stop discharge",
                "Stop the discharge and close? The log already written is kept.",
                parent=self,
            )
            if not stop:
                return
            self._closing = True
            self.stop_event.set()
            if self.worker is not None:
                self.worker.join(timeout=10)
        elif self.worker is not None and self.worker.is_alive():
            self._closing = True
            self.worker.join(timeout=5)
        else:
            self._closing = True
        session = self.session
        self.session = None
        if session is not None:
            try:
                session.off()
                session.close()
            except Exception:
                pass
        self.destroy()


def _display_samples(samples: list[Sample]) -> list[Sample]:
    step = max(1, len(samples) // 3000)
    view = samples[::step]
    if view and view[-1] is not samples[-1]:
        view = view + [samples[-1]]
    return view


class TimeRangeBar(tk.Canvas):
    """Start and end handles for the shared chart time axis."""

    def __init__(self, master, command):
        super().__init__(
            master,
            height=28,
            bg=BG,
            highlightthickness=0,
            cursor="sb_h_double_arrow",
        )
        self.command = command
        self.full = (0.0, 1.0)
        self.start = 0.0
        self.end = 1.0
        self._drag = None
        self._origin = (0, 0.0, 0.0)
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<ButtonPress-1>", self._press)

    def set_bounds(self, full_lo: float, full_hi: float, start: float, end: float) -> None:
        if full_hi <= full_lo:
            full_hi = full_lo + 1.0 / 60.0
        self.full = (full_lo, full_hi)
        self.start = start
        self.end = end
        self._draw()

    def _min_span(self) -> float:
        span = self.full[1] - self.full[0]
        if span <= 1.0 / 60.0:
            return span
        return 1.0 / 60.0

    def _pad(self) -> int:
        return 12

    def _x_of(self, value: float) -> float:
        lo, hi = self.full
        width = max(self.winfo_width(), self._pad() * 2 + 2)
        usable = width - self._pad() * 2
        return self._pad() + (value - lo) / (hi - lo) * usable

    def _value_at(self, x: float) -> float:
        lo, hi = self.full
        width = max(self.winfo_width(), self._pad() * 2 + 2)
        usable = width - self._pad() * 2
        fraction = min(1.0, max(0.0, (x - self._pad()) / usable))
        return lo + fraction * (hi - lo)

    def _draw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 2)
        mid = 14
        start_x = self._x_of(self.start)
        end_x = self._x_of(self.end)
        self.create_line(self._pad(), mid, width - self._pad(), mid, fill=LINE, width=4, capstyle="round")
        self.create_line(start_x, mid, end_x, mid, fill=TEAL, width=4, capstyle="round")
        for position in (start_x, end_x):
            self.create_oval(position - 6, mid - 6, position + 6, mid + 6, fill="#ffffff", outline=TEAL, width=2)

    def _hit(self, x: float) -> str | None:
        start_x = self._x_of(self.start)
        end_x = self._x_of(self.end)
        if abs(x - start_x) <= 10 and abs(x - start_x) <= abs(x - end_x):
            return "start"
        if abs(x - end_x) <= 10:
            return "end"
        if start_x < x < end_x:
            return "span"
        return None

    def _press(self, event) -> None:
        kind = self._hit(event.x)
        if kind is None:
            return
        self._drag = kind
        self._origin = (event.x, self.start, self.end)
        self.bind_all("<B1-Motion>", self._motion)
        self.bind_all("<ButtonRelease-1>", self._release)

    def _motion(self, event) -> None:
        if self._drag is None:
            return
        origin_x, origin_start, origin_end = self._origin
        delta = self._value_at(event.x) - self._value_at(origin_x)
        minimum = self._min_span()
        if self._drag == "start":
            start = min(origin_start + delta, origin_end - minimum)
            start = max(self.full[0], start)
            end = origin_end
        elif self._drag == "end":
            end = max(origin_end + delta, origin_start + minimum)
            end = min(self.full[1], end)
            start = origin_start
        else:
            start = origin_start + delta
            end = origin_end + delta
            if start < self.full[0]:
                end += self.full[0] - start
                start = self.full[0]
            if end > self.full[1]:
                start -= end - self.full[1]
                end = self.full[1]
        self.start = start
        self.end = end
        self._draw()
        self.command(start, end)

    def _release(self, _event) -> None:
        self._drag = None
        self.unbind_all("<B1-Motion>")
        self.unbind_all("<ButtonRelease-1>")


def _window_values(minutes: list[float], values: list[float], start: float, end: float) -> list[float]:
    chosen: list[float] = []
    before: float | None = None
    after: float | None = None
    for minute, value in zip(minutes, values):
        if minute < start:
            before = value
        elif minute > end:
            if after is None:
                after = value
        else:
            chosen.append(value)
    if before is not None:
        chosen.insert(0, before)
    if after is not None:
        chosen.append(after)
    return chosen or list(values)


def _fit_y(axis, values: list[float], floor: float | None = None) -> None:
    if not values:
        return
    ymin = min(values)
    ymax = max(values)
    if floor is not None:
        ymin = min(ymin, floor)
    span = ymax - ymin
    pad = max(span * 0.1, abs(ymax) * 0.02, 0.05)
    axis.set_ylim(ymin - (0 if floor == 0 and ymin >= 0 else pad), ymax + pad)


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


def main() -> None:
    app = CapacityApp()
    app.mainloop()


if __name__ == "__main__":
    main()
