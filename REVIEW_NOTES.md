# Review Notes

Target runtime: ThinkPad T14 Gen2 on Windows 11.

## Deferred P2 Findings

- OE1022D ID query: the current behavior still sends `*IDN?`, while the OCR manual lists `*IDND?`. Verify on hardware before changing the command because the existing lab setup may rely on observed compatibility.
- Power unit handling: the old `apply_parameters()` path could append `DBM` twice. The refactored SMB controller normalizes power for new writes, but this remains listed for hardware regression attention.
- Serial scan scope: the UI still prioritizes Windows `COM1` through `COM20`, matching the target Win11 runtime. Cross-platform `/dev/tty.*` and `/dev/cu.*` scanning is intentionally out of scope for now.

## Runtime Setup Notes

- Install Python dependencies with `pip install -r requirements.txt`.
- On the verified ThinkPad T14 Gen2, launch with `run_win.bat` or `py -3.13 main.py`; the default `python` command points to Python 3.14 and may not have the GUI dependencies installed.
- On this Mac, launch local GUI checks with `run_mac.command` or `/Users/erictseng/miniconda3/envs/odmr-gui/bin/python main.py`.
- Install a VISA runtime separately, such as NI-VISA or the R&S VISA runtime, so `pyvisa` can open the SMB100A resource.
- Confirm the OE1022D COM port in Windows Device Manager before connecting.
