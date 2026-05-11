from __future__ import annotations

import tempfile

import pytest

from data.capture_store import CaptureStore, ChannelSampleRow
from instruments.oe1022d import LockinChannel, OE1022DController
from instruments.smb100a import SMB100AController, SMBCommandError, SMBParameters, evaluate_power_safety, validate_power


class FakeOE(OE1022DController):
    def __init__(self, replies: dict[str, str] | None = None) -> None:
        super().__init__()
        self.commands: list[str] = []
        self.replies = replies or {}

    @property
    def is_connected(self) -> bool:
        return True

    def exchange(self, command: str, wait_s: float) -> str:
        self.commands.append(command)
        return self.replies.get(command, "1")


class FakeInstrument:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.writes: list[str] = []

    def write(self, command: str) -> None:
        self.writes.append(command)

    def query(self, _command: str) -> str:
        return self.replies.pop(0)


def test_oe_external_sine_reference_commands_are_channelized() -> None:
    oe = FakeOE()

    oe.configure_external_sine_reference(LockinChannel.A)
    oe.configure_external_sine_reference(LockinChannel.B)

    assert oe.commands == [
        "FMODD 1,0",
        "RSLPD 1,2",
        "PHASD 1,0.00",
        "FMODD 2,0",
        "RSLPD 2,2",
        "PHASD 2,0.00",
    ]


def test_oe_pll_query_uses_selected_channel() -> None:
    oe = FakeOE({"*PLLD? 2": "1"})

    assert oe.query_pll_locked(LockinChannel.B) is True
    assert oe.commands == ["*PLLD? 2"]


@pytest.mark.parametrize("power", [-20.0, 18.0, -19.0, 17.0])
def test_smb_power_limits_allow_in_range_values(power: float) -> None:
    safety = validate_power(power)

    assert safety.within_limits is True


@pytest.mark.parametrize("power", [-21.0, 19.0])
def test_smb_power_limits_block_out_of_range_without_override(power: float) -> None:
    with pytest.raises(ValueError):
        validate_power(power, override_enabled=False)


@pytest.mark.parametrize("power", [-21.0, 19.0])
def test_smb_power_limits_allow_override_but_mark_out_of_range(power: float) -> None:
    safety = validate_power(power, override_enabled=True)

    assert safety.within_limits is False
    assert safety.override_enabled is True


def test_smb_power_near_limit_warning() -> None:
    assert evaluate_power_safety(-19.5).near_limit is True
    assert evaluate_power_safety(17.5).near_limit is True
    assert evaluate_power_safety(0.0).near_limit is False


def test_smb_write_checked_raises_on_device_error() -> None:
    smb = SMB100AController()
    smb.instrument = FakeInstrument(["1", "-222,\"Data out of range\""])

    with pytest.raises(SMBCommandError):
        smb.write_checked("POW:LEV 30 DBM")


def test_capture_store_writes_quality_fields() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = CaptureStore(tmpdir, "unit", {"smb": SMBParameters(power_dbm=-10).as_comparable()})
        store.write_samples(
            "b",
            [
                ChannelSampleRow(
                    segment_id=1,
                    sample_index=0,
                    estimated_monotonic_ns=123,
                    sample_time_s=0.0,
                    frequency_set_hz=2.87e9,
                    frequency_readback_hz=2.87e9,
                    power_set_dbm=-10,
                    rf_enabled=True,
                    lf_enabled=True,
                    fm_enabled=True,
                    pll_locked_b=True,
                    oe_ref_source_b="EXTERNAL",
                    oe_ref_slope_b="SINE_ZERO_CROSSING",
                )
            ],
        )
        store.finish("completed")

        csv_text = next(store.path.glob("channel_b.csv")).read_text(encoding="utf-8")
        assert "pll_locked_b" in csv_text
        assert "SINE_ZERO_CROSSING" in csv_text
        h5_path = store.path / "capture.h5"
        if h5_path.exists():
            import h5py

            with h5py.File(h5_path, "r") as handle:
                assert handle["samples"]["channel_b"]["frequency_set_hz"][0] == pytest.approx(2.87e9)
                assert handle["samples"]["channel_b"]["pll_locked_b"][0] == 1
