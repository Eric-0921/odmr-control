from __future__ import annotations

import csv
import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


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
    X_mV: float | None
    Y_mV: float | None
    R_mV: float | None
    theta_deg: float | None


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

    def write_samples(self, channel_name: str, rows: Iterable[ChannelSampleRow]) -> int:
        self.open_channel(channel_name)
        count = 0
        writer = self._channel_writers[channel_name]
        for row in rows:
            writer.writerow(asdict(row))
            count += 1
        self._channel_files[channel_name].flush()
        return count

    def write_event(self, monotonic_ns: int, level: str, source: str, message: str) -> None:
        self._event_writer.writerow(
            {
                "timestamp_iso": dt.datetime.now().isoformat(timespec="milliseconds"),
                "monotonic_ns": monotonic_ns,
                "level": level,
                "source": source,
                "message": message,
            }
        )
        self._events_file.flush()

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

    def close(self) -> None:
        for handle in [self._segment_file, self._events_file, *self._channel_files.values()]:
            try:
                handle.close()
            except Exception:
                pass


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
