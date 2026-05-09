# ODMR Control

PyQt-based control software for coordinating an SMB100A microwave source with an OE1022D lock-in amplifier.

## Features

- Paged GUI for connection, microwave source control, lock-in monitoring, synchronized capture, waveform review, and diagnostics.
- SMB100A parameter application, manual sweep, and preset support.
- OE1022D low-rate live monitoring and high-rate device-buffer capture.
- Software-step synchronized experiments that pair each lock-in sample segment with microwave source parameters and timestamps for downstream machine learning workflows.
- CSV/JSON capture output with `metadata.json`, `segments.csv`, per-channel sample files, and `events.csv`.

## Setup

```bash
pip install -r requirements.txt
python main.py
```

On this workstation, the tested local conda environment is:

```bash
/Users/erictseng/miniconda3/envs/odmr-gui/bin/python main.py
```

## Hardware Notes

- Target machine: ThinkPad T14 Gen2, Windows 11.
- OE1022D default serial settings are 921600 baud, 8N1.
- The synchronized capture mode uses software-stepped microwave parameters instead of SMB100A internal continuous sweep so that lock-in samples can be labeled with stable microwave settings.
