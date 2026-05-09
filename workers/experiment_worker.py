from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any

from PyQt5.QtCore import QObject, pyqtSignal

from data.capture_store import CaptureStore, ChannelSampleRow, ExperimentSegment, SweepPoint, build_sweep_points
from instruments.oe1022d import LockinChannel, OE1022DController
from instruments.smb100a import SMB100AController, SMBParameters


@dataclass
class ExperimentConfig:
    experiment_name: str
    output_root: str
    smb_params: SMBParameters
    start_hz: float
    stop_hz: float
    step_hz: float
    settle_ms: int
    sample_interval_ms: int
    sample_count: int
    channel_a_enabled: bool = True
    channel_b_enabled: bool = True
    chunk_size: int = 512


class ExperimentWorker(QObject):
    progress_changed = pyqtSignal(int, int, int, str)
    segment_ready = pyqtSignal(object)
    event_logged = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    finished = pyqtSignal(str, str)

    def __init__(self, smb: SMB100AController, lockin: OE1022DController, config: ExperimentConfig) -> None:
        super().__init__()
        self.smb = smb
        self.lockin = lockin
        self.config = config
        self._stop_requested = False
        self._store: CaptureStore | None = None

    def stop(self) -> None:
        self._stop_requested = True

    def run(self) -> None:
        status = "completed"
        output_path = ""
        segments_done = 0
        try:
            points = build_sweep_points(self.config.start_hz, self.config.stop_hz, self.config.step_hz)
            if not points:
                raise ValueError("没有生成任何扫频点")
            if not self.config.channel_a_enabled and not self.config.channel_b_enabled:
                raise ValueError("至少启用一个锁相通道")

            self._store = CaptureStore(
                self.config.output_root,
                self.config.experiment_name,
                {
                    "mode": "software_step_synchronized",
                    "sample_interval_ms": self.config.sample_interval_ms,
                    "sample_count": self.config.sample_count,
                    "channels": {
                        "a": self.config.channel_a_enabled,
                        "b": self.config.channel_b_enabled,
                    },
                    "buffer_map": {"1": "X", "2": "Y", "3": "R", "4": "theta"},
                },
            )
            output_path = str(self._store.path)
            self._log("INFO", "experiment", f"创建采集目录: {output_path}")

            self.smb.set_frequency_mode_cw()
            self._configure_lockin_channels()

            for index, point in enumerate(points, start=1):
                if self._stop_requested:
                    status = "partial"
                    break
                if self._run_segment(point, index, len(points)):
                    segments_done += 1
                if self._stop_requested:
                    status = "partial"
                    break

        except Exception as exc:
            status = "failed"
            self.error_occurred.emit(str(exc))
            self._log("ERROR", "experiment", str(exc))
        finally:
            try:
                if self.lockin.is_connected:
                    self.lockin.pause_sampling(LockinChannel.BOTH)
            except Exception:
                pass
            if self._store is not None:
                self._store.finish(status, {"segments_done": segments_done})
                output_path = str(self._store.path)
            self.finished.emit(status, output_path)

    def _configure_lockin_channels(self) -> None:
        channels = self._selected_channels()
        for channel, _name in channels:
            self.lockin.set_sample_rate(channel, self.config.sample_interval_ms)
            self.lockin.set_sample_length(channel, self.config.sample_count)
            self.lockin.set_trigger_mode(channel, external=False)
            self.lockin.set_sample_mode(channel, loop=False)
            self.lockin.configure_xy_r_theta_buffers(channel)
            self._log("INFO", "lockin", f"配置通道 {channel.name}: {self.config.sample_interval_ms} ms, {self.config.sample_count} 点")

    def _run_segment(self, point: SweepPoint, point_index: int, total_points: int) -> bool:
        if self._store is None:
            raise RuntimeError("capture store is not initialized")

        segment_start_ns = time.perf_counter_ns()
        self.progress_changed.emit(point_index, total_points, 0, f"设置频率 {point.frequency_hz:.6g} Hz")

        params = replace(self.config.smb_params, cw_hz=point.frequency_hz)
        smb_set_ns = time.perf_counter_ns()
        self.smb.apply_cw_parameters(params)
        self._log("INFO", "smb", f"segment {point.segment_id}: FREQ:CW {point.frequency_hz:.12g} Hz")

        readback = self.smb.read_parameters()
        smb_readback_ns = time.perf_counter_ns()

        self._sleep_with_stop(self.config.settle_ms / 1000)
        if self._stop_requested:
            return False

        run_channel = self._run_channel()
        self.lockin.reset_sampling(run_channel)
        lockin_start_ns = time.perf_counter_ns()
        self.lockin.start_sampling(run_channel)
        self._log("INFO", "lockin", f"segment {point.segment_id}: STRDD {int(run_channel)}")

        acquired = self._wait_for_points(point_index, total_points)
        segment_end_ns = time.perf_counter_ns()

        segment = ExperimentSegment(
            segment_id=point.segment_id,
            frequency_set_hz=point.frequency_hz,
            frequency_readback_hz=readback.cw_hz,
            power_set_dbm=params.power_dbm,
            power_readback_dbm=readback.power_dbm,
            rf_enabled=params.rf_output,
            lf_enabled=params.lf_output,
            fm_enabled=params.fm_state,
            lf_frequency_hz=params.lf_freq_hz,
            lf_amplitude_mv=params.lf_amp_mv,
            fm_deviation=params.fm_dev_hz,
            settle_ms=self.config.settle_ms,
            sample_interval_ms=self.config.sample_interval_ms,
            sample_count=acquired,
            segment_start_monotonic_ns=segment_start_ns,
            segment_end_monotonic_ns=segment_end_ns,
            smb_set_command_time_ns=smb_set_ns,
            smb_readback_time_ns=smb_readback_ns,
            lockin_start_command_time_ns=lockin_start_ns,
        )
        self._store.write_segment(segment)

        plot_payload: dict[str, Any] = {"segment": segment, "channels": {}}
        for channel, name in self._selected_channels():
            traces = self._read_channel_traces(channel, acquired)
            rows = build_channel_rows(segment, name, traces)
            self._store.write_samples(name, rows)
            plot_payload["channels"][name] = traces
            self._log("INFO", "lockin", f"segment {point.segment_id}: 通道 {name.upper()} 保存 {acquired} 点")
        self.segment_ready.emit(plot_payload)
        self.progress_changed.emit(point_index, total_points, acquired, f"完成 segment {point.segment_id}")
        return True

    def _wait_for_points(self, point_index: int, total_points: int) -> int:
        deadline = time.monotonic() + max(10.0, self.config.sample_count * self.config.sample_interval_ms / 1000 * 3)
        acquired = 0
        while not self._stop_requested:
            counts = []
            for channel, _name in self._selected_channels():
                counts.append(self.lockin.query_sample_points(channel))
            acquired = min(counts) if counts else 0
            self.progress_changed.emit(point_index, total_points, acquired, f"采样 {acquired}/{self.config.sample_count}")
            if acquired >= self.config.sample_count:
                return self.config.sample_count
            if time.monotonic() > deadline:
                raise TimeoutError(f"OE1022D 采样超时，只获得 {acquired}/{self.config.sample_count} 点")
            time.sleep(0.1)
        return acquired

    def _read_channel_traces(self, channel: LockinChannel, count: int) -> dict[str, list[float | None]]:
        traces_v = {
            "X_mV": self._read_full_trace(channel, 1, count, scale=1000),
            "Y_mV": self._read_full_trace(channel, 2, count, scale=1000),
            "R_mV": self._read_full_trace(channel, 3, count, scale=1000),
            "theta_deg": self._read_full_trace(channel, 4, count, scale=1),
        }
        return traces_v

    def _read_full_trace(self, channel: LockinChannel, buffer_index: int, count: int, scale: float) -> list[float | None]:
        values: list[float | None] = []
        start = 0
        while start < count:
            length = min(self.config.chunk_size, count - start)
            chunk = self.lockin.read_trace(channel, buffer_index, start, length)
            values.extend(value * scale for value in chunk[:length])
            if len(chunk) < length:
                values.extend([None] * (length - len(chunk)))
            start += length
        if len(values) < count:
            values.extend([None] * (count - len(values)))
        return values[:count]

    def _selected_channels(self) -> list[tuple[LockinChannel, str]]:
        channels: list[tuple[LockinChannel, str]] = []
        if self.config.channel_a_enabled:
            channels.append((LockinChannel.A, "a"))
        if self.config.channel_b_enabled:
            channels.append((LockinChannel.B, "b"))
        return channels

    def _run_channel(self) -> LockinChannel:
        if self.config.channel_a_enabled and self.config.channel_b_enabled:
            return LockinChannel.BOTH
        if self.config.channel_a_enabled:
            return LockinChannel.A
        return LockinChannel.B

    def _sleep_with_stop(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not self._stop_requested:
            time.sleep(min(0.05, deadline - time.monotonic()))

    def _log(self, level: str, source: str, message: str) -> None:
        now_ns = time.perf_counter_ns()
        if self._store is not None:
            self._store.write_event(now_ns, level, source, message)
        self.event_logged.emit(f"{source}: {message}")


def build_channel_rows(
    segment: ExperimentSegment,
    _channel_name: str,
    traces: dict[str, list[float | None]],
) -> list[ChannelSampleRow]:
    count = segment.sample_count
    rows: list[ChannelSampleRow] = []
    for sample_index in range(count):
        sample_time_s = sample_index * segment.sample_interval_ms / 1000
        rows.append(
            ChannelSampleRow(
                segment_id=segment.segment_id,
                sample_index=sample_index,
                estimated_monotonic_ns=segment.lockin_start_command_time_ns + int(sample_time_s * 1_000_000_000),
                sample_time_s=sample_time_s,
                frequency_set_hz=segment.frequency_set_hz,
                frequency_readback_hz=segment.frequency_readback_hz,
                power_set_dbm=segment.power_set_dbm,
                rf_enabled=segment.rf_enabled,
                lf_enabled=segment.lf_enabled,
                fm_enabled=segment.fm_enabled,
                X_mV=traces["X_mV"][sample_index],
                Y_mV=traces["Y_mV"][sample_index],
                R_mV=traces["R_mV"][sample_index],
                theta_deg=traces["theta_deg"][sample_index],
            )
        )
    return rows
