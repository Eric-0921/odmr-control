# Mag Field Control v1 Branch Notes

This branch adds the standalone three-axis magnetic field control subproject under `mag-field-control/`.

The code is intentionally isolated from the current ODMR control application on `main`. It should be reviewed, tested, and integrated later as a hardware-control module or companion tool rather than merged directly over the existing GUI.

## What Is Included

- PyQt5 GUI for three-axis coil control.
- Serial SCPI driver for the original power-controller protocol.
- Three-axis field controller with zero-offset, reproduction-current, lock-zero, and polling semantics.
- Vector-field calculator and target/readback visualization.
- JSON presets.
- Sequence automation and CSV recording.
- Unit tests with fake serial coverage.
- Full project documentation in `mag-field-control/README.md`.

## Hardware Semantics

The subproject follows the behavior confirmed from the original `SimplePowerController.exe`:

- UI/internal units: `mA` and `nT`.
- Device protocol current unit: `A`.
- `CURR` command sends total current as `abs(total_mA) / 1000.0`, formatted with five decimals.
- `MEAS:CURR?` returns `A`, converted back to `mA`.
- Target field conversion: `recur_current_mA = target_field_nT / coil_constant`.
- Estimated field conversion: `estimated_field_nT = (measured_total_mA - zero_offset_mA) * coil_constant`.
- Lock-zero output: `total_current_mA = zero_offset_mA + recur_current_mA`.

The Python version deliberately rejects negative target field/current requests before they reach the serial layer.

## Validation

Run from `mag-field-control/`:

```bash
python -m unittest discover -v
python -m compileall instruments core app workers data tests main.py
```

Known validated environments:

- macOS local conda env: `odmr-gui`
- Windows TP14g2 Anaconda: `D:\anaconda3\python.exe`

## Integration Notes

Recommended next integration path:

1. Keep `mag-field-control/` as an independent subproject for hardware validation.
2. Extract `core/field_controller.py` and `instruments/magnet_coil.py` behind an ODMR hardware-service interface.
3. Let ODMR experiments call the field controller through a narrow API: connect, set target field, lock/unlock zero, output on/off, snapshot.
4. Preserve CSV field semantics if experiment recordings are unified later.

Do not commit original exe files, installer artifacts, decompiled source dumps, or runtime logs into this repository.
