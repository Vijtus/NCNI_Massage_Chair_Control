from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import types
import time

import app as app_module
import bridge.config as bridge_config
import bridge.firmware as firmware_module
import bridge.http_server as http_server_module
from bridge.auto_speed_pattern import speed_at_elapsed
from bridge.commands import AUTO_DEAD_COMMANDS, MODEL_ONLY_COMMANDS, RETRY_SAFE_COMMANDS
from bridge.firmware import FirmwareSerialBridge
from bridge.framing import FullFrameParser
from bridge.http_server import (
    _is_likely_virtual_interface,
    build_network_page,
    build_public_url,
    compute_state_etag,
)
from bridge.state import ChairState


def frame(
    *,
    b3=0x04,
    b4=0x02,
    b5=0x0C,
    b6=0x0F,
    foot_b7=0x0C,
    shared_b11=None,
    zone=0x07,
    heat=0x00,
    tail=(0x0A, 0x0B, 0x0C, 0x08),
    payload_zero=False,
):
    data = [0xAA, 0x55] + [0x00] * 31
    if payload_zero:
        return data
    data[2] = 0x08
    data[3] = b3
    data[4] = b4
    data[5] = b5  # byte 5 = intensity bucket (0x00=1, 0x0C=2, 0x0F=3)
    data[6] = b6  # byte 6 = running flag (0x0F=running, 0x00=cleared)
    data[7] = foot_b7  # byte 7 = foot_speed bucket
    data[11] = b5 if shared_b11 is None else shared_b11
    data[12] = b6
    data[17:21] = list(tail)
    data[21] = zone
    data[23] = heat
    data[29:33] = [0x00, 0x00, 0x00, 0x00]
    return data


class FakeSerial:
    def __init__(self):
        self.writes = []
        self.in_waiting = 0
        self.closed = False

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        return None

    def close(self):
        self.closed = True


def connected_bridge():
    state = ChairState()
    state.set_connection(True, "/dev/fake")
    state.note_frame(frame())
    serial = FakeSerial()
    bridge = FirmwareSerialBridge(state=state, baud_rate=115200)
    bridge.serial_handle = serial
    return state, bridge, serial


def drain_writes(bridge, count):
    bridge.last_write_at = 0.0
    for _ in range(count):
        bridge._write_commands()
        bridge.last_write_at = 0.0


def listen_off_payload(bridge):
    return bridge.config.LISTEN_OFF_CONTROL_PAYLOAD.encode("ascii")


def listen_on_payload(bridge):
    return bridge.config.LISTEN_ON_CONTROL_PAYLOAD.encode("ascii")


def expire_verifications(bridge):
    for tx in bridge.verifying.values():
        tx.verify_deadline = time.monotonic() - 0.01
    bridge._check_timeouts()


def test_bridge_rejects_backlog_until_command_verified():
    state, bridge, serial = connected_bridge()

    first = bridge.send_command("ramiona")
    rejected_while_unsent = bridge.send_command("przedramiona")
    drain_writes(bridge, 1)
    rejected_while_in_flight = bridge.send_command("przedramiona")
    bridge.note_backend_line(f"ACK seq={first} code=0x13")
    rejected_while_acked = bridge.send_command("przedramiona")
    bridge.note_backend_line(f"DONE seq={first} code=0x13")
    drain_writes(bridge, 1)
    state.note_backend_line("Chair read: ON")
    state.note_frame(frame(zone=0x06))
    expire_verifications(bridge)
    accepted_after_complete = bridge.send_command("przedramiona")

    assert first is not None
    assert rejected_while_unsent is None
    assert rejected_while_in_flight is None
    assert rejected_while_acked is None
    assert accepted_after_complete is not None
    assert serial.writes == [
        f"{first} ramiona\n".encode("ascii"),
        listen_on_payload(bridge),
    ]


def test_bridge_busy_reports_pending_command():
    _state, bridge, serial = connected_bridge()

    seq = bridge.send_command("ramiona")
    second = bridge.send_command("przedramiona")
    drain_writes(bridge, 1)

    assert seq is not None
    assert second is None
    assert bridge.is_busy()
    assert serial.writes == [f"{seq} ramiona\n".encode("ascii")]


def test_backrest_requires_hold_action_not_plain_command():
    _state, bridge, _serial = connected_bridge()

    assert bridge.send_command("oparcie_w_gore") is None
    assert bridge.send_command("oparcie_w_dol") is None


def test_backrest_hold_start_and_stop_bypasses_busy_gate():
    state, bridge, serial = connected_bridge()

    started = bridge.send_hold_start("oparcie_w_gore", "hold-1")
    drain_writes(bridge, 1)
    bridge.note_backend_line(f"ACK seq={started.seq} code=0x09")
    stopped = bridge.send_hold_stop("oparcie_w_gore", "hold-1")
    drain_writes(bridge, 1)

    assert started.ok and started.queued and started.seq is not None
    assert stopped.ok and stopped.queued and stopped.seq == started.seq
    assert serial.writes == [
        f"{started.seq} hold_start oparcie_w_gore\n".encode("ascii"),
        f"{started.seq} hold_stop oparcie_w_gore\n".encode("ascii"),
    ]
    assert state.snapshot()["hold"]["stop_requested"] is True

    bridge.note_backend_line(f"DONE seq={started.seq} code=0x09")
    assert state.snapshot()["hold"]["active"] is False


def test_backrest_duplicate_hold_stop_is_idempotent():
    _state, bridge, serial = connected_bridge()

    started = bridge.send_hold_start("oparcie_w_dol", "hold-2")
    drain_writes(bridge, 1)
    bridge.note_backend_line(f"ACK seq={started.seq} code=0x06")
    first = bridge.send_hold_stop("oparcie_w_dol", "hold-2")
    second = bridge.send_hold_stop("oparcie_w_dol", "hold-2")
    drain_writes(bridge, 1)

    assert first.ok and first.queued
    assert second.ok and not second.queued
    assert serial.writes == [
        f"{started.seq} hold_start oparcie_w_dol\n".encode("ascii"),
        f"{started.seq} hold_stop oparcie_w_dol\n".encode("ascii"),
    ]


def test_backrest_stop_before_start_cancels_late_start():
    _state, bridge, serial = connected_bridge()

    stopped = bridge.send_hold_stop("oparcie_w_gore", "fast-tap")
    started = bridge.send_hold_start("oparcie_w_gore", "fast-tap")
    drain_writes(bridge, 1)

    assert stopped.ok and not stopped.queued
    assert started.ok and not started.queued
    assert serial.writes == []
    assert not bridge.is_busy()


def test_backrest_rejects_both_directions_pressed():
    _state, bridge, _serial = connected_bridge()

    up = bridge.send_hold_start("oparcie_w_gore", "up")
    down = bridge.send_hold_start("oparcie_w_dol", "down")

    assert up.ok and up.queued
    assert not down.ok
    assert down.error == "bridge busy"


def test_backrest_hold_timeout_queues_stop_signal():
    _state, bridge, serial = connected_bridge()

    started = bridge.send_hold_start("oparcie_w_gore", "timeout")
    drain_writes(bridge, 1)
    bridge.note_backend_line(f"ACK seq={started.seq} code=0x09")
    tx = bridge.acked[started.seq]
    tx.acked_at = time.monotonic() - bridge_config.HOLD_MAX_SECONDS - 0.1

    bridge._check_timeouts()
    drain_writes(bridge, 1)

    assert serial.writes[-1] == f"{started.seq} hold_stop oparcie_w_gore\n".encode(
        "ascii"
    )


def test_backrest_hold_state_marks_opposite_direction_blocked():
    state, bridge, _serial = connected_bridge()

    bridge.send_hold_start("oparcie_w_gore", "state")
    snapshot = state.snapshot()

    assert snapshot["buttons"]["oparcie_w_gore"]["active"] is True
    assert snapshot["buttons"]["oparcie_w_dol"]["blocked"] is True


def test_bridge_turns_listen_off_before_usb_command():
    state, bridge, serial = connected_bridge()
    state.listening = True

    seq = bridge.send_command("ramiona")
    drain_writes(bridge, 1)
    drain_writes(bridge, 1)

    assert seq is not None
    assert serial.writes == [listen_off_payload(bridge)]

    state.note_backend_line("Chair read: OFF")
    drain_writes(bridge, 1)

    assert serial.writes[-1] == f"{seq} ramiona\n".encode("ascii")


def test_bridge_does_not_listen_on_until_done():
    state, bridge, serial = connected_bridge()
    state.listening = True

    seq = bridge.send_command("ramiona")
    drain_writes(bridge, 1)
    state.note_backend_line("Chair read: OFF")
    drain_writes(bridge, 1)
    bridge.note_backend_line(f"ACK seq={seq} code=0x13")
    drain_writes(bridge, 1)

    assert serial.writes == [
        listen_off_payload(bridge),
        f"{seq} ramiona\n".encode("ascii"),
    ]

    bridge.note_backend_line(f"DONE seq={seq} code=0x13")
    drain_writes(bridge, 1)

    assert serial.writes[-1] == listen_on_payload(bridge)


def test_ensure_listening_waits_for_in_flight_command():
    state, bridge, serial = connected_bridge()
    state.listening = True

    bridge.send_command("ramiona")
    drain_writes(bridge, 1)
    state.note_backend_line("Chair read: OFF")
    drain_writes(bridge, 1)
    bridge._ensure_listening()

    assert serial.writes == [listen_off_payload(bridge), b"1 ramiona\n"]


def test_bridge_retries_lost_listen_off_before_command():
    state, bridge, serial = connected_bridge()
    state.listening = True

    bridge.send_command("ramiona")
    drain_writes(bridge, 1)
    bridge.last_listen_off_sent = (
        time.monotonic() - bridge.config.LISTEN_OFF_RETRY_SECONDS
    )
    drain_writes(bridge, 1)

    assert serial.writes == [listen_off_payload(bridge), listen_off_payload(bridge)]


def test_optimistic_then_stale_frame_does_not_twitch():
    state = ChairState()
    state.note_frame(frame(heat=0x00))

    outcome = state.apply_command("ogrzewanie", seq=1)
    assert outcome.should_send
    assert state.snapshot()["zones"]["ogrzewanie"] is True

    state.note_frame(frame(heat=0x00))
    assert state.snapshot()["zones"]["ogrzewanie"] is True


def test_disagreeing_foot_speed_press_surrenders_without_retry():
    # foot_speed_level is verifiable from frame byte 7 (0x00=1, 0x0C=2, 0x0F=3).
    # Press predkosc_masazu_stop expecting 3, but the frame keeps showing
    # 0x0C (level 2) → verify_command returns disagree, no retry budget,
    # so it surrenders to failed_commands.
    state, bridge, _serial = connected_bridge()
    state.foot_speed_level = 2
    seq = bridge.send_command("predkosc_masazu_stop")
    drain_writes(bridge, 1)

    bridge.note_backend_line(f"ACK seq={seq} code=0x02")
    bridge.note_backend_line(f"DONE seq={seq} code=0x02")
    state.note_frame(frame(foot_b7=0x0C))  # disagrees: model 3, frame 2

    assert seq not in bridge.in_flight
    assert seq not in bridge.acked
    assert seq in bridge.verifying
    assert bridge.pending_count() == 1

    pending_snapshot = state.snapshot()
    assert pending_snapshot["levels"]["foot_speed"] == 3
    assert not pending_snapshot["failed_commands"]

    expire_verifications(bridge)
    snapshot = state.snapshot()
    # Per spec: model is truth even on disagreement.
    assert snapshot["levels"]["foot_speed"] == 3
    assert snapshot["last_error"]
    assert snapshot["failed_commands"]


def test_predkosc_masazu_stop_does_not_raise_false_unverified():
    # Live 2026-04-30 false positives came from byte 6 not being 0x0F even
    # though raw byte 7 carried the correct foot-speed bucket.
    state, bridge, _serial = connected_bridge()
    state.foot_speed_level = 2
    seq = bridge.send_command("predkosc_masazu_stop")
    drain_writes(bridge, 1)
    bridge.note_backend_line(f"ACK seq={seq} code=0x02")
    bridge.note_backend_line(f"DONE seq={seq} code=0x02")
    state.note_frame(frame(b6=0x00, foot_b7=0x0F))
    expire_verifications(bridge)

    snapshot = state.snapshot()
    assert seq in bridge.completed
    assert snapshot["levels"]["foot_speed"] == 3
    assert snapshot["unverified_commands"] == []
    assert snapshot["failed_commands"] == []


def test_predkosc_masazu_stop_updates_ui_state():
    state = powered_on_state()
    assert state.foot_massage_on is True
    assert state.foot_speed_level == 2

    state.apply_command("predkosc_masazu_stop")

    assert state.foot_speed_level == 3
    assert state.snapshot()["levels"]["foot_speed"] == 3


def test_predkosc_masazu_stop_verifier_uses_correct_fields():
    state = powered_on_state()
    state.apply_command("predkosc_masazu_stop")
    state.note_frame(frame(b6=0x00, foot_b7=0x0F))
    result = state.verify_command(
        "predkosc_masazu_stop",
        seq=44,
        fields={"foot_speed_level"},
        expected={"foot_speed_level": 3},
    )

    assert result.agreed is True
    assert result.checked == 1
    assert any("verify foot-speed seq=44" in line for line in state.backend_log)


def test_unrelated_commands_still_verify_strictly():
    state = powered_on_state()
    state.intensity_level = 3
    state.note_frame(frame(b5=0x0C))

    result = state.verify_command(
        "sila_nacisku_plus",
        seq=45,
        fields={"intensity_level"},
        expected={"intensity_level": 3},
    )

    assert result.agreed is False
    assert result.unverified is False
    assert result.disagreements == [
        {"field": "intensity_level", "expected": 3, "actual": 2}
    ]


def test_predkosc_masazu_stop_intermediate_byte_7_does_not_warn_unverified():
    # Safety net: even when byte 7 is not one of the known buckets (so the
    # frame-side returns UNKNOWN), pressing predkosc_masazu_stop must not
    # produce a user-visible UNVERIFIED warning. The MODEL_ONLY_COMMANDS
    # auto-pass covers the genuinely-unreadable case while strict checks
    # remain intact when byte 7 IS readable.
    state = powered_on_state()
    state.foot_speed_level = 3
    state.note_frame(frame(foot_b7=0x05))  # transient, not 0x00/0x0C/0x0F

    result = state.verify_command(
        "predkosc_masazu_stop",
        seq=46,
        fields={"foot_speed_level"},
        expected={"foot_speed_level": 3},
    )

    assert result.agreed is True
    assert result.unverified is False
    assert result.checked == 0


def test_predkosc_masazu_stop_real_disagree_still_surrenders():
    # MODEL_ONLY_COMMANDS auto-pass must not mask a real disagreement
    # when byte 7 IS one of the known buckets and contradicts the model.
    state, bridge, _serial = connected_bridge()
    state.foot_speed_level = 2
    seq = bridge.send_command("predkosc_masazu_stop")
    drain_writes(bridge, 1)
    bridge.note_backend_line(f"ACK seq={seq} code=0x02")
    bridge.note_backend_line(f"DONE seq={seq} code=0x02")
    state.note_frame(frame(foot_b7=0x0C))  # model expects 3, frame says 2
    expire_verifications(bridge)

    snapshot = state.snapshot()
    assert snapshot["unverified_commands"] == []
    assert snapshot["failed_commands"]
    assert snapshot["last_error"]


def test_explicitly_retry_safe_command_retries_once(monkeypatch):
    # predkosc_masazu_stop is verifiable (foot_speed_level via byte 7).
    # With it in RETRY_SAFE_COMMANDS, the bridge re-sends it once before
    # surrendering on a real disagreement.
    monkeypatch.setattr(
        firmware_module, "RETRY_SAFE_COMMANDS", {"predkosc_masazu_stop"}
    )
    state, bridge, _serial = connected_bridge()
    state.foot_speed_level = 2
    seq = bridge.send_command("predkosc_masazu_stop")
    drain_writes(bridge, 1)
    bridge.note_backend_line(f"ACK seq={seq} code=0x02")
    bridge.note_backend_line(f"DONE seq={seq} code=0x02")
    state.note_frame(frame(foot_b7=0x0C))  # disagrees with model's 3

    assert seq not in bridge.in_flight
    assert seq not in bridge.acked
    assert seq in bridge.verifying
    assert bridge.pending_count() == 1

    expire_verifications(bridge)
    assert bridge.pending_count() == 2


def test_disagreeing_frame_after_retry_surrenders_to_frame(monkeypatch):
    monkeypatch.setattr(
        firmware_module, "RETRY_SAFE_COMMANDS", {"predkosc_masazu_stop"}
    )
    state, bridge, _serial = connected_bridge()
    state.foot_speed_level = 2
    seq = bridge.send_command("predkosc_masazu_stop")
    drain_writes(bridge, 1)
    bridge.note_backend_line(f"ACK seq={seq} code=0x02")
    bridge.note_backend_line(f"DONE seq={seq} code=0x02")
    state.note_frame(frame(foot_b7=0x0C))
    expire_verifications(bridge)

    retry_seq = next(iter(bridge.in_flight))
    drain_writes(bridge, 1)
    bridge.note_backend_line(f"ACK seq={retry_seq} code=0x02")
    bridge.note_backend_line(f"DONE seq={retry_seq} code=0x02")
    state.note_frame(frame(foot_b7=0x0C))
    expire_verifications(bridge)

    snapshot = state.snapshot()
    assert snapshot["last_error"]
    assert snapshot["failed_commands"]


def test_nack_surrenders_without_timeout():
    state, bridge, _serial = connected_bridge()
    seq = bridge.send_command("ramiona")
    drain_writes(bridge, 1)

    bridge.note_backend_line(f"NACK seq={seq} code=0x13 error=queue_full")

    snapshot = state.snapshot()
    assert seq not in bridge.in_flight
    assert seq not in bridge.acked
    assert "firmware rejected ramiona" in snapshot["last_error"]
    assert snapshot["failed_commands"][0]["disagreements"][0]["actual"] == "queue_full"


def test_model_only_toggle_owns_model_without_warning():
    # Per spec: model is the truth for zones. masaz_stop has no confirmed
    # frame mapping, but that is expected. After ACK/DONE + fresh frame the
    # toggle stays in the model and completes without a stale warning chip.
    state, bridge, _serial = connected_bridge()
    initial = state.snapshot()["zones"]["masaz_stop"]
    assert initial is True  # default-A applied on connect: foot_massage_on=True

    seq = bridge.send_command("masaz_stop")
    drain_writes(bridge, 1)
    bridge.note_backend_line(f"ACK seq={seq} code=0x0D")
    bridge.note_backend_line(f"DONE seq={seq} code=0x0D")
    state.note_frame(frame())  # fresh post-DONE frame so verify settles

    # Press optimistically toggled the model to False; mute window shields it.
    assert state.snapshot()["zones"]["masaz_stop"] is False
    expire_verifications(bridge)

    snapshot = state.snapshot()
    assert snapshot["zones"]["masaz_stop"] is False  # model wins
    assert snapshot["failed_commands"] == []
    assert snapshot["unverified_commands"] == []
    assert snapshot["last_error"] is None
    assert seq in bridge.completed


def test_delayed_agreeing_frame_after_done_completes():
    state = ChairState()
    state.set_connection(True, "/dev/fake")
    state.note_frame(frame(payload_zero=True))
    serial = FakeSerial()
    bridge = FirmwareSerialBridge(state=state, baud_rate=115200)
    bridge.serial_handle = serial

    seq = bridge.send_command("power")
    drain_writes(bridge, 1)
    state.note_frame(frame(payload_zero=True))
    bridge.note_backend_line(f"ACK seq={seq} code=0x01")
    bridge.note_backend_line(f"DONE seq={seq} code=0x01")

    assert seq in bridge.verifying
    state.note_frame(frame(zone=0x07))
    expire_verifications(bridge)

    snapshot = state.snapshot()
    assert seq in bridge.completed
    assert snapshot["power_on"] is True
    assert snapshot["last_error"] is None
    assert snapshot["failed_commands"] == []


def test_drift_detection_after_2s():
    # Drift now flags only fields with stable frame mappings. Zones, heat,
    # timer, and continuous auto-profile tail are model-owned / transient.
    state = ChairState()
    state.note_frame(frame(b3=0x04, b4=0x0C))  # manual-back signature
    state.mode = "auto"
    state._drift_first_seen["mode"] = time.monotonic() - 2.1
    disagreements = state.assert_frame_consistent()

    assert any(item["field"] == "mode" for item in disagreements)
    assert any(item["field"] == "mode" for item in state.snapshot()["drift"])


def test_profile_tail_does_not_create_continuous_drift_after_czas():
    state = powered_on_state()
    state.apply_command("tryb_automatyczny")
    assert state.auto_profile == "B"

    # A later czas/countdown frame can reuse the old A-default tail bytes.
    # That tail is valid for command verification, but not stable enough for
    # continuous auto-profile drift because the tail also changes with timer
    # state.
    state.note_frame(frame(tail=(0x0A, 0x0B, 0x0C, 0x08)))
    state._drift_first_seen["auto_profile"] = time.monotonic() - 3.0

    disagreements = state.assert_frame_consistent()
    assert not any(item["field"] == "auto_profile" for item in disagreements)


def test_initial_auto_sync_with_unknown_tail_does_not_invent_default_a():
    state = ChairState()
    # This tail was captured during countdown and is not one of the stable
    # tryb_automatyczny profile tails. The attach path must not claim
    # default-A zones from it.
    state.note_frame(frame(tail=(0x0A, 0x02, 0x0E, 0x08)))
    snapshot = state.snapshot()

    assert snapshot["power_on"] is True
    assert snapshot["mode"] == "auto"
    assert snapshot["auto_profile"] is None
    assert "Tryb_automatyczny-A" not in snapshot["layers"]["visible"]
    assert snapshot["zones"] == {
        "ramiona": False,
        "przedramiona": False,
        "nogi": False,
        "masaz_posladkow": False,
        "masaz_stop": False,
        "szyja": False,
        "plecy_i_talia": False,
        "ogrzewanie": False,
    }


def test_tryb_automatyczny_from_unknown_auto_adopts_post_command_tail():
    state = ChairState()
    state.note_frame(frame(tail=(0x0A, 0x02, 0x0E, 0x08)))
    assert state.mode == "auto"
    assert state.auto_profile is None

    outcome = state.apply_command("tryb_automatyczny", seq=7)
    assert outcome.should_send is True
    assert outcome.muted_fields == set()
    assert outcome.expected_fields == {}
    assert state.auto_profile is None

    state.note_frame(frame(tail=(0x04, 0x09, 0x0E, 0x00)))
    snapshot = state.snapshot()
    assert snapshot["auto_profile"] == "C"
    assert "Tryb_automatyczny-C" in snapshot["layers"]["visible"]
    assert snapshot["zones"]["szyja"] is True
    assert snapshot["zones"]["plecy_i_talia"] is True
    assert snapshot["zones"]["masaz_posladkow"] is True
    assert snapshot["zones"]["ramiona"] is True
    assert snapshot["zones"]["masaz_stop"] is False
    assert snapshot["zones"]["przedramiona"] is False
    assert snapshot["zones"]["nogi"] is False


def test_frame_parser_dropped_byte():
    parser = FullFrameParser()
    good = frame()
    stream = good[1:] + good
    parsed = []
    for value in stream:
        parsed.extend(parser.feed(value))
    assert parsed == [good]


def test_frame_parser_doubled_AA():
    parser = FullFrameParser()
    good = frame()
    stream = [0xAA] + good
    parsed = []
    for value in stream:
        parsed.extend(parser.feed(value))
    assert parsed == [good]


def test_frame_parser_spurious_AA_back_to_back_trailing_garbage():
    parser = FullFrameParser()
    first = frame()
    second = frame(zone=0x03)
    first[10] = 0xAA
    stream = first + second + [0x99, 0xAA, 0x42]
    parsed = []
    for value in stream:
        parsed.extend(parser.feed(value))
    assert parsed == [first, second]


def test_bridge_accepts_compact_frame_line():
    state, bridge, _serial = connected_bridge()
    compact = "".join(f"{value:02X}" for value in frame(zone=0x03, heat=0x0C))

    bridge._consume_serial_chunk(f"FRAME {compact}\n".encode("ascii"))
    snapshot = state.snapshot()

    # Frame parsing succeeded and mode signature was recognized. Zone bits
    # in frame[21] / heat in frame[23] are no longer truth — the model owns
    # those, so we don't assert them here.
    assert snapshot["raw_frame"][21] == 0x03
    assert snapshot["raw_frame"][23] == 0x0C
    assert snapshot["power_on"] is True
    assert snapshot["mode"] == "auto"
    assert snapshot["frame_signature"] == "auto-running"


def test_reconnect_does_not_replay_queue():
    _state, bridge, serial = connected_bridge()
    assert bridge.send_command("ramiona") is not None
    bridge._disconnect()

    assert serial.closed
    assert bridge.pending_count() == 0
    assert bridge.in_flight == {}
    assert bridge.acked == {}


def test_listen_backoff():
    _state, bridge, _serial = connected_bridge()

    assert bridge._listen_retry_delay(0) == bridge.config.LISTEN_INITIAL_RETRY_SECONDS
    assert bridge._listen_retry_delay(3) > bridge._listen_retry_delay(2)
    assert bridge._listen_retry_delay(20) == bridge.config.LISTEN_MAX_RETRY_SECONDS


def test_power_debounce():
    _state, bridge, _serial = connected_bridge()

    first = bridge.send_command("power")
    second = bridge.send_command("power")

    assert first is not None
    assert second is None


def test_layer_set_matches_tutorial():
    assert AUTO_DEAD_COMMANDS == {
        "pauza",
        "predkosc_plus",
        "predkosc_minus",
        "do_przodu_do_tylu_1",
        "do_przodu_do_tylu_2",
    }


def test_no_current_chair_button_is_retry_safe():
    assert RETRY_SAFE_COMMANDS == frozenset()


def test_known_model_only_commands_do_not_warn_as_unverified():
    assert {"czas", "ogrzewanie", "masaz_stop", "ramiona"} <= MODEL_ONLY_COMMANDS


def test_lost_press_simulation():
    state, bridge, serial = connected_bridge()
    accepted: list[int] = []
    rejected = 0
    for _ in range(8):
        seq = bridge.send_command("ramiona")
        if seq is None:
            rejected += 1
        else:
            accepted.append(seq)

    assert len(accepted) == 1
    assert rejected == 7
    assert bridge.is_busy()
    drain_writes(bridge, 1)
    assert serial.writes == [f"{accepted[0]} ramiona\n".encode("ascii")]
    assert state.snapshot()["unverified_commands"] == []


def test_verify_unverified_when_no_confirmed_fields():
    # Unknown commands with no confirmed frame mapping still return
    # unverified; known model-only commands are covered separately below.
    state = ChairState()
    state.note_frame(frame())
    result = state.verify_command(
        "__unknown_probe__",
        seq=42,
        fields={"heat_on"},
        expected={"heat_on": True},
    )
    assert result.unverified is True
    assert result.agreed is False
    assert result.checked == 0
    assert result.disagreements == []


def test_model_only_command_with_no_confirmed_fields_completes():
    state = ChairState()
    state.note_frame(frame())
    result = state.verify_command(
        "ogrzewanie",
        seq=42,
        fields={"heat_on"},
        expected={"heat_on": True},
    )
    assert result.unverified is False
    assert result.agreed is True
    assert result.checked == 0
    assert result.disagreements == []


def _state_with_busy(state: ChairState, *, bridge_busy: bool = False) -> dict:
    snapshot = state.snapshot()
    snapshot["bridge_busy"] = bridge_busy
    return snapshot


def test_compute_state_etag_stable_across_volatile_fields():
    state = ChairState()
    state.note_frame(frame())
    snapshot_a = _state_with_busy(state)
    snapshot_b = dict(snapshot_a)
    snapshot_b["frame_age_ms"] = (snapshot_a.get("frame_age_ms") or 0) + 9999
    snapshot_b["time_text"] = "999"
    snapshot_b["command_history"] = [{"command": "noise"}]
    snapshot_b["backend_log"] = ["junk"]
    assert compute_state_etag(snapshot_a) == compute_state_etag(snapshot_b)


def test_compute_state_etag_changes_on_power_change():
    state = ChairState()
    state.note_frame(frame(payload_zero=True))
    off_etag = compute_state_etag(_state_with_busy(state))
    state.note_frame(frame())
    on_etag = compute_state_etag(_state_with_busy(state))
    assert off_etag != on_etag


def test_compute_state_etag_changes_on_unverified_append():
    state = ChairState()
    state.note_frame(frame())
    before = compute_state_etag(_state_with_busy(state))
    state.note_unverified_command("predkosc_masazu_stop", 7, {"foot_speed_level"})
    after = compute_state_etag(_state_with_busy(state))
    assert before != after


def test_compute_state_etag_changes_on_remaining_seconds_tick():
    state = ChairState()
    state.note_frame(frame())
    before = compute_state_etag(_state_with_busy(state))
    state.remaining_seconds = max(0, state.remaining_seconds - 1)
    after = compute_state_etag(_state_with_busy(state))
    assert before != after


def test_model_only_terminal_state():
    state, bridge, _serial = connected_bridge()
    seq = bridge.send_command("ogrzewanie")  # heat_on is model-owned
    drain_writes(bridge, 1)
    bridge.note_backend_line(f"ACK seq={seq} code=0x03")
    bridge.note_backend_line(f"DONE seq={seq} code=0x03")
    state.note_frame(frame())
    expire_verifications(bridge)

    snapshot = state.snapshot()
    assert snapshot["unverified_commands"] == []
    assert snapshot["failed_commands"] == []
    assert snapshot["last_error"] is None
    assert seq in bridge.completed


def test_default_host_is_lan_accessible(monkeypatch):
    monkeypatch.delenv("CHAIR_BRIDGE_HOST", raising=False)

    args = app_module.parse_args([])

    assert bridge_config.DEFAULT_HOST == "0.0.0.0"
    assert app_module.resolve_bind_host(args) == "0.0.0.0"


def test_local_flag_uses_loopback():
    args = app_module.parse_args(["--local"])

    assert app_module.resolve_bind_host(args) == "127.0.0.1"


def test_public_url_uses_lan_ip_not_bind_host():
    url = build_public_url("0.0.0.0", 8080, "192.168.1.42")

    assert url == "http://192.168.1.42:8080"
    assert "0.0.0.0" not in url


def test_lan_ip_prefers_default_route_over_virtual_interface(monkeypatch):
    monkeypatch.setattr(
        http_server_module,
        "_iter_interface_ipv4s",
        lambda: ["172.17.0.1", "192.168.1.42"],
    )
    monkeypatch.setattr(http_server_module, "_iter_hostname_ipv4s", lambda: [])
    monkeypatch.setattr(
        http_server_module,
        "_ip_from_udp_route",
        lambda _target: "192.168.1.42",
    )

    assert http_server_module.get_lan_ip() == "192.168.1.42"


def test_lan_ip_detection_skips_virtual_bridge_interfaces():
    assert _is_likely_virtual_interface("docker0")
    assert _is_likely_virtual_interface("br-1234")
    assert _is_likely_virtual_interface("vethabc")
    assert not _is_likely_virtual_interface("wlan0")
    assert not _is_likely_virtual_interface("enp3s0")


def test_startup_output_is_minimal():
    public_url = "http://192.168.1.42:8080"
    browser_url = "http://127.0.0.1:8080"
    output = app_module.build_startup_output(public_url, browser_url)

    assert public_url in output
    assert browser_url in output
    assert "NCNI Massage Chair Control Panel" in output
    # No terminal QR. No implementation noise. No raw bind host.
    assert "QR" not in output
    assert "Scan" not in output
    assert "Bound to" not in output
    assert "Network:" not in output
    assert "DEBUG" not in output
    assert "0.0.0.0" not in output


def test_startup_output_no_browser_disables_open_message():
    output = app_module.build_startup_output(
        "http://192.168.1.42:8080",
        "http://127.0.0.1:8080",
        browser_enabled=False,
    )

    assert "Browser auto-open disabled" in output
    assert "Opening browser" not in output


def test_print_startup_banner_writes_minimal_lines(monkeypatch):
    capture = StringIO()
    with redirect_stdout(capture):
        public = app_module.print_startup_banner("0.0.0.0", 8080, "192.168.1.42")

    output = capture.getvalue()
    assert public == "http://192.168.1.42:8080"
    assert "http://192.168.1.42:8080" in output
    assert "http://127.0.0.1:8080" in output
    assert "0.0.0.0" not in output
    # No terminal QR, ever, on the doctor banner.
    assert "QR" not in output


def test_no_browser_flag_is_recognised():
    args = app_module.parse_args(["--no-browser"])
    assert args.no_browser is True


def test_browser_target_url_uses_loopback_when_bound_lan():
    url = app_module._browser_target_url("0.0.0.0", 8080, "192.168.1.42")
    assert url == "http://127.0.0.1:8080"


def test_browser_target_url_uses_lan_when_explicitly_bound():
    url = app_module._browser_target_url("192.168.1.42", 8080, "192.168.1.42")
    assert url == "http://127.0.0.1:8080"


def test_open_browser_async_returns_started_timer(monkeypatch):
    calls = []
    monkeypatch.setattr(
        app_module, "open_browser", lambda url: (calls.append(url), True)[1]
    )

    timer = app_module.open_browser_async("http://127.0.0.1:8080", 0.0)
    timer.join(timeout=1.0)

    assert calls == ["http://127.0.0.1:8080"]


def test_network_page_uses_lan_url():
    html = build_network_page(8080, "192.168.1.42", "0.0.0.0")

    assert "http://192.168.1.42:8080" in html
    assert "0.0.0.0" not in html
    assert "localhost" not in html


def test_qr_svg_encodes_lan_url_when_qrcode_available(monkeypatch):
    captured = {}

    class FakeImage:
        def save(self, buf):
            buf.write(b"<svg>fake</svg>")

    class FakeQRCode:
        def __init__(self, **_kwargs):
            pass

        def add_data(self, data):
            captured["data"] = data

        def make(self, *, fit):
            captured["fit"] = fit

        def make_image(self, *, image_factory):
            captured["image_factory"] = image_factory
            return FakeImage()

    fake_qrcode = types.SimpleNamespace(
        constants=types.SimpleNamespace(ERROR_CORRECT_L=1),
        QRCode=FakeQRCode,
        image=types.SimpleNamespace(svg=types.SimpleNamespace(SvgPathImage=object())),
    )
    monkeypatch.setattr(http_server_module, "HAS_QRCODE", True)
    monkeypatch.setattr(http_server_module, "qrcode", fake_qrcode)

    svg = http_server_module.generate_qr_svg("http://192.168.1.42:8080")

    assert svg == "<svg>fake</svg>"
    assert captured["data"] == "http://192.168.1.42:8080"
    assert captured["fit"] is True


# ---------------------------------------------------------------------------
# Packaging tests: in-UI block, /api/network, file inventory, launchers.
# ---------------------------------------------------------------------------


def test_root_view_markup_includes_connection_block():
    from bridge.svg import load_root_view_markup

    markup = load_root_view_markup(bridge_config.ROOT_VIEW_PATH)

    # Connection panel + QR exist in the markup but only inside the
    # help modal — they must not be permanently visible on the main UI.
    assert 'class="connection-panel"' in markup
    assert 'class="qr-panel"' in markup
    assert 'id="helpModal"' in markup
    # The modal is hidden by default. Anything else would put a giant
    # debug card permanently on top of the operator controls.
    assert 'id="helpModal"' in markup and "hidden" in markup
    # Tiny "Sieć" pill button is the only network/help affordance on
    # the main screen.
    assert 'id="helpPill"' in markup
    assert ">Sieć<" in markup or ">Siec<" in markup

    assert "panelAddress" in markup
    assert "lanAddress" in markup
    assert "lanIp" in markup
    assert "panelQr" in markup
    assert "/api/network" in (
        Path(bridge_config.ROOT) / "static/root-view.js"
    ).read_text(encoding="utf-8")
    # Universal font stack must be present so device-specific fonts do
    # not break Polish labels.
    assert "system-ui" in markup
    assert "BlinkMacSystemFont" in markup
    # Power-icon stroke fix: no thick 8px center line.
    assert 'stroke-width="6"' in markup
    assert 'stroke-width="8"' not in markup
    assert "0.0.0.0" not in markup
    # Long-label classes — at least one button uses them so multi-word
    # Polish labels do not overflow on small landscape phones.
    assert "label--long" in markup
    assert "label--xlong" in markup
    assert "label--two-line" in markup
    # Autofit JS hook lives in static/root-view.js.
    js = (Path(bridge_config.ROOT) / "static/root-view.js").read_text(encoding="utf-8")
    assert "is-autofit" in js
    # No "Electric Chair" leaks into the doctor-facing operator UI.
    lower = markup.lower()
    assert "electric chair" not in lower
    assert "ai_codex" not in lower


def test_api_network_payload_has_lan_url_and_port():
    from bridge.http_server import build_network_payload

    payload = build_network_payload(8080, "192.168.1.42", "0.0.0.0")

    assert payload["lan_url"] == "http://192.168.1.42:8080"
    assert payload["port"] == 8080
    assert payload["local_only"] is False
    assert "0.0.0.0" not in payload["lan_url"]
    assert payload["qr_url"] == "/qr.svg"
    assert payload["qr_available"] in {True, False}
    assert "NCNI" in payload["product"]


def test_api_network_payload_local_only_when_loopback():
    from bridge.http_server import build_network_payload

    payload = build_network_payload(8080, "192.168.1.42", "127.0.0.1")

    assert payload["local_only"] is True
    assert payload["lan_ip"] == "127.0.0.1"


def test_required_packaging_files_exist():
    repo = Path(bridge_config.ROOT)
    must_exist = [
        "app.py",
        "README.md",
        "requirements.txt",
        "install.py",
        "update.py",
        "ROOT-VIEW.html",
        # Renamed launchers — the doctor sees these immediately.
        "START_WINDOWS.bat",
        "START_MACOS.command",
        "START_LINUX.sh",
        "INSTALL_WINDOWS.bat",
        "INSTALL_MACOS.command",
        "INSTALL_LINUX.sh",
        "UPDATE_WINDOWS.bat",
        "UPDATE_MACOS.command",
        "UPDATE_LINUX.sh",
        "tools/verify_installation.py",
        "tools/build_release_package.py",
        "tools/restart_linux.sh",
        "docs/file_inventory.md",
        "docs/CREDITS.md",
        "docs/SAFETY.md",
        "docs/BASIC_TROUBLESHOOTING.md",
    ]
    missing = [rel for rel in must_exist if not (repo / rel).exists()]
    assert not missing, f"missing required packaging files: {missing}"


def test_legacy_launcher_names_are_gone():
    # Doctors should see only the obvious uppercase names.
    repo = Path(bridge_config.ROOT)
    for legacy in (
        "Start_NCNI_Massage_Chair.bat",
        "Start_NCNI_Massage_Chair.command",
        "start_ncni_massage_chair.sh",
    ):
        assert not (repo / legacy).exists(), f"legacy launcher still present: {legacy}"


def test_archived_cleanup_files_are_not_in_source_tree():
    repo = Path(bridge_config.ROOT)
    for archived in (
        "static/index.html",
        "docs/NCNI_project_context.txt",
        "assets/reference/ui_ip_qr_layout_idea.png",
        "assets/screenshots/problem_mobile_layout_before.png",
        "RESTART_LINUX.sh",
    ):
        assert not (repo / archived).exists(), f"archived file still present: {archived}"


def test_no_user_facing_electric_chair_string():
    """No clinic-facing file may say "Electric Chair"."""
    repo = Path(bridge_config.ROOT)
    targets = [
        "README.md",
        "ROOT-VIEW.html",
        "docs/CREDITS.md",
        "docs/SAFETY.md",
        "docs/BASIC_TROUBLESHOOTING.md",
        "START_WINDOWS.bat",
        "START_MACOS.command",
        "START_LINUX.sh",
        "INSTALL_WINDOWS.bat",
        "INSTALL_MACOS.command",
        "INSTALL_LINUX.sh",
    ]
    for rel in targets:
        body = (repo / rel).read_text(encoding="utf-8").lower()
        assert "electric chair" not in body, f"{rel} still says 'electric chair'"
        assert "ai_codex" not in body, f"{rel} still says 'AI_CODEX'"


def test_readme_links_to_existing_files():
    readme = (Path(bridge_config.ROOT) / "README.md").read_text(encoding="utf-8")
    for relative in (
        "docs/file_inventory.md",
        "docs/SAFETY.md",
        "docs/CREDITS.md",
    ):
        assert relative in readme, f"README does not mention {relative}"
        assert (Path(bridge_config.ROOT) / relative).exists()


def test_verify_installation_report_includes_hardware_not_validated(tmp_path):
    from tools import verify_installation

    report = tmp_path / "verification_report.txt"
    checks = verify_installation.run_checks()
    verify_installation.write_report(checks, report)
    text = report.read_text(encoding="utf-8")

    assert "PHYSICAL CHAIR NOT VALIDATED" in text
    assert "Physical chair not validated" in text


def test_release_package_top_level_is_minimal(tmp_path):
    """Release root must contain exactly the 5 things a doctor sees:
    README.md + START_WINDOWS.bat + START_MACOS.command + START_LINUX.sh + app/."""
    from tools import build_release_package

    out = tmp_path / "NCNI_Massage_Chair_Control"
    build_release_package.build(out)

    top_level = sorted(p.name for p in out.iterdir())
    assert top_level == [
        "README.md",
        "START_LINUX.sh",
        "START_MACOS.command",
        "START_WINDOWS.bat",
        "app",
    ], f"unexpected release top-level: {top_level}"

    # Internals must live under app/, not at the root.
    for hidden in ("app.py", "requirements.txt", "ROOT-VIEW.html", "bridge", "static"):
        assert (out / "app" / hidden).exists(), f"missing app/{hidden}"
        assert not (out / hidden).exists(), f"{hidden} should be inside app/"

    # INSTALL_* and UPDATE_* are technician-only; they belong in app/.
    for tech in (
        "INSTALL_WINDOWS.bat",
        "INSTALL_MACOS.command",
        "INSTALL_LINUX.sh",
        "UPDATE_WINDOWS.bat",
        "UPDATE_MACOS.command",
        "UPDATE_LINUX.sh",
    ):
        assert not (out / tech).exists(), f"{tech} must not be at the top level"
        assert (out / "app" / tech).exists(), f"missing app/{tech}"


def test_release_package_excludes_dev_junk(tmp_path):
    """No git, no caches, no tests, no developer junk anywhere in the release."""
    from tools import build_release_package

    out = tmp_path / "NCNI_Massage_Chair_Control"
    build_release_package.build(out)

    forbidden_dirs = {
        ".git",
        ".claude",
        ".codex",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "tests",
        "developer",
        "COORDINATION",
        "_archive_unused",
        "electric_chair_firmware",
        "notes",
    }
    for path in out.rglob("*"):
        rel = path.relative_to(out).as_posix()
        for bad in forbidden_dirs:
            assert bad not in rel.split("/"), (
                f"forbidden dir {bad!r} present in release at {rel}"
            )

    for path in out.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix in {".png", ".jpg", ".svg", ".zip"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        assert "electric chair" not in text.lower(), (
            f"release file leaks 'electric chair': {path.relative_to(out)}"
        )
        assert "ai_codex" not in text.lower(), (
            f"release file leaks 'AI_CODEX': {path.relative_to(out)}"
        )

    assert not (out / "verification_report.txt").exists()
    # The packager itself must not be shipped.
    assert not (out / "app" / "tools" / "build_release_package.py").exists()


def test_release_top_level_readme_is_short_and_doctor_facing(tmp_path):
    from tools import build_release_package

    out = tmp_path / "NCNI_Massage_Chair_Control"
    build_release_package.build(out)
    text = (out / "README.md").read_text(encoding="utf-8")

    # Doctor-facing release README must be tight (under ~2.5 KB).
    assert len(text) < 2500, f"top-level README too long: {len(text)} bytes"
    assert "START_WINDOWS.bat" in text
    assert "START_MACOS.command" in text
    assert "START_LINUX.sh" in text
    assert "kontakt@ncni.pl" in text
    # No leaks.
    assert "electric chair" not in text.lower()
    # No developer noise at the top level.
    assert "pytest" not in text.lower()
    assert "ruff " not in text.lower()


# ---------------------------------------------------------------------------
# Spec tests: state machine matches the Polish manual + matrix file (S1).
# ---------------------------------------------------------------------------


def powered_on_state() -> ChairState:
    state = ChairState()
    state.apply_command("power")
    # Existing tests model "chair has finished booting". Clear the
    # boot-settle gate so manual zone / direction / scalar commands are
    # accepted without further setup. New tests for the boot-settle gate
    # explicitly do NOT use this helper.
    _force_boot_settled(state)
    return state


def _force_boot_settled(state: ChairState) -> None:
    """Pretend the chair has finished its post-power-on boot phase."""
    state.boot_settle_started_at = None
    state._running_frames_since_boot = 0


def _known_manual_back_state() -> ChairState:
    state = powered_on_state()
    state.apply_command("plecy_i_talia")
    state.back_direction_known = True
    state.back_forward_cycle_1 = 1
    return state


def _known_manual_neck_state() -> ChairState:
    state = powered_on_state()
    state.apply_command("szyja")
    state.neck_direction_known = True
    state.back_forward_cycle_2 = 2
    return state


def test_power_on_default_matches_manual():
    # "DEFAULTOWY TRYB PO KLIKNĘCIU POWER, WŁĄCZONE SĄ" — every field listed
    # in the Polish manual must match after one power press from off.
    state = powered_on_state()
    assert state.power_on is True
    assert state.mode == "auto"
    assert state.auto_profile == "A"
    assert state.intensity_level == 2
    assert state.speed_level == 2
    assert state.foot_speed_level == 2
    assert state.timer_minutes == 15
    assert state.remaining_seconds == 15 * 60
    assert state.shoulders_on is True
    assert state.forearms_on is True
    assert state.legs_on is True
    assert state.foot_massage_on is True  # Masaz_Stop in defaults
    assert state.neck_on is True
    assert state.back_waist_on is True
    assert state.full_body_on is True
    assert state.buttocks_on is False  # not in manual defaults
    assert state.heat_on is False
    assert state.paused is False

    visible = set(state.snapshot()["layers"]["visible"])
    for layer in (
        "Background",
        "Body",
        "Czas-TEXT",
        "Czas-NUMBER",
        "Tryb_automatyczny",
        "Tryb_automatyczny-A",
        "Ramiona",
        "Przedramiona",
        "Nogi",
        "Masaz_Stop",
        "Szyja",
        "? Szyja",
        "Plecy_i_talia",
        "? Plecy_i_talia",
    ):
        assert layer in visible, f"missing default layer: {layer}"


def test_auto_profile_cycle_A_B_C_D_A_with_correct_visibility():
    # Manual section 14 of !!AUTO!!: pressing tryb_automatyczny in auto cycles
    # A → B → C → D → A. Each profile has a specific zone set and F1..F4
    # prompt; timer resets to 15:00.
    state = powered_on_state()
    assert state.auto_profile == "A"

    state.apply_command("tryb_automatyczny")
    assert state.auto_profile == "B"
    assert state.prompt_text == "F2"
    assert state.shoulders_on and state.forearms_on and state.legs_on
    assert state.neck_on and state.back_waist_on and state.buttocks_on
    assert state.foot_massage_on
    assert state.intensity_level == 2

    state.apply_command("tryb_automatyczny")
    assert state.auto_profile == "C"
    assert state.prompt_text == "F3"
    # User bench correction: Profile C shows szyja, plecy_i_talia,
    # masaz_posladkow, and ramiona. The Polish manual's C zone list is wrong.
    assert state.neck_on
    assert state.back_waist_on
    assert state.buttocks_on
    assert state.shoulders_on
    assert not state.foot_massage_on
    assert not state.legs_on
    assert not state.forearms_on
    visible = set(state.snapshot()["layers"]["visible"])
    assert "Predkosc_masazu_stop" not in visible
    assert "PredkoscTEXT" in visible

    state.apply_command("tryb_automatyczny")
    assert state.auto_profile == "D"
    assert state.prompt_text == "F4"
    assert state.shoulders_on and state.forearms_on and state.legs_on
    assert state.neck_on and state.back_waist_on and state.buttocks_on
    assert state.foot_massage_on
    visible = set(state.snapshot()["layers"]["visible"])
    assert "Predkosc_masazu_stop" in visible

    state.apply_command("tryb_automatyczny")
    assert state.auto_profile == "A"
    assert state.prompt_text == "F1"
    # Profile A is unified: cycled A matches the default power-on profile.
    assert state.shoulders_on and state.forearms_on and state.legs_on
    assert state.neck_on
    assert state.back_waist_on
    assert state.foot_massage_on
    assert not state.buttocks_on


def test_szyja_in_auto_switches_to_manual_neck():
    state = powered_on_state()
    state.apply_command("szyja")
    assert state.mode == "manual"
    assert state.auto_profile is None
    assert state.neck_on is True
    assert state.back_waist_on is False
    assert state.shoulders_on is False
    assert state.forearms_on is False
    assert state.legs_on is False
    assert state.buttocks_on is False
    assert state.foot_massage_on is False
    assert state.prompt_text == ""
    assert state.snapshot()["direction"]["neck_known"] is False
    visible = set(state.snapshot()["layers"]["visible"])
    assert "Tryb_manualny" in visible
    assert "Tryb_automatyczny" not in visible
    assert "Szyja" in visible
    assert "? Szyja" in visible
    assert "Plecy_i_talia" not in visible


def test_plecy_i_talia_in_auto_switches_to_manual_back():
    state = powered_on_state()
    state.apply_command("plecy_i_talia")
    assert state.mode == "manual"
    assert state.auto_profile is None
    assert state.back_waist_on is True
    assert state.neck_on is False
    assert state.shoulders_on is False
    assert state.forearms_on is False
    assert state.legs_on is False
    assert state.buttocks_on is False
    assert state.foot_massage_on is False
    assert state.prompt_text == ""
    assert state.snapshot()["direction"]["back_known"] is False
    visible = set(state.snapshot()["layers"]["visible"])
    assert "Tryb_manualny" in visible
    assert "Plecy_i_talia" in visible
    assert "? Plecy_i_talia" in visible
    assert "Szyja" not in visible


def test_szyja_from_auto_preserves_heat_on_late_regression():
    state = powered_on_state()
    state.apply_command("ogrzewanie")
    assert state.heat_on is True

    state.apply_command("szyja")

    assert state.mode == "manual"
    assert state.heat_on is True
    assert state.snapshot()["zones"]["ogrzewanie"] is True


def test_plecy_i_talia_from_auto_preserves_heat_on_late_regression():
    state = powered_on_state()
    state.apply_command("ogrzewanie")
    assert state.heat_on is True

    state.apply_command("plecy_i_talia")

    assert state.mode == "manual"
    assert state.heat_on is True
    assert state.snapshot()["zones"]["ogrzewanie"] is True


def test_heat_layer_visible_after_manual_entry():
    state = powered_on_state()
    state.apply_command("ogrzewanie")

    state.apply_command("szyja")

    visible = set(state.snapshot()["layers"]["visible"])
    assert "Ogrzewanie" in visible


def test_frontend_heat_layer_follows_structured_heat_state():
    repo_root = Path(__file__).resolve().parents[1]
    for relpath in ("static/root-view.js", "static/app.js"):
        source = (repo_root / relpath).read_text(encoding="utf-8")

        assert "function heatIsExplicitlyOff(state)" in source
        assert "state.zones && state.zones.ogrzewanie" in source
        assert "state.buttons.ogrzewanie.active" in source
        assert 'visible.delete("Ogrzewanie")' in source


def test_root_view_has_mobile_viewport_meta():
    repo_root = Path(__file__).resolve().parents[1]
    for relpath in ("ROOT-VIEW.html", "static/ROOT-VIEW.html"):
        source = (repo_root / relpath).read_text(encoding="utf-8")

        assert (
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            in source
        )


def test_root_view_has_landscape_orientation_handling():
    repo_root = Path(__file__).resolve().parents[1]
    source = "\n".join(
        [
            (repo_root / "ROOT-VIEW.html").read_text(encoding="utf-8"),
            (repo_root / "static/ROOT-VIEW.html").read_text(encoding="utf-8"),
            (repo_root / "static/app.css").read_text(encoding="utf-8"),
            (repo_root / "static/root-view.js").read_text(encoding="utf-8"),
        ]
    )

    assert "orientation-overlay" in source
    assert "@media (orientation: portrait)" in source
    assert "orientation.lock" in source


def test_root_view_uses_responsive_units():
    repo_root = Path(__file__).resolve().parents[1]
    source = "\n".join(
        [
            (repo_root / "ROOT-VIEW.html").read_text(encoding="utf-8"),
            (repo_root / "static/app.css").read_text(encoding="utf-8"),
        ]
    )

    for token in ("clamp(", "dvh", "svh", "vw", "aspect-ratio"):
        assert token in source


def test_chair_layers_are_not_reparented_or_removed():
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "ROOT-VIEW.html").read_text(encoding="utf-8")

    for token in (
        'class="screen"',
        'inkscape:label="Background"',
        'inkscape:label="Body"',
        'inkscape:label="Ramiona"',
        'inkscape:label="Czas-NUMBER"',
    ):
        assert token in source


def test_frontend_state_bindings_still_exist():
    repo_root = Path(__file__).resolve().parents[1]
    root_html = (repo_root / "ROOT-VIEW.html").read_text(encoding="utf-8")
    static_html = (repo_root / "static/ROOT-VIEW.html").read_text(encoding="utf-8")
    root_js = (repo_root / "static/root-view.js").read_text(encoding="utf-8")

    for token in (
        'class="power js-toggle"',
        'class="stack stack--left"',
        'class="stack stack--right"',
        'class="bottom-grid"',
    ):
        assert token in root_html

    for token in (
        '".stack--left .btn"',
        '".stack--right .btn"',
        '".bottom-grid .btn"',
        "MANAGED_LAYERS",
    ):
        assert token in root_js

    for token in (
        'id="svgMount"',
        'id="serialStatus"',
        'id="modeStatus"',
        'data-command="power"',
        'data-command="ogrzewanie"',
    ):
        assert token in static_html


def test_portrait_overlay_copy_exists():
    repo_root = Path(__file__).resolve().parents[1]
    source = "\n".join(
        [
            (repo_root / "ROOT-VIEW.html").read_text(encoding="utf-8"),
            (repo_root / "static/ROOT-VIEW.html").read_text(encoding="utf-8"),
        ]
    )

    assert "Rotate device to landscape" in source


def test_no_desktop_only_fixed_app_dimensions():
    repo_root = Path(__file__).resolve().parents[1]
    root_html = (repo_root / "ROOT-VIEW.html").read_text(encoding="utf-8")
    app_css = (repo_root / "static/app.css").read_text(encoding="utf-8")

    assert "inline-size: min(" in root_html
    assert "block-size: 100dvh" in root_html
    assert "width: min(100vw, 1600px)" in app_css
    assert "width: 1460px" not in app_css
    assert "height: 788px" not in root_html


def test_frame_sync_does_not_clear_heat_after_manual_entry():
    state = powered_on_state()
    state.apply_command("ogrzewanie")
    state.apply_command("plecy_i_talia")

    state.note_frame(frame(b4=0x0C, heat=0x0F))

    assert state.heat_on is True
    assert "Ogrzewanie" in set(state.snapshot()["layers"]["visible"])


def test_intensity_only_changes_when_intensity_ui_visible():
    # In manual mode entered via szyja: only neck_on=True. Intensity UI is
    # NOT shown (it requires shoulders/forearms/legs). sila_nacisku_plus is
    # blocked at the bridge level.
    state = powered_on_state()
    state.apply_command("szyja")
    assert state._show_intensity_ui_locked() is False
    starting_intensity = state.intensity_level
    outcome = state.apply_command("sila_nacisku_plus")
    assert outcome.should_send is False
    assert outcome.reason == "intensity controls hidden"
    assert state.intensity_level == starting_intensity

    # Toggle ramiona on → intensity UI now visible → press allowed.
    state.apply_command("ramiona")
    assert state._show_intensity_ui_locked() is True
    outcome2 = state.apply_command("sila_nacisku_plus")
    assert outcome2.should_send is True
    assert state.intensity_level == 3

    # Cap at 3.
    state.apply_command("sila_nacisku_plus")
    assert state.intensity_level == 3


def test_pauza_blocks_other_commands_in_manual():
    state = powered_on_state()
    state.apply_command("plecy_i_talia")  # → manual-back
    state.apply_command("pauza")
    assert state.paused is True

    outcome = state.apply_command("ramiona")
    assert outcome.should_send is False
    assert outcome.reason == "paused"

    # Power and pauza itself remain allowed.
    out2 = state.apply_command("pauza")
    assert out2.should_send is True
    assert state.paused is False


def test_pauza_dead_in_auto():
    state = powered_on_state()
    assert state.mode == "auto"
    assert state.paused is False
    outcome = state.apply_command("pauza")
    assert outcome.should_send is False
    assert outcome.reason == "disabled in auto mode"
    assert state.paused is False


def test_predkosc_dead_in_auto():
    state = powered_on_state()
    starting = state.speed_level
    out_plus = state.apply_command("predkosc_plus")
    out_minus = state.apply_command("predkosc_minus")
    assert out_plus.should_send is False
    assert out_minus.should_send is False
    assert out_plus.reason == "disabled in auto mode"
    assert state.speed_level == starting


def test_do_przodu_do_tylu_2_dead_in_auto():
    state = powered_on_state()
    starting_cycle = state.back_forward_cycle_2
    outcome = state.apply_command("do_przodu_do_tylu_2")
    assert outcome.should_send is False
    assert outcome.reason == "disabled in auto mode"
    assert state.back_forward_cycle_2 == starting_cycle


def test_overlay_does_not_clear_state():
    # Press grawitacja_zero in default-A, then receive an overlay frame
    # (mode bytes all 0x00 but payload non-zero). State must be preserved
    # — power_on stays True, zones are not cleared.
    state = powered_on_state()
    pre_zones = (
        state.shoulders_on,
        state.forearms_on,
        state.legs_on,
        state.foot_massage_on,
        state.neck_on,
        state.back_waist_on,
    )
    state.apply_command("grawitacja_zero")
    # Simulate the overlay frame: mode bytes zeroed, rest of payload still set.
    overlay = frame()
    overlay[3] = 0x00
    overlay[4] = 0x00
    overlay[11] = 0x00
    overlay[12] = 0x00
    state.note_frame(overlay)

    post_zones = (
        state.shoulders_on,
        state.forearms_on,
        state.legs_on,
        state.foot_massage_on,
        state.neck_on,
        state.back_waist_on,
    )
    assert state.power_on is True
    assert post_zones == pre_zones
    assert state.mode == "auto"


def test_power_off_detected_from_zero_payload():
    state = powered_on_state()
    assert state.power_on is True
    # Power press mutes power_on briefly; clear it so the chair-driven
    # all-zero payload signal is not shielded.
    state._muted_fields.clear()
    state.note_frame(frame(payload_zero=True))
    assert state.power_on is False
    assert state.mode == "off"
    assert state.power_off_text_until > time.monotonic()


def test_timer_counts_down_one_per_second():
    state = powered_on_state()
    assert state.remaining_seconds == 900
    # Fast-forward the model's last_tick by 1.5 seconds, then poke _tick_locked
    # via snapshot() which calls it under lock.
    state.last_tick = time.monotonic() - 1.5
    state.snapshot()
    assert state.remaining_seconds == 899


def test_timer_text_uses_calibrated_start_offset():
    # With the calibrated offset (config.TIMER_DISPLAY_OFFSET_SECONDS = 45
    # by default), visible "15" stays through remaining_seconds=855 and
    # drops to "14" at 854. Adjust the constant in bridge/config.py to
    # retune if live panel timing moves again.
    from bridge import config as bridge_config

    state = powered_on_state()
    offset = bridge_config.TIMER_DISPLAY_OFFSET_SECONDS
    assert offset == 45  # default — change with intent

    state.remaining_seconds = 900
    assert state.snapshot()["layers"]["text"]["Czas-NUMBER"] == "15"
    state.remaining_seconds = 900 - offset
    assert state.snapshot()["layers"]["text"]["Czas-NUMBER"] == "15"
    state.remaining_seconds = 900 - offset - 1
    assert state.snapshot()["layers"]["text"]["Czas-NUMBER"] == "14"
    state.remaining_seconds = 60
    assert state.snapshot()["layers"]["text"]["Czas-NUMBER"] == "1"
    state.remaining_seconds = 1
    assert state.snapshot()["layers"]["text"]["Czas-NUMBER"] == "1"
    state.remaining_seconds = 0
    assert state.snapshot()["layers"]["text"]["Czas-NUMBER"] == "0"


def test_timer_offset_is_named_and_configurable(monkeypatch):
    # The offset must be a single named knob that can be retuned without
    # touching unrelated logic. Verify a runtime override changes the
    # transition without touching state internals.
    from bridge import config as bridge_config

    state = powered_on_state()
    monkeypatch.setattr(bridge_config, "TIMER_DISPLAY_OFFSET_SECONDS", 35)
    state.remaining_seconds = 900 - 35
    assert state.snapshot()["layers"]["text"]["Czas-NUMBER"] == "15"
    state.remaining_seconds = 900 - 35 - 1
    assert state.snapshot()["layers"]["text"]["Czas-NUMBER"] == "14"


def test_auto_speed_program_uses_separate_offset_from_timer_display(monkeypatch):
    # Live correction 2026-04-30: sharing the visible timer display
    # offset made Predkosc-LVL animations too delayed. The speed program
    # has its own offset, defaulting to 0 so the level table is read on
    # the previously better-aligned raw program timeline.
    state = powered_on_state()
    now = time.monotonic()
    display_offset = bridge_config.TIMER_DISPLAY_OFFSET_SECONDS

    assert state.remaining_seconds == 900  # canonical, no offset
    assert display_offset == 45
    assert bridge_config.AUTO_SPEED_PROGRAM_OFFSET_SECONDS == 0
    assert speed_at_elapsed("A", 0) == 2
    assert speed_at_elapsed("A", 8) == 3

    state._auto_speed_program_started_at = now - 8
    assert state._auto_speed_program_level_locked(now) == 3

    monkeypatch.setattr(bridge_config, "AUTO_SPEED_PROGRAM_OFFSET_SECONDS", 12)
    state._auto_speed_program_started_at = now - 8
    assert state._auto_speed_program_level_locked(now) == 2

    state._auto_speed_program_started_at = now - 12 - 8
    assert state._auto_speed_program_level_locked(now) == 3


def test_timer_and_level_program_share_one_monotonic_anchor():
    # On a fresh power-on, both the countdown anchor (last_tick) and the
    # auto-speed program anchor must be set at the same monotonic instant
    # so they tick together. They legitimately diverge later via separate
    # reset paths (czas / profile cycle), but on power-on they MUST agree.
    state = ChairState()
    state.apply_command("power")
    delta = abs(state.last_tick - state._auto_speed_program_started_at)
    assert delta < 0.05, (state.last_tick, state._auto_speed_program_started_at)


def test_timer_diagnostics_snapshot_fields_exist():
    state = powered_on_state()
    state.note_frame(frame())
    diagnostics = state.snapshot()["diagnostics"]

    assert {
        "monotonic",
        "power_on_elapsed_seconds",
        "web_remaining_seconds",
        "web_time_text",
        "timer_minutes",
        "mode",
        "profile",
        "auto_pattern_elapsed_seconds",
        "visible_levels",
        "raw_timer_candidates",
        "verification",
    } <= diagnostics.keys()
    assert diagnostics["raw_timer_candidates"]["bytes_3_to_6"] == [4, 2, 12, 15]


def test_timer_diagnostics_do_not_mutate_state():
    state = powered_on_state()
    before = (
        state.remaining_seconds,
        state.timer_minutes,
        state.intensity_level,
        state.speed_level,
        state.foot_speed_level,
        state._auto_speed_program_started_at,
    )

    _diagnostics = state.snapshot()["diagnostics"]

    after = (
        state.remaining_seconds,
        state.timer_minutes,
        state.intensity_level,
        state.speed_level,
        state.foot_speed_level,
        state._auto_speed_program_started_at,
    )
    assert after == before


def test_stats_level_timing_not_changed_by_timer_diagnostics():
    state = powered_on_state()
    now = time.monotonic()
    state._auto_speed_program_started_at = now - 8

    assert state._auto_speed_program_level_locked(now) == 3
    diagnostics = state._timer_diagnostics_locked(
        now, state._effective_speed_level_locked()
    )

    assert diagnostics["auto_pattern_elapsed_seconds"] == 8
    assert state._auto_speed_program_level_locked(now) == 3


def test_manual_speed_level_adopts_from_shared_frame_register():
    state = powered_on_state()
    state.apply_command("plecy_i_talia")
    assert state.mode == "manual"
    assert state.speed_level == 2
    state._muted_fields.clear()

    state.note_frame(frame(b4=0x0C, b5=0x00, b6=0x00, shared_b11=0x0F))
    assert state.snapshot()["levels"]["speed"] == 3

    state.note_frame(frame(b4=0x0C, b5=0x00, b6=0x00, shared_b11=0x0C))
    assert state.snapshot()["levels"]["speed"] == 2


def test_czas_cycles_15_20_25_30_15_resets_timer():
    state = powered_on_state()
    # Burn a few seconds first so we can prove czas resets remaining_seconds.
    state.remaining_seconds = 600
    assert state.timer_minutes == 15

    state.apply_command("czas")
    assert state.timer_minutes == 20
    assert state.remaining_seconds == 20 * 60

    state.apply_command("czas")
    assert state.timer_minutes == 25
    assert state.remaining_seconds == 25 * 60

    state.apply_command("czas")
    assert state.timer_minutes == 30
    assert state.remaining_seconds == 30 * 60

    state.apply_command("czas")
    assert state.timer_minutes == 15
    assert state.remaining_seconds == 15 * 60


def test_full_body_toggles_three_zones():
    state = powered_on_state()
    # default-A has all three zones on.
    assert state.shoulders_on and state.forearms_on and state.legs_on
    assert state.full_body_on is True

    state.apply_command("masaz_calego_ciala")
    assert state.full_body_on is False
    assert state.shoulders_on is False
    assert state.forearms_on is False
    assert state.legs_on is False

    state.apply_command("masaz_calego_ciala")
    assert state.full_body_on is True
    assert state.shoulders_on is True
    assert state.forearms_on is True
    assert state.legs_on is True


def test_back_forward_cycle_1_increments_b1_b2():
    state = _known_manual_back_state()
    assert state.back_waist_on is True
    assert state.back_forward_cycle_1 == 1

    state.apply_command("do_przodu_do_tylu_1")
    assert state.back_forward_cycle_1 == 2
    assert state.prompt_text == "b2"

    state.apply_command("do_przodu_do_tylu_1")
    assert state.back_forward_cycle_1 == 1
    assert state.prompt_text == "b1"


def test_manual_to_auto_via_tryb_automatyczny_uses_unified_A():
    state = powered_on_state()
    state.remaining_seconds = 733
    state.apply_command("szyja")  # → manual-neck
    assert state.mode == "manual"

    state.apply_command("tryb_automatyczny")
    assert state.mode == "auto"
    assert state.auto_profile == "A"
    assert state.prompt_text == "F1"
    # Profile A is always the same full default set.
    assert state.shoulders_on and state.forearms_on and state.legs_on
    assert state.neck_on
    assert state.back_waist_on
    assert state.foot_massage_on
    assert not state.buttocks_on
    assert state.remaining_seconds == 733


def test_overlay_visibility_only_body_and_time():
    state = powered_on_state()
    state.apply_command("grawitacja_zero")
    visible = set(state.snapshot()["layers"]["visible"])
    # During the overlay window: black Background, Body, Czas-TEXT,
    # Czas-NUMBER survive, plus SHAPE_CHECK-TEXT pulse.
    assert "Body" in visible
    assert "Czas-TEXT" in visible
    assert "Czas-NUMBER" in visible
    assert "Background" in visible
    assert "Tryb_automatyczny" not in visible
    assert "Tryb_automatyczny-A" not in visible
    assert "Ramiona" not in visible


def test_power_off_window_is_pure_black():
    state = powered_on_state()
    state._muted_fields.clear()
    state.note_frame(frame(payload_zero=True))
    visible = set(state.snapshot()["layers"]["visible"])
    # Live panel correction: power-off is just the black background.
    assert visible == {"Background"}


def test_power_off_settles_to_pure_black_not_white():
    state = powered_on_state()
    state._muted_fields.clear()
    state.note_frame(frame(payload_zero=True))
    state.power_off_text_until = time.monotonic() - 0.01
    state.check_until = time.monotonic() - 0.01
    visible = set(state.snapshot()["layers"]["visible"])
    assert visible == {"Background"}


def test_back_forward_cycle_2_increments_A2_A1_from_default_A():
    state = _known_manual_neck_state()
    assert state.neck_on is True
    assert state.back_forward_cycle_2 == 2

    state.apply_command("do_przodu_do_tylu_2")
    assert state.back_forward_cycle_2 == 1
    assert state.prompt_text == "A1"

    state.apply_command("do_przodu_do_tylu_2")
    assert state.back_forward_cycle_2 == 2
    assert state.prompt_text == "A2"


def test_timer_does_not_reset_on_zone_toggle():
    state = powered_on_state()
    state.remaining_seconds = 733
    state.apply_command("ramiona")
    assert state.remaining_seconds == 733


def test_timer_does_not_reset_on_intensity_change():
    state = powered_on_state()
    state.remaining_seconds = 733
    state.apply_command("sila_nacisku_plus")
    assert state.remaining_seconds == 733


def test_timer_does_not_reset_on_overlay_command():
    state = powered_on_state()
    state.remaining_seconds = 733
    state.apply_command("grawitacja_zero")
    assert state.remaining_seconds == 733


def test_timer_does_not_reset_on_szyja_or_plecy_i_talia():
    state = powered_on_state()
    state.remaining_seconds = 733
    state.apply_command("szyja")
    assert state.remaining_seconds == 733
    state.apply_command("plecy_i_talia")
    assert state.remaining_seconds == 733


def test_timer_resets_on_czas_press():
    state = powered_on_state()
    state.remaining_seconds = 733
    state.apply_command("czas")
    assert state.timer_minutes == 20
    assert state.remaining_seconds == 20 * 60


def test_timer_does_not_reset_on_profile_cycle():
    state = powered_on_state()
    state.remaining_seconds = 733
    state.apply_command("tryb_automatyczny")
    assert state.auto_profile == "B"
    assert state.timer_minutes == 15
    assert state.remaining_seconds == 733


def test_timer_resets_on_power_on_only_once_within_500ms():
    state = ChairState()
    state.apply_command("power")
    first_reset = state._last_timer_reset_at
    state._set_default_auto_locked()
    assert state._last_timer_reset_at == first_reset
    assert any("timer reset skipped" in line for line in state.backend_log)


def test_default_after_power_on_matches_profile_A():
    default_state = powered_on_state()
    profile_state = ChairState()
    profile_state.power_on = True
    profile_state._apply_auto_profile_locked("A")

    for field in (
        "mode",
        "auto_profile",
        "intensity_level",
        "speed_level",
        "foot_speed_level",
        "heat_on",
        "shoulders_on",
        "forearms_on",
        "legs_on",
        "buttocks_on",
        "foot_massage_on",
        "neck_on",
        "back_waist_on",
        "full_body_on",
        "paused",
    ):
        assert getattr(profile_state, field) == getattr(default_state, field)


def test_profile_cycle_D_to_A_matches_default():
    state = powered_on_state()
    for _ in range(4):
        state.remaining_seconds = 733
        state.apply_command("tryb_automatyczny")
    assert state.auto_profile == "A"
    assert state.shoulders_on and state.forearms_on and state.legs_on
    assert state.foot_massage_on
    assert state.neck_on
    assert state.back_waist_on
    assert state.full_body_on
    assert state.remaining_seconds == 733


def test_profile_A_zones_are_full_set():
    state = powered_on_state()
    assert state.shoulders_on is True
    assert state.forearms_on is True
    assert state.legs_on is True
    assert state.foot_massage_on is True
    assert state.neck_on is True
    assert state.back_waist_on is True
    assert state.full_body_on is True
    assert state.buttocks_on is False


def test_profile_C_shows_correct_zones():
    state = powered_on_state()
    state.apply_command("tryb_automatyczny")  # B
    state.apply_command("tryb_automatyczny")  # C
    visible = set(state.snapshot()["layers"]["visible"])
    for layer in ("Szyja", "Plecy_i_talia", "Masaz_Posladkow", "Ramiona"):
        assert layer in visible
    for layer in ("Nogi", "Masaz_Stop", "Przedramiona"):
        assert layer not in visible


def test_profile_C_shows_speed_ui():
    state = powered_on_state()
    state.apply_command("tryb_automatyczny")  # B
    state.apply_command("tryb_automatyczny")  # C
    visible = set(state.snapshot()["layers"]["visible"])
    assert "PredkoscTEXT" in visible
    assert "Predkosc-LVL2" in visible


def test_profile_C_hides_foot_speed_ui():
    state = powered_on_state()
    state.apply_command("tryb_automatyczny")  # B
    state.apply_command("tryb_automatyczny")  # C
    visible = set(state.snapshot()["layers"]["visible"])
    assert "Predkosc_masazu_stop" not in visible
    assert "Predkosc_masazu_stop-LVL1" not in visible
    assert "Predkosc_masazu_stop-LVL2" not in visible
    assert "Predkosc_masazu_stop-LVL3" not in visible


def test_szyja_then_plecy_i_talia_keeps_both():
    state = powered_on_state()
    state.apply_command("szyja")
    state.apply_command("plecy_i_talia")
    assert state.neck_on is True
    assert state.back_waist_on is True


def test_plecy_i_talia_then_szyja_keeps_both():
    state = powered_on_state()
    state.apply_command("plecy_i_talia")
    state.apply_command("szyja")
    assert state.back_waist_on is True
    assert state.neck_on is True


def test_szyja_toggled_off_keeps_back_waist():
    state = powered_on_state()
    state.apply_command("szyja")
    state.apply_command("plecy_i_talia")
    state.apply_command("szyja")
    assert state.neck_on is False
    assert state.back_waist_on is True


def test_plecy_i_talia_toggled_off_keeps_neck():
    state = powered_on_state()
    state.apply_command("plecy_i_talia")
    state.apply_command("szyja")
    state.apply_command("plecy_i_talia")
    assert state.back_waist_on is False
    assert state.neck_on is True


def test_szyja_first_press_from_default_A_suppresses_unknown_prompt():
    state = powered_on_state()
    state.apply_command("szyja")
    assert state.prompt_text == ""
    assert state.snapshot()["direction"]["neck_known"] is False


def test_plecy_i_talia_first_press_suppresses_unknown_prompt():
    state = powered_on_state()
    state.apply_command("plecy_i_talia")
    assert state.prompt_text == ""
    assert state.snapshot()["direction"]["back_known"] is False


def test_plecy_i_talia_first_press_from_A_ignores_stale_b2_without_prompt():
    state = powered_on_state()
    state.back_forward_cycle_1 = 2
    state.back_forward_cycle_2 = 2
    state.apply_command("plecy_i_talia")
    assert state.back_direction_known is False
    assert state.neck_direction_known is False
    assert state.prompt_text == ""


def test_plecy_i_talia_first_press_from_unknown_auto_ignores_stale_b2_without_prompt():
    state = powered_on_state()
    state.auto_profile = None
    state.back_forward_cycle_1 = 2
    state.back_forward_cycle_2 = 2
    state.apply_command("plecy_i_talia")
    assert state.back_direction_known is False
    assert state.neck_direction_known is False
    assert state.prompt_text == ""


def test_szyja_then_plecy_i_talia_suppresses_unknown_prompts():
    state = powered_on_state()
    state.apply_command("szyja")
    assert state.prompt_text == ""
    state.apply_command("plecy_i_talia")
    assert state.prompt_text == ""


def test_plecy_i_talia_then_szyja_suppresses_unknown_prompts():
    state = powered_on_state()
    state.apply_command("plecy_i_talia")
    assert state.prompt_text == ""
    state.apply_command("szyja")
    assert state.prompt_text == ""


def test_do_przodu_do_tylu_2_advances_only_neck_counter():
    state = _known_manual_neck_state()
    state.apply_command("do_przodu_do_tylu_2")
    assert state.back_forward_cycle_2 == 1
    assert state.prompt_text == "A1"


# ---------------------------------------------------------------------------
# Ogrzewanie state bug fix — heat survives AUTO → MANUAL
# ---------------------------------------------------------------------------


def test_power_on_heat_is_off():
    """Power on: Ogrzewanie must be OFF and the SVG layer hidden."""
    state = powered_on_state()
    assert state.heat_on is False
    visible = set(state.snapshot()["layers"]["visible"])
    assert "Ogrzewanie" not in visible


def test_ogrzewanie_turns_on_in_auto():
    """User presses Ogrzewanie in auto: heat_on becomes True, layer visible."""
    state = powered_on_state()
    state.apply_command("ogrzewanie")
    assert state.heat_on is True
    visible = set(state.snapshot()["layers"]["visible"])
    assert "Ogrzewanie" in visible


def test_szyja_from_auto_preserves_heat_on():
    """AUTO → MANUAL via szyja: heat_on must survive."""
    state = powered_on_state()
    state.apply_command("ogrzewanie")
    assert state.heat_on is True
    state.apply_command("szyja")
    assert state.mode == "manual"
    assert state.heat_on is True


def test_plecy_i_talia_from_auto_preserves_heat_on():
    """AUTO → MANUAL via plecy_i_talia: heat_on must survive."""
    state = powered_on_state()
    state.apply_command("ogrzewanie")
    assert state.heat_on is True
    state.apply_command("plecy_i_talia")
    assert state.mode == "manual"
    assert state.heat_on is True


def test_heat_layer_visible_after_szyja_manual_entry():
    """AUTO → MANUAL via szyja with heat ON: Ogrzewanie SVG layer stays visible."""
    state = powered_on_state()
    state.apply_command("ogrzewanie")
    state.apply_command("szyja")
    visible = set(state.snapshot()["layers"]["visible"])
    assert "Ogrzewanie" in visible


def test_heat_layer_visible_after_plecy_i_talia_manual_entry():
    """AUTO → MANUAL via plecy_i_talia with heat ON: Ogrzewanie SVG layer stays visible."""
    state = powered_on_state()
    state.apply_command("ogrzewanie")
    state.apply_command("plecy_i_talia")
    visible = set(state.snapshot()["layers"]["visible"])
    assert "Ogrzewanie" in visible


def test_manual_to_auto_clears_heat():
    """MANUAL → AUTO (tryb_automatyczny): heat_on must be cleared."""
    state = powered_on_state()
    state.apply_command("ogrzewanie")
    assert state.heat_on is True
    state.apply_command("szyja")
    assert state.heat_on is True
    state.apply_command("tryb_automatyczny")
    assert state.mode == "auto"
    assert state.heat_on is False


def test_heat_layer_hidden_after_manual_to_auto():
    """MANUAL → AUTO with heat ON: Ogrzewanie SVG layer becomes hidden."""
    state = powered_on_state()
    state.apply_command("ogrzewanie")
    state.apply_command("szyja")
    state.apply_command("tryb_automatyczny")
    visible = set(state.snapshot()["layers"]["visible"])
    assert "Ogrzewanie" not in visible


def test_do_przodu_do_tylu_1_advances_only_back_counter():
    state = _known_manual_back_state()
    state.apply_command("do_przodu_do_tylu_1")
    assert state.back_forward_cycle_1 == 2
    assert state.prompt_text == "b2"


def test_zone_reentry_keeps_neck_prompt_cycle():
    state = _known_manual_neck_state()
    state.apply_command("do_przodu_do_tylu_2")
    assert state.prompt_text == "A1"
    state.apply_command("szyja")
    assert state.neck_on is False
    state.apply_command("szyja")
    assert state.neck_on is True
    assert state.back_forward_cycle_2 == 1
    assert state.prompt_text == "A1"


def test_zone_reentry_keeps_back_prompt_cycle():
    state = _known_manual_back_state()
    state.apply_command("do_przodu_do_tylu_1")
    assert state.prompt_text == "b2"
    state.apply_command("plecy_i_talia")
    assert state.back_waist_on is False
    state.apply_command("plecy_i_talia")
    assert state.back_waist_on is True
    assert state.back_forward_cycle_1 == 2
    assert state.prompt_text == "b2"


def test_auto_mode_does_not_fake_known_direction_state():
    state = powered_on_state()

    assert state.snapshot()["direction"] == {
        "back_known": False,
        "neck_known": False,
        "back_cycle": None,
        "neck_cycle": None,
    }

    state.apply_command("tryb_automatyczny")

    assert state.back_direction_known is False
    assert state.neck_direction_known is False
    assert state.back_forward_cycle_1 == 0
    assert state.back_forward_cycle_2 == 0


def test_manual_entry_uses_hardware_direction_if_available():
    state = powered_on_state()
    # Simulate a future proven decoder setting known state before manual entry.
    state.neck_direction_known = True
    state.back_forward_cycle_2 = 1

    state.apply_command("szyja")

    assert state.prompt_text == "A1"


def test_manual_entry_does_not_show_confident_wrong_A_prompt():
    state = powered_on_state()

    state.apply_command("szyja")

    assert state.prompt_text == ""
    assert state.back_forward_cycle_2 == 0
    assert state.neck_direction_known is False


def test_manual_entry_does_not_show_confident_wrong_b_prompt():
    state = powered_on_state()

    state.apply_command("plecy_i_talia")

    assert state.prompt_text == ""
    assert state.back_forward_cycle_1 == 0
    assert state.back_direction_known is False


def test_direction_counters_not_advanced_by_unrelated_auto_logic():
    state = powered_on_state()
    for _ in range(3):
        state.apply_command("tryb_automatyczny")

    assert state.mode == "auto"
    assert state.back_forward_cycle_1 == 0
    assert state.back_forward_cycle_2 == 0


def test_overlay_window_shows_only_body_czas_shape_check():
    state = powered_on_state()
    state.apply_command("grawitacja_zero")
    visible = set(state.snapshot()["layers"]["visible"])
    assert visible == {
        "Background",
        "Body",
        "Czas-TEXT",
        "Czas-NUMBER",
        "SHAPE_CHECK-TEXT",
    }


def test_overlay_window_hides_all_zones():
    state = powered_on_state()
    state.apply_command("grawitacja_zero")
    visible = set(state.snapshot()["layers"]["visible"])
    for layer in (
        "Szyja",
        "Plecy_i_talia",
        "Masaz_Posladkow",
        "Masaz_Stop",
        "Nogi",
        "Przedramiona",
        "Ramiona",
    ):
        assert layer not in visible


def test_overlay_window_hides_all_mode_badges():
    state = powered_on_state()
    state.apply_command("grawitacja_zero")
    visible = set(state.snapshot()["layers"]["visible"])
    assert "Tryb_manualny" not in visible
    assert "Tryb_automatyczny" not in visible
    assert "Tryb_automatyczny-A" not in visible


def test_overlay_window_hides_intensity_speed_ui():
    state = powered_on_state()
    state.apply_command("grawitacja_zero")
    visible = set(state.snapshot()["layers"]["visible"])
    for layer in (
        "Sila_nacisku-TEXT",
        "Sila_nacisku-LVL1",
        "Sila_nacisku-LVL2",
        "Sila_nacisku-LVL3",
        "PredkoscTEXT",
        "Predkosc-LVL1",
        "Predkosc-LVL2",
        "Predkosc-LVL3",
        "Predkosc_masazu_stop",
    ):
        assert layer not in visible


def test_speed_at_elapsed_profile_A_matches_user_log():
    assert speed_at_elapsed("A", 0) == 2
    assert speed_at_elapsed("A", 8) == 3
    assert speed_at_elapsed("A", 224) == 1
    assert speed_at_elapsed("A", 227) == 3
    assert speed_at_elapsed("A", 827) is None


def test_auto_profile_A_pattern_matches_notes():
    assert speed_at_elapsed("A", 0) == 2
    assert speed_at_elapsed("A", 810) == 3
    assert speed_at_elapsed("A", 827) is None


def test_speed_at_elapsed_profile_B_matches_user_log():
    assert speed_at_elapsed("B", 0) == 2
    assert speed_at_elapsed("B", 218) == 1
    assert speed_at_elapsed("B", 222) == 3
    assert speed_at_elapsed("B", 833) is None


def test_auto_profile_B_pattern_matches_notes():
    assert speed_at_elapsed("B", 832) == 3
    assert speed_at_elapsed("B", 833) is None


def test_auto_profile_C_pattern_matches_notes():
    assert speed_at_elapsed("C", 832) == 3
    assert speed_at_elapsed("C", 837) == 3
    assert speed_at_elapsed("C", 838) is None


def test_auto_profile_D_pattern_matches_notes():
    assert speed_at_elapsed("D", 832) == 3
    assert speed_at_elapsed("D", 852) == 2
    assert speed_at_elapsed("D", 855) is None


def test_profile_BCD_share_timeline():
    for profile in ("B", "C", "D"):
        assert speed_at_elapsed(profile, 361) == 1
        assert speed_at_elapsed(profile, 364) == 3


def test_existing_good_timing_not_shifted_without_evidence():
    assert bridge_config.AUTO_SPEED_PROGRAM_OFFSET_SECONDS == 0
    for profile in ("A", "B", "C", "D"):
        assert speed_at_elapsed(profile, 8) == 3
        assert speed_at_elapsed(profile, 76) == 1


def test_speed_program_ends_powers_off():
    state = powered_on_state()
    state._auto_speed_program_started_at = (
        time.monotonic() - bridge_config.AUTO_SPEED_PROGRAM_OFFSET_SECONDS - 828
    )
    snapshot = state.snapshot()
    assert snapshot["power_on"] is False
    assert snapshot["mode"] == "off"


def test_auto_intensity_user_adjustable():
    state = powered_on_state()
    assert state.mode == "auto"
    state.apply_command("sila_nacisku_plus")
    assert state.intensity_level == 3
    state.apply_command("sila_nacisku_minus")
    assert state.intensity_level == 2


def test_auto_foot_speed_user_adjustable_when_shown():
    state = powered_on_state()
    assert state.foot_massage_on is True
    state.apply_command("predkosc_masazu_stop")
    assert state.foot_speed_level == 3
    state.apply_command("tryb_automatyczny")  # B
    state.apply_command("tryb_automatyczny")  # C: no foot massage
    assert state.foot_massage_on is False
    before = state.foot_speed_level
    outcome = state.apply_command("predkosc_masazu_stop")
    assert outcome.should_send is False
    assert state.foot_speed_level == before


# ---------------------------------------------------------------------------
# Boot-settle gate: fast manual presses immediately after a local power-on
# must not desync the bridge's optimistic A/b prompt counter from the
# hardware LCD. The state machine refuses manual zone / direction / scalar
# presses until the chair has either streamed enough running frames or the
# fail-open timeout has elapsed.
# ---------------------------------------------------------------------------


def _booting_chair_state() -> ChairState:
    """A fresh power-on without manually clearing the boot-settle gate."""
    state = ChairState()
    state.apply_command("power")
    return state


def test_power_on_arms_boot_settle_window():
    state = _booting_chair_state()
    assert state.boot_settle_started_at is not None
    assert state._running_frames_since_boot == 0
    assert state._boot_settling_locked() is True


def test_power_off_clears_boot_settle_window():
    state = _booting_chair_state()
    state._muted_fields.clear()
    state.apply_command("power")  # toggles back off
    assert state.boot_settle_started_at is None
    assert state._running_frames_since_boot == 0
    assert state._boot_settling_locked() is False


def test_manual_command_during_boot_settle_does_not_advance_prompt():
    state = _booting_chair_state()
    seeded_cycle_2 = state.back_forward_cycle_2
    seeded_cycle_1 = state.back_forward_cycle_1
    assert state._boot_settling_locked() is True

    outcome = state.apply_command("szyja")
    assert outcome.should_send is False
    assert outcome.reason == "chair booting"
    # Optimistic counter advance must NOT have happened.
    assert state.back_forward_cycle_2 == seeded_cycle_2
    assert state.back_forward_cycle_1 == seeded_cycle_1
    # The model is still in auto-A; szyja did not flip mode to manual.
    assert state.mode == "auto"
    assert state.auto_profile == "A"
    # Prompt text must not show A2 yet.
    assert state.prompt_text != "A2"


def test_boot_settle_blocks_all_listed_commands():
    state = _booting_chair_state()
    blocked = []
    for command in (
        "szyja",
        "plecy_i_talia",
        "do_przodu_do_tylu_1",
        "do_przodu_do_tylu_2",
        "predkosc_plus",
        "predkosc_minus",
        "sila_nacisku_plus",
        "sila_nacisku_minus",
        "predkosc_masazu_stop",
    ):
        outcome = state.apply_command(command)
        if outcome.should_send is False and outcome.reason == "chair booting":
            blocked.append(command)
    assert len(blocked) == 9


def test_boot_settle_does_not_block_overlay_or_off():
    state = _booting_chair_state()
    # power and overlay commands must remain available even while
    # booting — those are user-safety controls.
    out = state.apply_command("grawitacja_zero")
    assert out.should_send is True
    out = state.apply_command("oparcie_w_gore")
    assert out.should_send is True
    # power must always go through.
    out = state.apply_command("power")
    assert out.should_send is True


def test_boot_settle_releases_after_min_seconds_and_frames():
    state = _booting_chair_state()
    # Stream the required number of running frames.
    for _ in range(bridge_config.BOOT_SETTLE_FRAMES):
        state.note_frame(frame())
    # Frames alone should not release the gate; the minimum wall delay
    # also has to elapse.
    assert state._boot_settling_locked() is True
    state.boot_settle_started_at = (
        time.monotonic() - bridge_config.BOOT_SETTLE_MIN_SECONDS - 0.1
    )
    assert state._boot_settling_locked() is False


def test_boot_settle_min_delay_alone_does_not_release():
    state = _booting_chair_state()
    # Min delay elapsed but no running frames seen yet: gate stays.
    state.boot_settle_started_at = (
        time.monotonic() - bridge_config.BOOT_SETTLE_MIN_SECONDS - 0.1
    )
    assert state._running_frames_since_boot == 0
    assert state._boot_settling_locked() is True


def test_boot_settle_fails_open_after_timeout():
    state = _booting_chair_state()
    # Chair never streams running frames. After the hard timeout, the
    # gate must release so the UI is not pinned in "booting" forever.
    state.boot_settle_started_at = (
        time.monotonic() - bridge_config.BOOT_SETTLE_TIMEOUT_SECONDS - 0.1
    )
    assert state._running_frames_since_boot == 0
    assert state._boot_settling_locked() is False


def test_szyja_after_boot_settle_suppresses_unknown_A_prompt():
    # Press szyja immediately after the chair has stably booted. The
    # boot gate still prevents the too-early race, but auto mode does not
    # prove a readable A1/A2 state, so the web UI avoids a confident prompt.
    state = _booting_chair_state()
    for _ in range(bridge_config.BOOT_SETTLE_FRAMES):
        state.note_frame(frame())
    state.boot_settle_started_at = (
        time.monotonic() - bridge_config.BOOT_SETTLE_MIN_SECONDS - 0.1
    )
    assert state._boot_settling_locked() is False
    state.apply_command("szyja")
    assert state.prompt_text == ""
    assert state.snapshot()["direction"]["neck_known"] is False


def test_szyja_then_plecy_after_boot_settle_suppresses_unknown_prompts():
    # Stable-boot szyja and then plecy_i_talia must not invent A/b prompts
    # when auto may have changed the hidden direction state.
    state = _booting_chair_state()
    for _ in range(bridge_config.BOOT_SETTLE_FRAMES):
        state.note_frame(frame())
    state.boot_settle_started_at = (
        time.monotonic() - bridge_config.BOOT_SETTLE_MIN_SECONDS - 0.1
    )
    state.apply_command("szyja")
    assert state.prompt_text == ""
    state.apply_command("plecy_i_talia")
    assert state.prompt_text == ""
    assert state.snapshot()["direction"]["back_known"] is False


def test_fast_szyja_after_boot_cannot_desync_web_from_hardware():
    # User clicks szyja the instant the bridge accepts commands. The
    # gate must reject the press, so the bridge's prompt counter cannot
    # tick while the chair LCD is still settling.
    state = _booting_chair_state()
    rejected = state.apply_command("szyja")
    assert rejected.should_send is False
    assert rejected.reason == "chair booting"
    # Now stable boot: the press is accepted and produces the correct
    # post-stable prompt.
    for _ in range(bridge_config.BOOT_SETTLE_FRAMES):
        state.note_frame(frame())
    state.boot_settle_started_at = (
        time.monotonic() - bridge_config.BOOT_SETTLE_MIN_SECONDS - 0.1
    )
    accepted = state.apply_command("szyja")
    assert accepted.should_send is True
    assert state.prompt_text == ""
    assert state.snapshot()["direction"]["neck_known"] is False


def test_boot_settle_does_not_reset_timer_twice():
    # The boot-settle window must not call `_reset_timer_to_locked`
    # itself, and frame-sync increment of the running-frame counter
    # also must not trigger a second reset.
    state = _booting_chair_state()
    initial_reset = state._last_timer_reset_at
    initial_remaining = state.remaining_seconds
    for _ in range(bridge_config.BOOT_SETTLE_FRAMES + 2):
        state.note_frame(frame())
    # Force-elapsed monotonic so the gate releases naturally.
    state.boot_settle_started_at = (
        time.monotonic() - bridge_config.BOOT_SETTLE_MIN_SECONDS - 0.1
    )
    state._boot_settling_locked()
    assert state._last_timer_reset_at == initial_reset
    assert state.remaining_seconds == initial_remaining


def test_duplicate_power_on_does_not_double_reset_timer():
    # Frame sync after a local power press must not reset the timer.
    state = ChairState()
    state.apply_command("power")
    initial_reset = state._last_timer_reset_at
    initial_remaining = state.remaining_seconds
    state.note_frame(frame())  # chair confirms running
    assert state._last_timer_reset_at == initial_reset
    assert state.remaining_seconds == initial_remaining


def test_snapshot_exposes_boot_settling_and_controls_ready():
    # The UI and any external dashboard need a transparent flag, even
    # though existing `bridge_busy` already grays out the same buttons.
    state = _booting_chair_state()
    snapshot = state.snapshot()
    assert snapshot["boot_settling"] is True
    assert snapshot["controls_ready"] is False
    state.boot_settle_started_at = (
        time.monotonic() - bridge_config.BOOT_SETTLE_TIMEOUT_SECONDS - 0.1
    )
    snapshot = state.snapshot()
    assert snapshot["boot_settling"] is False
    assert snapshot["controls_ready"] is True


def test_snapshot_when_off_reports_controls_not_ready():
    state = ChairState()
    snapshot = state.snapshot()
    assert snapshot["boot_settling"] is False
    assert snapshot["controls_ready"] is False


def _make_frame_with_zone_byte(byte21: int, byte23: int = 0x00) -> list[int]:
    frame = [0x00] * 33
    frame[3] = 0x04
    frame[4] = 0x02
    frame[5] = 0x0C
    frame[6] = 0x0F
    frame[21] = byte21
    frame[23] = byte23
    return frame


def test_press_diagnostics_empty_history_returns_empty_list():
    state = ChairState()
    assert state.press_diagnostics() == []


def test_press_diagnostics_records_command_and_byte_code():
    state = powered_on_state()
    state.apply_command("plecy_i_talia")
    entries = state.press_diagnostics()
    assert any(entry["command"] == "plecy_i_talia" for entry in entries)
    plecy_entry = next(
        entry for entry in entries if entry["command"] == "plecy_i_talia"
    )
    assert plecy_entry["byte_code"] == 0x17
    assert plecy_entry["label"] == "Plecy i talia"


def test_press_diagnostics_captures_before_and_after_frames():
    state = ChairState()
    state.apply_command("power")
    _force_boot_settled(state)
    state.note_frame(_make_frame_with_zone_byte(0x07))  # all 3 zone bits on
    state.apply_command("ramiona")
    state.note_frame(_make_frame_with_zone_byte(0x06))  # shoulders bit cleared
    entries = state.press_diagnostics()
    ramiona_entries = [e for e in entries if e["command"] == "ramiona"]
    assert ramiona_entries, "ramiona press should be recorded"
    entry = ramiona_entries[-1]
    assert entry["frame_before"] is not None
    assert entry["frame_after"] is not None
    assert entry["frame_before"]["zone_byte21"] == 0x07
    assert entry["frame_after"]["zone_byte21"] == 0x06
    assert entry["delta"]["zone_byte21_xor"] == 0x01  # shoulders bit flipped


def test_press_diagnostics_delta_flags_unchanged_frame_for_dead_channel():
    # Scenario the user described: bridge sends 0x17 (plecy_i_talia) but
    # the chair's OEM frame does not change because the OEM controller
    # itself is the dead stage. The diagnostic must surface this so the
    # tech knows where to look.
    state = ChairState()
    state.apply_command("power")
    _force_boot_settled(state)
    stuck_frame = _make_frame_with_zone_byte(0x07, byte23=0x00)
    state.note_frame(stuck_frame)
    state.apply_command("plecy_i_talia")
    state.note_frame(_make_frame_with_zone_byte(0x07, byte23=0x00))  # identical
    entries = state.press_diagnostics()
    plecy = next(e for e in entries if e["command"] == "plecy_i_talia")
    assert plecy["delta"]["zone_byte21_xor"] == 0x00
    assert plecy["delta"]["mode_bytes_changed"] is False
    assert plecy["delta"]["signature_changed"] is False


def test_press_diagnostics_limit_caps_returned_entries():
    state = powered_on_state()
    for _ in range(10):
        state.apply_command("ogrzewanie")
    entries = state.press_diagnostics(limit=3)
    assert len(entries) <= 3
