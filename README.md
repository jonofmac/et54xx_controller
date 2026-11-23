# Battery discharge

Control software for an East Tester ET54-series electronic load. `et54xx_app.py` is the windowed program for pack capacity tests. `et54xx_cli.py` is the same discharge from a terminal: it edits the saved settings, logs CSV, and shows a live status page.

## Software requirements

- Python 3.10 or newer (the ET54 library requires Python below 4)
- [ET54](https://github.com/philpagel/ET54.py) 0.3.1 or newer, which brings in `pyvisa`, `pyvisa-py`, and `pyserial`
- `matplotlib` 3.8 or newer (charts)
- Tk (the standard `tkinter` module). It is included with the usual Windows and macOS Python installers. On Linux, install the matching Tk package if `import tkinter` fails (Arch: `tk`; Debian/Ubuntu: `python3-tk`)

`pyvisa-py` is the VISA backend. The instrument’s serial baud must be **9600**, which is the ET54 library default.

The command-line program does not import Tk or matplotlib. It needs the ET54 library and PyVISA, which the install step below provides.

Tk comes from the system Python. Install that package before creating the virtual environment, because a venv does not bundle it.

## Install

From the project directory, create a virtual environment and install into that environment. `requirements.txt` supplies matplotlib. The ET54 library is installed from its repository and brings in PyVISA, pyvisa-py, and pyserial.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pip install "ET54 @ git+https://github.com/philpagel/ET54.py"
```

On Windows, activate with `.venv\Scripts\activate` instead of `source .venv/bin/activate`.

## Run

Open a console in the project directory, activate the environment, and start the app with that environment’s Python:

```bash
source .venv/bin/activate
python et54xx_app.py
```

A new console does not keep the environment active, so run the `source` command again each time before `python et54xx_app.py`. A double-click does not activate `.venv`, so start the app from the console.

The file can also be started as a program once it is marked executable. Do that from a console where `.venv` is already active, so `./et54xx_app.py` uses the environment’s Python:

```bash
chmod +x et54xx_app.py
./et54xx_app.py
```

`et54xx_app.py` is the pack-capacity application. `et54xx_cli.py` is the command-line program. `capacity.py` is the discharge engine both use. `tests/test_capacity.py` and `tests/test_cli.py` check that engine and the command-line screens without an instrument. Run the app with `.venv` active. The checks are:

```bash
python -m unittest discover -s tests -t .
```

**ET54xx Device Limits** holds the start voltage, turn-off voltage, over-voltage protection, and over-current protection stored in the instrument. Edit the boxes and press **Send** to write them. **Match voltage cutoff** fills the start and turn-off boxes from the voltage stop and grays those two out. Current, time, amp-hour, and energy stops are enforced by this program. The load can stop on time, capacity, or energy only in its own battery-test mode, which this program does not use.

While the UI is connected it asks the load for its identity about every 3 seconds. The indicator at the top shows **Connected**, **Connecting**, **Disconnected**, or **No response**. If a running test loses the instrument, the load is turned off and the test stops.

## Command line

`et54xx_cli.py` reads and writes the same `settings.json` as the window. The discharge mode, setpoints, cutoffs, **Match voltage cutoff**, **Write CSV**, and the resource are shared. The command line also remembers the sample interval, the CSV path, open-circuit checks, and the voltage and current ranges. The window does not load those four; it keeps the interval and CSV path you set in the window.

Open the setup menu from the project directory with `.venv` active:

```bash
python et54xx_cli.py
```

Type a number to change that setting. `r` lists serial ports, `c` connects and reads the load, `d` disconnects, `a` applies the selected ranges, `s` starts, and `q` quits. Start asks before the load turns on. During the run, `q` or Ctrl-C turns the load off. The CSV already written is kept.

The status page shows voltage, current, discharge amp-hours, energy, and elapsed time. The bar under those readings runs from the voltage cutoff on the left to the voltage measured at the start of the run on the right. The present voltage is written on the bar. When the terminal has room, the page also draws the voltage, current, and discharge charts from the window. A cutoff that sits far from the samples is left off its chart so the trace stays readable; the bar still shows a distant voltage cutoff.

Options on the command line replace the saved value for this run and are written back to `settings.json`. Omitted options stay as they were. `--start` skips the menu:

```bash
python et54xx_cli.py --mode CC --current 2 --cutoff-voltage 40 --csv pack.csv --start
```

`--yes` overwrites an existing CSV without asking. `--no-csv` runs without a log. `--list-ports` prints the serial resources and exits. `python et54xx_cli.py --help` lists the rest.

The file can be started directly once it is executable, from a console where `.venv` is already active:

```bash
chmod +x et54xx_cli.py
./et54xx_cli.py
```

## Resource names

The Resource field is a PyVISA serial resource. Until one has been chosen, it reads “Click Select or enter resource path”. **Select** lists the serial ports this computer can see and fills the field with the one you choose. That choice is saved in `settings.json` next to the app and filled in on the next start, along with the discharge mode, its setpoints, the stop limits, whether **Match voltage cutoff** is checked, and whether **Write CSV** is on. Uncheck **Write CSV** to run the load without creating a log. A path typed by hand is saved after a successful connect. The same list can be printed from a terminal:

```bash
python -c "import pyvisa; print(list(pyvisa.ResourceManager('@py').list_resources()))"
```

`@py` selects the pyvisa-py backend. On Windows, `ASRL` plus the COM number is the port (`ASRL3` is `COM3`). On Linux and macOS the device path is written after `ASRL`.

| System | Example resource | What it is |
| --- | --- | --- |
| Linux | `ASRL/dev/ttyUSB0::INSTR` | First USB-serial adapter (`ttyUSB0`). A CDC device may be `ttyACM0` instead. |
| macOS | `ASRL/dev/cu.usbserial-XXXXXXXX::INSTR` | USB-serial callout device. Use the `cu.` name, not `tty.` |
| Windows | `ASRL3::INSTR` | `COM3`. The number is the COM port number from Device Manager. |

The CH340 serial chip used on many ET54 loads usually appears as `/dev/ttyUSB0` on Linux, `/dev/cu.wchusbserial…` or `/dev/cu.usbserial…` on macOS, and a `COMn` port on Windows. Replace the example with the name from the listing above.
