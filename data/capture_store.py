from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import h5py
except ImportError:
    h5py = None


BOOL_SAMPLE_FIELDS = {
    "rf_enabled",
    "lf_enabled",
    "fm_enabled",
    "pll_locked_a",
    "pll_locked_b",
    "input_overload_a",
    "input_overload_b",
    "gain_overload_a",
    "gain_overload_b",
}
INT_SAMPLE_FIELDS = {"segment_id", "sample_index", "estimated_monotonic_ns", "smb_error_code"}
STRING_SAMPLE_FIELDS = {"oe_ref_source_a", "oe_ref_source_b", "oe_ref_slope_a", "oe_ref_slope_b"}


@dataclass
class SweepPoint:
    segment_id: int
    frequency_hz: float


@dataclass
class ExperimentSegment:
    segment_id: int
    frequency_set_hz: float
    frequency_readback_hz: float | None
    power_set_dbm: float | None
    power_readback_dbm: float | None
    rf_enabled: bool | None
    lf_enabled: bool | None
    fm_enabled: bool | None
    lf_frequency_hz: float | None
    lf_amplitude_mv: float | None
    fm_deviation: float | None
    settle_ms: int
    sample_interval_ms: int
    sample_count: int
    segment_start_monotonic_ns: int
    segment_end_monotonic_ns: int
    smb_set_command_time_ns: int
    smb_readback_time_ns: int
    lockin_start_command_time_ns: int


@dataclass
class ChannelSampleRow:
    segment_id: int
    sample_index: int
    estimated_monotonic_ns: int
    sample_time_s: float
    frequency_set_hz: float
    frequency_readback_hz: float | None
    power_set_dbm: float | None
    rf_enabled: bool | None
    lf_enabled: bool | None
    fm_enabled: bool | None
    pll_locked_a: bool | None = None
    pll_locked_b: bool | None = None
    input_overload_a: bool | None = None
    input_overload_b: bool | None = None
    gain_overload_a: bool | None = None
    gain_overload_b: bool | None = None
    oe_ref_source_a: str | None = None
    oe_ref_source_b: str | None = None
    oe_ref_slope_a: str | None = None
    oe_ref_slope_b: str | None = None
    oe_ref_phase_deg_a: float | None = None
    oe_ref_phase_deg_b: float | None = None
    smb_error_code: int | None = None
    X_mV: float | None = None
    Y_mV: float | None = None
    R_mV: float | None = None
    theta_deg: float | None = None


class CaptureStore:
    def __init__(self, root_dir: str | Path, experiment_name: str, metadata: dict[str, Any]) -> None:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = sanitize_name(experiment_name or "experiment")
        self.path = Path(root_dir).expanduser().resolve() / f"{stamp}_{safe_name}"
        self.path.mkdir(parents=True, exist_ok=False)
        self._metadata = {
            **metadata,
            "experiment_name": experiment_name,
            "created_at": dt.datetime.now().isoformat(timespec="milliseconds"),
            "status": "running",
        }
        self._segment_file = open(self.path / "segments.csv", "w", newline="", encoding="utf-8")
        self._events_file = open(self.path / "events.csv", "w", newline="", encoding="utf-8")
        self._channel_files: dict[str, Any] = {}
        self._segment_writer = csv.DictWriter(self._segment_file, fieldnames=list(ExperimentSegment.__annotations__))
        self._event_writer = csv.DictWriter(
            self._events_file,
            fieldnames=["timestamp_iso", "monotonic_ns", "level", "source", "message"],
        )
        self._channel_writers: dict[str, csv.DictWriter] = {}
        self._segment_writer.writeheader()
        self._event_writer.writeheader()
        self._h5 = self._open_hdf5()
        self.write_metadata()

    def open_channel(self, channel_name: str) -> None:
        if channel_name in self._channel_writers:
            return
        filename = f"channel_{channel_name.lower()}.csv"
        handle = open(self.path / filename, "w", newline="", encoding="utf-8")
        writer = csv.DictWriter(handle, fieldnames=list(ChannelSampleRow.__annotations__))
        writer.writeheader()
        self._channel_files[channel_name] = handle
        self._channel_writers[channel_name] = writer

    def write_segment(self, segment: ExperimentSegment) -> None:
        self._segment_writer.writerow(asdict(segment))
        self._segment_file.flush()
        self._append_h5_json("segments", asdict(segment))

    def write_samples(self, channel_name: str, rows: Iterable[ChannelSampleRow]) -> int:
        self.open_channel(channel_name)
        count = 0
        h5_rows: list[dict[str, Any]] = []
        writer = self._channel_writers[channel_name]
        for row in rows:
            payload = asdict(row)
            writer.writerow(payload)
            h5_rows.append(payload)
            count += 1
        self._channel_files[channel_name].flush()
        self._append_h5_samples(channel_name.lower(), h5_rows)
        return count

    def write_event(self, monotonic_ns: int, level: str, source: str, message: str) -> None:
        self._event_writer.writerow(
            payload := {
                "timestamp_iso": dt.datetime.now().isoformat(timespec="milliseconds"),
                "monotonic_ns": monotonic_ns,
                "level": level,
                "source": source,
                "message": message,
            }
        )
        self._events_file.flush()
        self._append_h5_json("events", payload)

    def finish(self, status: str, extra: dict[str, Any] | None = None) -> None:
        self._metadata["status"] = status
        self._metadata["finished_at"] = dt.datetime.now().isoformat(timespec="milliseconds")
        if extra:
            self._metadata.update(extra)
        self.write_metadata()
        self.close()

    def write_metadata(self) -> None:
        (self.path / "metadata.json").write_text(
            json.dumps(self._metadata, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        if self._h5 is not None:
            self._h5.attrs["metadata_json"] = json.dumps(self._metadata, ensure_ascii=False)
            self._h5.flush()

    def close(self) -> None:
        for handle in [self._segment_file, self._events_file, *self._channel_files.values()]:
            try:
                handle.close()
            except Exception:
                pass
        if self._h5 is not None:
            try:
                self._h5.close()
            except Exception:
                pass
            self._h5 = None

    def _open_hdf5(self):
        if h5py is None:
            return None
        handle = h5py.File(self.path / "capture.h5", "w")
        handle.attrs["schema_version"] = "odmr-control.capture.v1"
        handle.create_group("metadata")
        handle.create_group("segments")
        handle.create_group("samples")
        handle.create_group("events")
        handle.create_group("raw")
        return handle

    def _append_h5_json(self, name: str, payload: dict[str, Any]) -> None:
        if self._h5 is None:
            return
        group_name = "events" if name == "events" else "segments" if name == "segments" else "samples"
        group = self._h5[group_name]
        dataset_name = name if group_name == "samples" else "records"
        if dataset_name not in group:
            dtype = h5py.string_dtype(encoding="utf-8")
            group.create_dataset(dataset_name, shape=(0,), maxshape=(None,), dtype=dtype)
        dataset = group[dataset_name]
        dataset.resize((dataset.shape[0] + 1,))
        dataset[-1] = json.dumps(payload, ensure_ascii=False)
        self._h5.flush()

    def _append_h5_samples(self, channel_name: str, rows: list[dict[str, Any]]) -> None:
        if self._h5 is None or not rows:
            return
        channel_group = self._h5["samples"].require_group(f"channel_{channel_name}")
        for field in ChannelSampleRow.__annotations__:
            values = [row.get(field) for row in rows]
            encoded, dtype = encode_h5_sample_values(field, values)
            if field not in channel_group:
                channel_group.create_dataset(
                    field,
                    data=encoded,
                    maxshape=(None,),
                    chunks=True,
                    dtype=dtype,
                )
                continue
            dataset = channel_group[field]
            old_size = dataset.shape[0]
            dataset.resize((old_size + len(encoded),))
            dataset[old_size:] = encoded
        self._h5.flush()


def encode_h5_sample_values(field: str, values: list[Any]) -> tuple[list[Any], Any]:
    if field in STRING_SAMPLE_FIELDS:
        return ["" if value is None else str(value) for value in values], h5py.string_dtype(encoding="utf-8")
    if field in BOOL_SAMPLE_FIELDS:
        return [-1 if value is None else int(bool(value)) for value in values], "i1"
    if field in INT_SAMPLE_FIELDS:
        return [-1 if value is None else int(value) for value in values], "i8"
    return [float("nan") if value is None else float(value) for value in values], "f8"


def sanitize_name(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name.strip())
    return safe[:80] or "experiment"


def build_sweep_points(start_hz: float, stop_hz: float, step_hz: float) -> list[SweepPoint]:
    if step_hz == 0:
        raise ValueError("frequency step cannot be 0")
    if start_hz < stop_hz and step_hz < 0:
        raise ValueError("frequency step must be positive for ascending sweep")
    if start_hz > stop_hz and step_hz > 0:
        raise ValueError("frequency step must be negative for descending sweep")

    points: list[SweepPoint] = []
    current = start_hz
    segment_id = 1
    tolerance = abs(step_hz) / 1000
    while (step_hz > 0 and current <= stop_hz + tolerance) or (step_hz < 0 and current >= stop_hz - tolerance):
        points.append(SweepPoint(segment_id=segment_id, frequency_hz=current))
        current += step_hz
        segment_id += 1
    return points
