# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

PyQt5-based control application for coordinating an **R&S SMB100A microwave source** with an **OE1022D lock-in amplifier** for ODMR (Optically Detected Magnetic Resonance) experiments.

## Running the Application

```bash
pip install -r requirements.txt
python main.py
```

On Windows with Anaconda: `run_win.bat`
On Mac with miniconda: `/Users/erictseng/miniconda3/envs/odmr-gui/bin/python main.py`

**Prerequisites:**
- NI-VISA or R&S VISA runtime must be installed for pyvisa to communicate with SMB100A
- Confirm OE1022D COM port in Windows Device Manager before connecting

## Architecture

```
main.py                    # Entry point - creates QApplication, launches SMB100AControlGUI
app/gui.py                 # Main PyQt5 window (SMB100AControlGUI class) - tabbed interface
config/
  smb100a.py              # SMB100A VISA instrument controller
  oe1022d.py              # OE1022D serial instrument controller
workers/
  experiment_worker.py    # Synchronized sweep experiment execution (QThread)
  lockin_worker.py        # OE1022D polling worker (QThread)
data/
  capture_store.py        # Experiment data storage (CSV/JSON output)
  recorder.py             # Simple CSV recorder
instruments/
  presets.py              # Preset save/load system
```

## GUI Structure (6 tabs)

1. **连接/总览** - Device connection controls and status
2. **微波源** - SMB100A parameter control and sweep
3. **锁相实时** - OE1022D live monitoring
4. **同步采集** - Coordinated experiment execution
5. **波形/数据** - pyqtgraph waveform plotting
6. **日志/诊断** - Event logs

## Hardware Configuration

| Device | Interface | Default Address/Settings |
|--------|-----------|--------------------------|
| SMB100A | USB/VISA | `USB::0x0AAD::0x0054::101623::INSTR` |
| OE1022D | Serial | COM4, 921600 baud, 8N1 |

## Data Output Format

Captures create timestamped directories containing:
- `metadata.json` - Experiment configuration
- `segments.csv` - Per-segment microwave parameters
- `channel_a.csv`, `channel_b.csv` - Per-channel lock-in samples
- `events.csv` - Timestamped event log

## Key Design Decisions

- **Software-stepped sweep**: Synchronized capture uses software-stepped frequency changes (not SMB100A internal sweep) so each lock-in sample is labeled with stable microwave settings
- **QThread workers**: LockinWorker polls OE1022D; ExperimentWorker coordinates synchronized experiments
- **Signal/slot architecture**: PyQt5 signals for thread-safe GUI-worker communication

## Known Hardware Quirks (from REVIEW_NOTES.md)

- OE1022D ID query uses `*IDN?` (not `*IDND?` as the OCR manual suggests) - existing lab setup may depend on this
- Power unit handling has a potential double-`DBM` append issue in old code paths
- Serial port scanning only covers Windows `COM1`-`COM20`
