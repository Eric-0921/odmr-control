# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ODMR Control is a PyQt5 desktop application for controlling Optically Detected Magnetic Resonance experiments. It controls three instruments:
- **SMB100A** (Rohde & Schwarz) — microwave signal source, VISA/SCPI protocol
- **OE1022D** (SSI) — dual-channel DSP lock-in amplifier, Serial/RS-232 protocol
- **MSL-U** (CNI) — 532nm laser, Serial/RS-232 one-way protocol (no query, software-cached state)

The codebase language is Chinese (comments, docs, UI labels) with English code identifiers.

## Running and Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Run the application
python main.py

# Run unit tests (no hardware or PyQt5 GUI required — uses FakeVISA/FakeSerial stubs)
python -m pytest tests/ -v
# or
python -m unittest tests.test_instruments -v
```

## Architecture

Six-layer architecture with a unified command bus:

```
Layer 5: app/gui.py, app/config_io.py          — PyQt5 GUI (Siemens industrial style)
Layer 4: core/agent_api.py                       — AI Agent Python API
Layer 3: core/command_service.py                 — Command bus (single-thread queue)
Layer 2: core/instrument_controller.py           — Facade over drivers + workers
Layer 1.5: core/sweep_engine.py, timestamp_sync  — Sweep sequences, timestamp alignment
Layer 1: instruments/smb100a.py, instruments/oe1022d.py, instruments/laser_msl.py  — Device drivers
Layer 0: data/recorder.py, data/circular_buffer.py       — Parquet recording, ring buffer
```

**Key design decisions:**
- All device operations go through `CommandService` as serialized `Command` objects (command pattern). GUI and AI Agent share the same interface via `submit()` / `submit_sync()`.
- Single-thread serial execution in `CommandService._process_loop()` — avoids VISA/Serial concurrency issues. Do not add parallelism to command execution.
- Workers (`SMBPollWorker`, `LockinMonitorWorker`, `LockinAcquireWorker`) read drivers directly for low-latency polling, bypassing CommandService (read-only, driver-locked).
- Signal flow: `Driver → InstrumentController → CommandService → GUI` (three-level broadcast).

## Thread Model

- Main thread: GUI + QTimer display refresh
- QThread: CommandService worker (serial command queue)
- QThread: SMBPollWorker (100ms), LockinMonitorWorker (94ms)
- QThread: LockinAcquireWorker (50ms, sweep-only)
- QThread: LaserPollWorker (1000ms, cache-only no I/O)
- QThread: SweepEngine `_SweepRunner` (must be parentless QObject for moveToThread safety)

## Safety Constraints

- Power limits enforced at `CommandService._check_power_limit()`: 10 dBm with amplifier, 25 dBm without (`smb.amplifier_installed` config key)
- Frequency range: 100 kHz – 12.75 GHz
- Emergency stop: `SYS_EMERGENCY_STOP` command — turns off RF/LF/FM, returns to CW mode
- All VISA/Serial I/O protected by `threading.Lock`/`RLock`

## Config Format

`config.xml` or `config.json` at project root. Loaded by `app/config_io.py`, merged with `DEFAULT_CONFIG` defaults. Key sections: `smb`, `lockin`, `lockin_bindings` (SN→COM mapping), `sweep`, `acquisition`, `ui`.

## Data Output

Parquet (Snappy) with JSON metadata:
```
experiment_YYYYMMDD_HHMMSS/
├── data.parquet
├── columns.json
└── metadata.json
```
Schema: `sample_index`, `time_s`, `host_timestamp_s`, 20 RALL? lock-in params, SMB freq/power/RF state.

## Pitfalls to Watch

- `_SweepRunner` must have `super().__init__(None)` — Qt forbids moveToThread on objects with a parent
- GUI thread must never call `submit_sync()` (blocks event loop) — use `submit()` + signal callback
- VISA `list_resources()` can hang on stale devices — always use short timeouts (500ms)
- RALL? data can be incomplete (USB drops) — always validate `len(raw) == 12288` before parsing
- OE1022D `*IDND?` response varies across firmware — use substring `"SSI LIA-OE1022D" in resp`, not exact match
- `origin/smb-locked_2.1.1.py` is ~60k lines legacy code — do not modify
