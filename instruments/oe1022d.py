from __future__ import annotations

import time
from dataclasses import dataclass
from enum import IntEnum

try:
    import serial
except ImportError:  # Allows parser checks before dependencies are installed.
    serial = None


@dataclass
class LockinSample:
    x_mv: float
    y_mv: float
    r_mv: float
    theta_deg: float
    raw: str


class LockinChannel(IntEnum):
    A = 1
    B = 2
    BOTH = 3


class LockinSampleItem(IntEnum):
    R = 0
    X = 1
    Y = 2
    THETA = 3
    RH1 = 4
    XH1 = 5
    YH1 = 6
    THETA_H1 = 7
    RH2 = 8
    XH2 = 9
    YH2 = 10
    THETA_H2 = 11
    NOISE = 12
    A1 = 13
    A2 = 14
    A3 = 15
    A4 = 16
    E1 = 17
    E2 = 18
    E3 = 19
    E4 = 20
    FREQ = 21


class ConnectionMode:
    RS232 = "RS232"
    USB = "USB"


class OE1022DController:
    def __init__(self) -> None:
        self.serial_port = None
        self.connection_mode = ConnectionMode.RS232
        self.usb_address = None

    @property
    def is_connected(self) -> bool:
        return self.serial_port is not None and self.serial_port.is_open

    def connect_rs232(self, port: str, baudrate: int) -> None:
        """连接 RS232 串口"""
        if serial is None:
            raise RuntimeError("pyserial is not installed; run `pip install -r requirements.txt` first")
        self.connection_mode = ConnectionMode.RS232
        self.serial_port = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=1,
        )
        self.serial_port.reset_input_buffer()
        self.serial_port.reset_output_buffer()

    def connect_usb(self, address: str = None) -> None:
        """连接 USB 接口
        OE1022D USB 接口可以直接通过 pyserial 打开（作为虚拟串口）
        或者通过 VISA 地址连接
        """
        if serial is None:
            raise RuntimeError("pyserial is not installed; run `pip install -r requirements.txt` first")
        self.connection_mode = ConnectionMode.USB

        # 尝试作为串口打开（OE1022D USB 多数情况下作为虚拟串口）
        if address and address.startswith("COM"):
            # 直接指定了 COM 端口
            self.serial_port = serial.Serial(
                port=address,
                baudrate=921600,  # USB 模式通常使用默认波特率
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=1,
            )
        else:
            # 尝试查找 OE1022D USB 设备
            raise RuntimeError("USB 地址未指定，请提供 COM 端口号")

        self.serial_port.reset_input_buffer()
        self.serial_port.reset_output_buffer()

    def connect(self, port: str, baudrate: int, mode: str = ConnectionMode.RS232) -> None:
        """统一连接接口"""
        if mode == ConnectionMode.USB:
            self.connect_usb(port)
        else:
            self.connect_rs232(port, baudrate)

    def close(self) -> None:
        if self.serial_port is not None:
            try:
                self.serial_port.close()
            finally:
                self.serial_port = None

    def identify(self) -> str:
        response = self.exchange("*IDND?", wait_s=0.1)
        if response.strip():
            return response
        return self.exchange("*IDN?", wait_s=0.1)

    def exchange(self, command: str, wait_s: float) -> str:
        if not self.is_connected or self.serial_port is None:
            raise RuntimeError("OE1022D is not connected")
        self.serial_port.write((command + "\r\n").encode("ascii"))
        time.sleep(wait_s)
        if self.serial_port.in_waiting:
            data = self.serial_port.read(self.serial_port.in_waiting)
            return data.decode("ascii", errors="ignore")
        return ""

    def query_snapshot(self, command: str, wait_s: float) -> list[LockinSample]:
        response = self.exchange(command, wait_s=wait_s)
        return parse_snapshot_response(response)

    def set_sample_rate(self, channel: int | LockinChannel, interval_ms: int) -> None:
        interval = int(interval_ms)
        if interval < 1 or interval > 100_000:
            raise ValueError("OE1022D sample interval must be between 1 ms and 100 s")
        self.write_command(f"SRATD {int(channel)},{interval}", wait_s=0.02)

    def set_sample_length(self, channel: int | LockinChannel, length: int) -> None:
        count = int(length)
        if count < 1 or count > 16384:
            raise ValueError("OE1022D sample length must be 1..16384")
        self.write_command(f"SLEND {int(channel)},{count}", wait_s=0.02)

    def set_sample_buffer(self, channel: int | LockinChannel, buffer_index: int, item: int | LockinSampleItem) -> None:
        buffer_number = int(buffer_index)
        if buffer_number < 1 or buffer_number > 4:
            raise ValueError("OE1022D buffer index must be 1..4")
        self.write_command(f"SSLED {int(channel)},{buffer_number},{int(item)}", wait_s=0.02)

    def configure_xy_r_theta_buffers(self, channel: int | LockinChannel) -> None:
        self.set_sample_buffer(channel, 1, LockinSampleItem.X)
        self.set_sample_buffer(channel, 2, LockinSampleItem.Y)
        self.set_sample_buffer(channel, 3, LockinSampleItem.R)
        self.set_sample_buffer(channel, 4, LockinSampleItem.THETA)

    def set_trigger_mode(self, channel: int | LockinChannel, external: bool = False) -> None:
        self.write_command(f"STRGD {int(channel)},{1 if external else 0}", wait_s=0.02)

    def set_sample_mode(self, channel: int | LockinChannel, loop: bool = False) -> None:
        self.write_command(f"SPRMD {int(channel)},{1 if loop else 0}", wait_s=0.02)

    def reset_sampling(self, channel: int | LockinChannel = LockinChannel.BOTH) -> None:
        self.write_command(f"RESTD {int(channel)}", wait_s=0.05)

    def start_sampling(self, channel: int | LockinChannel = LockinChannel.BOTH) -> None:
        self.write_command(f"STRDD {int(channel)}", wait_s=0.02)

    def pause_sampling(self, channel: int | LockinChannel = LockinChannel.BOTH) -> None:
        self.write_command(f"PAUSD {int(channel)}", wait_s=0.02)

    def query_sample_points(self, channel: int | LockinChannel) -> int:
        response = self.exchange(f"SPTSD? {int(channel)}", wait_s=0.05)
        return parse_first_int(response)

    def read_trace(self, channel: int | LockinChannel, buffer_index: int, start: int, length: int) -> list[float]:
        if length < 1:
            return []
        response = self.exchange(f"TRCAD? {int(channel)},{int(buffer_index)},{int(start)},{int(length)}", wait_s=0.08)
        return parse_float_list(response)

    def write_command(self, command: str, wait_s: float = 0.0) -> None:
        self.exchange(command, wait_s=wait_s)


def parse_snapshot_response(response: str) -> list[LockinSample]:
    filtered = "".join(ch for ch in response if ch not in ["\x00", "\r", "\t"])
    samples: list[LockinSample] = []
    for line in filtered.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            samples.append(
                LockinSample(
                    x_mv=float(parts[0]) * 1000,
                    y_mv=float(parts[1]) * 1000,
                    r_mv=float(parts[2]) * 1000,
                    theta_deg=float(parts[3]),
                    raw=line,
                )
            )
        except ValueError:
            continue
    return samples


def parse_float_list(response: str) -> list[float]:
    filtered = "".join(ch for ch in response if ch not in ["\x00", "\r", "\t", "\n"])
    values: list[float] = []
    for part in filtered.replace(";", ",").split(","):
        text = part.strip()
        if not text:
            continue
        try:
            values.append(float(text))
        except ValueError:
            continue
    return values


def parse_first_int(response: str) -> int:
    for value in parse_float_list(response):
        return int(value)
    cleaned = "".join(ch for ch in response if ch.isdigit() or ch in "+-")
    return int(cleaned) if cleaned else 0
