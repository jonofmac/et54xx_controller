#!/usr/bin/env python3
"""Discharge one lithium-ion cell on an ET54-series electronic load.

The load runs in constant-current mode on channel 1. Every check interval the
channel is turned off, allowed to settle, and read open-circuit. The first time
that reading is within 10 mV of the cutoff, the script waits the close interval
(3 s by default) and reads again. Later checks wait that long before reading.
The channel stays off once the open-circuit voltage is at or below the cutoff
(the target voltage plus an offset, -5 mV by default). Otherwise the load turns
back on and the cycle repeats. Once two open-circuit readings have been taken
after discharging, status lines estimate the time left from the average drop
between the first of those readings and the latest.

Example:
    python discharge.py --resource ASRL/dev/ttyUSB0::INSTR
    python discharge.py --current 0.5 --voltage 3.700 --interval 30
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import datetime

from ET54 import ET54

DEFAULT_CURRENT_A = 1.0
DEFAULT_TARGET_V = 3.900
DEFAULT_OFFSET_V = -0.005
DEFAULT_INTERVAL_S = 30.0
DEFAULT_SETTLE_S = 1.0
DEFAULT_CLOSE_WAIT_S = 3.0
NEAR_WINDOW_V = 0.010
DEFAULT_STATUS_S = 10.0
DEFAULT_RESOURCE = "ASRL/dev/ttyUSB0::INSTR"
# Loaded voltage this far below the target means the cell is sagging hard
# or the connection is wrong. Stop rather than keep pulling current.
DEFAULT_MIN_LOADED_V = 2.500


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discharge a single Li-ion cell to an open-circuit voltage.",
    )
    parser.add_argument(
        "--resource",
        default=DEFAULT_RESOURCE,
        help=f"PyVISA resource (default: {DEFAULT_RESOURCE})",
    )
    parser.add_argument(
        "--current",
        type=float,
        default=DEFAULT_CURRENT_A,
        help=f"constant-current load in amps (default: {DEFAULT_CURRENT_A})",
    )
    parser.add_argument(
        "--voltage",
        type=float,
        default=DEFAULT_TARGET_V,
        help=(
            "target open-circuit voltage in volts; "
            f"the cutoff is this plus --offset (default: {DEFAULT_TARGET_V:.3f})"
        ),
    )
    parser.add_argument(
        "--offset",
        type=float,
        default=DEFAULT_OFFSET_V,
        help=(
            "volts added to --voltage to form the open-circuit cutoff; "
            "stop at or below that voltage (default: -0.005)"
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_S,
        help=f"seconds of discharge between open-circuit checks (default: {DEFAULT_INTERVAL_S:g})",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=DEFAULT_SETTLE_S,
        help=(
            "seconds to wait after turning the load off before reading, "
            f"until within 10 mV of the cutoff (default: {DEFAULT_SETTLE_S:g})"
        ),
    )
    parser.add_argument(
        "--close-wait",
        type=float,
        default=DEFAULT_CLOSE_WAIT_S,
        help=(
            "seconds to wait before an open-circuit reading once within 10 mV "
            "of the cutoff. The first time that window is entered, this wait "
            f"is added after the normal settle and the voltage is read again "
            f"(default: {DEFAULT_CLOSE_WAIT_S:g})"
        ),
    )
    parser.add_argument(
        "--status-interval",
        type=float,
        default=DEFAULT_STATUS_S,
        help=f"seconds between under-load status lines (default: {DEFAULT_STATUS_S:g})",
    )
    parser.add_argument(
        "--min-loaded-voltage",
        type=float,
        default=DEFAULT_MIN_LOADED_V,
        help="abort if loaded voltage falls to this level (default: 2.500)",
    )
    parser.add_argument("--baudrate", type=int, default=9600)
    parser.add_argument(
        "--delay",
        type=float,
        default=None,
        help="ET54 post-command delay in seconds (library default if omitted)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help='model id override, e.g. "ET5407A+", when *IDN? is not useful',
    )
    return parser.parse_args()


def log(message: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp}  {message}", flush=True)


def format_elapsed(seconds: float) -> str:
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


def format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "eta --"
    if seconds > 99 * 3600:
        return "eta >99:00:00"
    return f"eta {format_elapsed(seconds)}"


class EtaEstimate:
    """Time to the cutoff from the average open-circuit drop so far.

    The first sample is the first open-circuit reading after discharging
    starts. Each later reading replaces the end of that span, so the rate
    is the drop over the whole discharge, not just the last minute.
    """

    def __init__(self) -> None:
        self.first_voltage: float | None = None
        self.first_at: float | None = None
        self.latest_voltage: float | None = None
        self.latest_at: float | None = None

    def note_open_circuit(self, now: float, voltage: float) -> None:
        if self.first_voltage is None:
            self.first_voltage = voltage
            self.first_at = now
        self.latest_voltage = voltage
        self.latest_at = now

    def seconds_remaining(self, cutoff: float, now: float | None = None) -> float | None:
        if (
            self.first_voltage is None
            or self.first_at is None
            or self.latest_voltage is None
            or self.latest_at is None
            or self.latest_at <= self.first_at
        ):
            return None
        dt = self.latest_at - self.first_at
        drop = self.first_voltage - self.latest_voltage
        if drop <= 0:
            return None
        rate = drop / dt
        if now is None:
            now = self.latest_at
        elapsed = max(0.0, now - self.latest_at)
        remaining = self.latest_voltage - rate * elapsed - cutoff
        if remaining <= 0:
            return 0.0
        return remaining / rate


def cutoff_voltage(target: float, offset: float) -> float:
    return target + offset


def at_stop(voltage: float, cutoff: float) -> bool:
    return voltage <= cutoff


def near_cutoff(voltage: float, cutoff: float) -> bool:
    return abs(voltage - cutoff) <= NEAR_WINDOW_V


def measure_open_circuit(
    el: ET54,
    cutoff: float,
    close: bool,
    settle: float,
    close_wait: float,
) -> tuple[float, bool]:
    """Read open-circuit voltage, re-reading the first time it is near the cutoff.

    Later calls wait `close_wait` instead of `settle` before the single read.
    """
    time.sleep(close_wait if close else settle)
    voltage = read_voltage(el)
    if close or not near_cutoff(voltage, cutoff):
        return voltage, close
    log(
        f"open-circuit {voltage:.4f} V is within {NEAR_WINDOW_V * 1000:.0f} mV "
        f"of cutoff; waiting an additional {close_wait:g} s and re-reading"
    )
    time.sleep(close_wait)
    return read_voltage(el), True


def connect(args: argparse.Namespace) -> ET54:
    kwargs: dict = {"baudrate": args.baudrate}
    if args.delay is not None:
        kwargs["delay"] = args.delay
    if args.model is not None:
        kwargs["model"] = args.model
    return ET54(args.resource, **kwargs)


def read_voltage(el: ET54) -> float:
    return float(el.ch1.read_voltage())


def read_load(el: ET54) -> tuple[float, float]:
    voltage = float(el.ch1.read_voltage())
    current = float(el.ch1.read_current())
    return voltage, current


def discharge_for(
    el: ET54,
    seconds: float,
    status_interval: float,
    min_loaded: float,
    started: float,
    milliamp_hours: list[float],
    estimate: EtaEstimate,
    cutoff: float,
) -> str | None:
    """Keep the load on for `seconds`, printing status. Return an abort reason, if any."""
    deadline = time.monotonic() + seconds
    next_status = time.monotonic() + min(status_interval, seconds)
    last_mark = time.monotonic()

    while True:
        now = time.monotonic()
        if now >= next_status or now >= deadline:
            voltage, current = read_load(el)
            milliamp_hours[0] += current * (now - last_mark) * 1000.0 / 3600.0
            last_mark = now
            log(
                f"discharging  {voltage:7.4f} V  {current:6.3f} A  "
                f"{voltage * current:5.2f} W  "
                f"elapsed {format_elapsed(now - started)}  "
                f"~{milliamp_hours[0]:.0f} mAh  "
                f"{format_eta(estimate.seconds_remaining(cutoff, now))}"
            )
            if voltage <= min_loaded:
                return (
                    f"loaded voltage {voltage:.4f} V is at or below "
                    f"{min_loaded:.3f} V; leaving the load off"
                )
            if now >= deadline:
                return None
            next_status += status_interval
            continue

        nap = min(1.0, next_status - now, deadline - now)
        if nap > 0:
            time.sleep(nap)


def turn_off(el: ET54) -> None:
    el.ch1.off()
    log("channel 1 off")


def main() -> int:
    args = parse_args()
    if args.current <= 0:
        print("error: --current must be positive", file=sys.stderr)
        return 2
    if args.voltage <= 0:
        print("error: --voltage must be positive", file=sys.stderr)
        return 2
    if args.interval <= 0 or args.settle < 0 or args.close_wait < 0 or args.status_interval <= 0:
        print("error: intervals must be positive (--settle and --close-wait may be 0)", file=sys.stderr)
        return 2

    el: ET54 | None = None
    started = time.monotonic()
    milliamp_hours = [0.0]
    stop_reason = "stopped"
    cutoff = cutoff_voltage(args.voltage, args.offset)
    close = False
    estimate = EtaEstimate()

    def request_stop(signum, _frame) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGHUP, request_stop)

    try:
        log(f"connecting to {args.resource}")
        el = connect(args)
        log("connected")

        # A single Li-ion sits near 4.2 V. These limits turn the input off if a
        # higher-voltage pack is connected or the current runs away.
        el.ch1.OVP = 4.45
        el.ch1.OCP = max(args.current * 1.5, args.current + 0.25)
        el.ch1.OPP = max(20.0, args.current * 6.0)
        el.ch1.Vrange = "low"
        el.ch1.Crange = "high"
        el.ch1.CC_mode(args.current)
        el.ch1.off()

        open_circuit, close = measure_open_circuit(
            el, cutoff, close, args.settle, args.close_wait
        )
        log(
            f"open-circuit {open_circuit:.4f} V  "
            f"cutoff {cutoff:.4f} V "
            f"(target {args.voltage:.3f} V {args.offset * 1000:+.1f} mV)  "
            f"load {args.current:.3f} A"
        )
        if at_stop(open_circuit, cutoff):
            stop_reason = "already at cutoff"
            log("already at or below the cutoff; channel stays off")
            return 0

        el.ch1.on()
        log(
            f"load on  checking open-circuit voltage every {args.interval:g} s "
            f"(settle {args.settle:g} s, {args.close_wait:g} s within 10 mV of cutoff)"
        )

        while True:
            abort = discharge_for(
                el,
                args.interval,
                args.status_interval,
                args.min_loaded_voltage,
                started,
                milliamp_hours,
                estimate,
                cutoff,
            )
            if abort is not None:
                stop_reason = "aborted on loaded voltage"
                el.ch1.off()
                log(abort)
                return 1

            el.ch1.off()
            if close:
                log(f"load off, waiting {args.close_wait:g} s before open-circuit reading")
            else:
                log("load off, reading open-circuit voltage")
            open_circuit, close = measure_open_circuit(
                el, cutoff, close, args.settle, args.close_wait
            )
            estimate.note_open_circuit(time.monotonic(), open_circuit)
            delta_mv = (open_circuit - cutoff) * 1000.0
            log(
                f"open-circuit {open_circuit:.4f} V  "
                f"{delta_mv:+.1f} mV from cutoff {cutoff:.4f} V  "
                f"{format_eta(estimate.seconds_remaining(cutoff))}"
            )

            if at_stop(open_circuit, cutoff):
                stop_reason = "cutoff reached"
                log("at or below cutoff; channel stays off")
                return 0

            el.ch1.on()
            log("above cutoff; load back on")

    except KeyboardInterrupt:
        stop_reason = "interrupted"
        log("interrupted; turning the load off")
        return 130
    except Exception:
        stop_reason = "error"
        raise
    finally:
        if el is not None:
            try:
                turn_off(el)
            except Exception as exc:
                log(f"failed to turn channel 1 off: {exc}")
        elapsed = time.monotonic() - started
        log(
            f"{stop_reason}  elapsed {format_elapsed(elapsed)}  "
            f"~{milliamp_hours[0]:.0f} mAh removed"
        )


if __name__ == "__main__":
    sys.exit(main())
