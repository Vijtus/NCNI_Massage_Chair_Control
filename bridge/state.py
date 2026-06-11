from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from . import config
from .auto_speed_pattern import speed_at_elapsed
from .commands import (
    AUTO_DEAD_COMMANDS,
    AUTO_PROFILE_SEQUENCE,
    BOOT_SETTLE_BLOCKED_COMMANDS,
    BUTTON_ORDER,
    COMMAND_INDEX,
    COMMAND_MUTE_FIELDS,
    FRAME_OBSERVED_COMMANDS,
    HOLD_TO_ADJUST_COMMANDS,
    MODEL_ONLY_COMMANDS,
    MOMENTARY_COMMANDS,
    TIME_OPTIONS_MINUTES,
)
from .framing import FullFrameParser

UNKNOWN = object()

AUTO_TAIL_SIGNATURES: dict[tuple[int, int, int, int], str] = {
    (0x0A, 0x0B, 0x0C, 0x08): "A",
    (0x04, 0x00, 0x0A, 0x00): "A",
    (0x04, 0x0D, 0x06, 0x00): "B",
    (0x04, 0x09, 0x0E, 0x00): "C",
    (0x04, 0x02, 0x0E, 0x00): "D",
}

AUTO_PROFILE_FIELDS = {
    "mode",
    "auto_profile",
    "shoulders_on",
    "forearms_on",
    "legs_on",
    "buttocks_on",
    "foot_massage_on",
    "neck_on",
    "back_waist_on",
    "full_body_on",
}


@dataclass(frozen=True)
class CommandOutcome:
    should_send: bool
    muted_fields: set[str]
    expected_fields: dict[str, Any]
    reason: str | None = None


@dataclass(frozen=True)
class VerifyResult:
    agreed: bool
    checked: int
    disagreements: list[dict[str, Any]]
    unverified: bool = False


@dataclass(frozen=True)
class FrameRecord:
    wall_time: float
    monotonic_time: float
    raw: list[int]
    signature: str
    bytes_3_to_6: list[int]
    zones: dict[str, bool]
    levels: dict[str, int]
    mode: str
    power_on: bool


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def monotonic_deadline(seconds: float) -> float:
    return time.monotonic() + seconds


class ChairState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.command_history: list[dict[str, Any]] = []
        self.backend_log: list[str] = []
        self.failed_commands: list[dict[str, Any]] = []
        self.unverified_commands: list[dict[str, Any]] = []
        self.rx_parser = FullFrameParser()
        self.last_command: str | None = None
        self.last_error: str | None = None
        self.connected = False
        self.port_name = "Disconnected"
        self.listening = False
        self.board_ready = False
        self.raw_frame: list[int] | None = None
        self.frame_seen_at: float | None = None
        self.frame_seen_monotonic: float | None = None
        self.frame_signature = "unknown"
        self.bytes_3_to_6 = [0, 0, 0, 0]
        self.full_frame_tail: list[int] = []
        self.power_on = False
        self.mode = "off"
        self.auto_profile: str | None = None
        self.timer_minutes = 15
        self.remaining_seconds = 15 * 60
        self.last_tick = time.monotonic()
        self.power_started_monotonic: float | None = None
        self._last_timer_reset_at: float | None = None
        self._elapsed_accumulator = 0.0
        self.paused = False
        self.intensity_level = 2
        self.speed_level = 2
        self.foot_speed_level = 2
        self._auto_speed_program_started_at: float | None = None
        self.heat_on = False
        self.shoulders_on = False
        self.forearms_on = False
        self.legs_on = False
        self.buttocks_on = False
        self.foot_massage_on = False
        self.neck_on = False
        self.back_waist_on = False
        self.full_body_on = False
        self.flash_command_until: dict[str, float] = {}
        self.failed_command_until: dict[str, float] = {}
        self.check_until = 0.0
        self.mask_until = 0.0
        self.power_off_text_until = 0.0
        self.power_off_started_at = 0.0
        self.prompt_text = ""
        self.prompt_until = 0.0
        self.back_forward_cycle_1 = 0
        self.back_forward_cycle_2 = 0
        self.back_direction_known = False
        self.neck_direction_known = False
        # Boot-settle gate: set on the local power-on press that turns
        # the chair on, cleared on power-off. While the gate is active,
        # `BOOT_SETTLE_BLOCKED_COMMANDS` are rejected at the state layer.
        # The gate clears once `BOOT_SETTLE_MIN_SECONDS` AND
        # `BOOT_SETTLE_FRAMES` have both been satisfied, or after
        # `BOOT_SETTLE_TIMEOUT_SECONDS` as a fail-open.
        self.boot_settle_started_at: float | None = None
        self._running_frames_since_boot = 0
        self.hold_command: str | None = None
        self.hold_id: str | None = None
        self.hold_started_at: float | None = None
        self.hold_stop_requested_at: float | None = None
        self._muted_fields: dict[str, float] = {}
        self._drift_first_seen: dict[str, float] = {}
        self.drift: list[dict[str, Any]] = []
        # Frame history for debug view
        self.frame_history: list[FrameRecord] = []

    def _tick_locked(self) -> None:
        now = time.monotonic()
        if self.power_on and not self.paused and self.remaining_seconds > 0:
            elapsed = now - self.last_tick
            if elapsed > 0:
                self._elapsed_accumulator += elapsed
                if self._elapsed_accumulator >= 1.0:
                    whole_seconds = int(self._elapsed_accumulator)
                    self.remaining_seconds = max(
                        0, self.remaining_seconds - whole_seconds
                    )
                    self._elapsed_accumulator -= whole_seconds
        self.last_tick = now
        if self.power_on and self.mode == "auto":
            auto_speed = self._auto_speed_program_level_locked(now)
            if auto_speed is None:
                self._power_off_locked()

    def _remember_log_locked(self, line: str) -> None:
        self.backend_log.append(line)
        if len(self.backend_log) > config.BACKEND_LOG_LIMIT:
            self.backend_log = self.backend_log[-config.BACKEND_LOG_LIMIT :]

    def _remember_command_locked(self, command: str, seq: int | None) -> None:
        self.command_history.append(
            {
                "command": command,
                "label": COMMAND_INDEX[command].label,
                "seq": seq,
                "at": time.time(),
            }
        )
        if len(self.command_history) > config.COMMAND_HISTORY_LIMIT:
            self.command_history = self.command_history[-config.COMMAND_HISTORY_LIMIT :]

    def set_connection(self, connected: bool, port_name: str) -> None:
        with self.lock:
            self.connected = connected
            self.port_name = port_name
            if not connected:
                self.listening = False
                self.board_ready = False

    def invalidate_frame(self) -> None:
        with self.lock:
            self.raw_frame = None
            self.frame_seen_at = None
            self.frame_seen_monotonic = None
            self.frame_signature = "unknown"
            self.bytes_3_to_6 = [0, 0, 0, 0]
            self.full_frame_tail = []
            self.rx_parser.reset()

    def note_error(self, message: str) -> None:
        with self.lock:
            self.last_error = message
            self._remember_log_locked(f"error: {message}")

    def note_backend_line(self, line: str) -> None:
        with self.lock:
            self._remember_log_locked(line)
            if "Chair read: ON" in line:
                self.listening = True
            elif "Chair read: OFF" in line:
                self.listening = False
            if line.startswith("Ready.") or line.startswith("Controls:"):
                self.board_ready = True

    def note_backend_rx_value(self, value: int) -> None:
        with self.lock:
            for parsed in self.rx_parser.feed(value):
                self._note_frame_locked(parsed)

    def note_frame(self, frame: list[int]) -> None:
        with self.lock:
            self._note_frame_locked(frame)

    def _note_frame_locked(self, frame: list[int]) -> None:
        if len(frame) != config.FULL_FRAME_LENGTH:
            return
        self.raw_frame = list(frame)
        self.frame_seen_at = time.time()
        self.frame_seen_monotonic = time.monotonic()
        self.bytes_3_to_6 = self._extract_matrix_bytes_locked()
        self.full_frame_tail = self.raw_frame[17:29]
        self.frame_signature = self._describe_frame_signature_locked()
        self._sync_from_frame_locked()
        # Record frame for debug view
        self._record_frame_locked(frame)

    def _record_frame_locked(self, frame: list[int]) -> None:
        record = FrameRecord(
            wall_time=time.time(),
            monotonic_time=time.monotonic(),
            raw=list(frame),
            signature=self.frame_signature,
            bytes_3_to_6=list(self.bytes_3_to_6),
            zones={
                "ramiona": self.shoulders_on,
                "przedramiona": self.forearms_on,
                "nogi": self.legs_on,
                "masaz_posladkow": self.buttocks_on,
                "masaz_stop": self.foot_massage_on,
                "szyja": self.neck_on,
                "plecy_i_talia": self.back_waist_on,
                "ogrzewanie": self.heat_on,
            },
            levels={
                "intensity": self.intensity_level,
                "speed": self.speed_level,
                "foot_speed": self.foot_speed_level,
            },
            mode=self.mode,
            power_on=self.power_on,
        )
        self.frame_history.append(record)
        if len(self.frame_history) > config.FRAME_HISTORY_LIMIT:
            self.frame_history = self.frame_history[-config.FRAME_HISTORY_LIMIT :]

    def _extract_matrix_bytes_locked(self) -> list[int]:
        if not self.raw_frame or len(self.raw_frame) < config.FULL_FRAME_LENGTH:
            return [0, 0, 0, 0]
        # Per matrix file Section 1 + brief sections 2.2–2.4, the four
        # authoritative mode/level/running bytes are at frame indices
        # 3, 4, 5, 6:
        #   [3] = mode-bit
        #   [4] = mode-byte (auto / back / neck / neck+back)
        #   [5] = intensity bucket (0x00=1, 0x0C=2, 0x0F=3)
        #   [6] = running flag (0x0F=running, 0x00=idle/cleared)
        # Byte 11 is a separate "shared last-touched LVL register" the
        # chair updates on +/- presses; it coincidentally takes the same
        # values in steady auto-running but is NOT the intensity bucket.
        return [
            self.raw_frame[3],
            self.raw_frame[4],
            self.raw_frame[5],
            self.raw_frame[6],
        ]

    def _describe_frame_signature_locked(self) -> str:
        b3, b4, b5, b6 = self.bytes_3_to_6
        if [b3, b4, b5, b6] == [0x00, 0x00, 0x00, 0x00]:
            return "all-zero"
        if b3 == 0x04 and b4 == 0x02 and b6 == 0x0F:
            # Tail bytes are stable enough for immediate command verification,
            # but live timer traces show they later carry countdown-ish values.
            # Do not label a continuous frame as program A/B/C/D here or the
            # debug API will contradict the model after `czas` / countdown.
            return "auto-running"
        if [b3, b4, b5, b6] == [0x00, 0x0E, 0x0C, 0x0F]:
            return "manual-neck"
        if [b3, b4, b5, b6] == [0x04, 0x0E, 0x0C, 0x0F]:
            return "manual-neck-back"
        if [b3, b4, b5, b6] == [0x04, 0x0C, 0x0C, 0x0F]:
            return "manual-back"
        if [b3, b4, b5, b6] == [0x04, 0x02, 0x0F, 0x0F]:
            return "intensity-up-signature"
        if b4 == 0x0E:
            return "neck-signature"
        if b4 == 0x0C:
            return "back-waist-signature"
        return f"b3={b3:02X} b4={b4:02X} b5={b5:02X} b6={b6:02X}"

    def _running_signature_locked(self) -> bool:
        return self.power_on and self.mode != "off"

    def _show_intensity_ui_locked(self) -> bool:
        return self._running_signature_locked() and (
            self.shoulders_on or self.forearms_on or self.legs_on
        )

    def _show_speed_ui_locked(self) -> bool:
        return self._running_signature_locked() and (
            (self.mode == "manual" and (self.neck_on or self.back_waist_on))
            or (
                self.mode == "auto"
                and self.auto_profile in {"A", "B", "C", "D"}
                and (self.neck_on or self.back_waist_on or self.auto_profile == "C")
            )
        )

    def _auto_speed_program_level_locked(
        self, now: float | None = None
    ) -> int | None | object:
        if (
            self.mode != "auto"
            or self.auto_profile not in {"A", "B", "C", "D"}
            or self._auto_speed_program_started_at is None
        ):
            return UNKNOWN
        if now is None:
            now = time.monotonic()
        elapsed = self._auto_speed_elapsed_seconds_locked(
            self._auto_speed_program_started_at, now
        )
        return speed_at_elapsed(self.auto_profile, elapsed)

    def _auto_speed_elapsed_seconds_locked(self, started_at: float, now: float) -> int:
        raw_elapsed = int(now - started_at)
        return max(0, raw_elapsed - config.AUTO_SPEED_PROGRAM_OFFSET_SECONDS)

    def _effective_speed_level_locked(self) -> int:
        auto_speed = self._auto_speed_program_level_locked()
        if auto_speed is UNKNOWN:
            return self.speed_level
        if auto_speed is None:
            return self.speed_level
        return auto_speed

    def _can_adjust_foot_speed_locked(self) -> bool:
        return self._running_signature_locked() and self.foot_massage_on

    def _show_foot_speed_ui_locked(self) -> bool:
        return self._can_adjust_foot_speed_locked() and not (
            self.mode == "auto" and self.auto_profile == "C"
        )

    def _clear_zones_locked(self) -> None:
        self.shoulders_on = False
        self.forearms_on = False
        self.legs_on = False
        self.buttocks_on = False
        self.foot_massage_on = False
        self.neck_on = False
        self.back_waist_on = False
        self.full_body_on = False

    def _clear_prompt_locked(self) -> None:
        self.prompt_text = ""
        self.prompt_until = 0.0

    def _mark_auto_direction_unknown_locked(self) -> None:
        # User live observation 2026-04-30: auto mode may move the
        # back/neck direction state internally. No repeated frame mapping
        # proves A1/A2 or b1/b2, so auto mode must not leave a confident
        # local counter behind.
        self.back_direction_known = False
        self.neck_direction_known = False
        self.back_forward_cycle_1 = 0
        self.back_forward_cycle_2 = 0

    def _switch_to_manual_zone_locked(self, zone_key: str) -> None:
        self.mode = "manual"
        self.auto_profile = None
        self._auto_speed_program_started_at = None
        self.paused = False
        self.check_until = 0.0
        self.mask_until = 0.0
        self._clear_zones_locked()
        if zone_key == "szyja":
            self.neck_on = True
            if self.neck_direction_known:
                self._set_prompt_locked(f"A{self.back_forward_cycle_2}")
            else:
                self._clear_prompt_locked()
                self._remember_log_locked(
                    "direction prompt suppressed: neck direction unknown after auto"
                )
        else:
            self.back_waist_on = True
            if self.back_direction_known:
                self._set_prompt_locked(f"b{self.back_forward_cycle_1}")
            else:
                self._clear_prompt_locked()
                self._remember_log_locked(
                    "direction prompt suppressed: back direction unknown after auto"
                )

    def _ensure_back_forward_counters_locked(self) -> None:
        if self.back_forward_cycle_1 == 0:
            self.back_forward_cycle_1 = 1
        if self.back_forward_cycle_2 == 0:
            self.back_forward_cycle_2 = 1

    def _advance_back_cycle_locked(self) -> int | None:
        if not self.back_direction_known:
            return None
        self._ensure_back_forward_counters_locked()
        self.back_forward_cycle_1 = 1 if self.back_forward_cycle_1 == 2 else 2
        return self.back_forward_cycle_1

    def _advance_neck_cycle_locked(self) -> int | None:
        if not self.neck_direction_known:
            return None
        self._ensure_back_forward_counters_locked()
        self.back_forward_cycle_2 = 1 if self.back_forward_cycle_2 == 2 else 2
        return self.back_forward_cycle_2

    def _clear_expired_mutes_locked(self) -> None:
        now = time.monotonic()
        self._muted_fields = {
            field: deadline
            for field, deadline in self._muted_fields.items()
            if deadline > now
        }

    def _field_muted_locked(self, field: str) -> bool:
        self._clear_expired_mutes_locked()
        return field in self._muted_fields

    def _any_muted_locked(self, fields: set[str]) -> bool:
        self._clear_expired_mutes_locked()
        return any(field in self._muted_fields for field in fields)

    def _mute_fields_locked(
        self, fields: set[str], seconds: float = config.MUTE_SECONDS
    ) -> None:
        deadline = monotonic_deadline(seconds)
        for field in fields:
            self._muted_fields[field] = deadline

    def extend_mute(
        self, fields: set[str], seconds: float = config.MUTE_SECONDS
    ) -> None:
        with self.lock:
            self._mute_fields_locked(fields, seconds)

    def _clear_mute_locked(self, fields: set[str]) -> None:
        for field in fields:
            self._muted_fields.pop(field, None)

    def _set_frame_field_locked(self, field: str, value: Any) -> None:
        if not self._field_muted_locked(field):
            setattr(self, field, value)

    def _sync_from_frame_locked(self) -> None:
        # Per spec: the chair frame is informational. The state machine is the
        # source of truth for SVG visibility (zones, heat, paused, timer).
        # The frame authoritatively answers a small set of fields only:
        #   1. Is the chair powered off? (all-zero payload from byte 2)
        #   2. What auto profile is the chair running? (frame[17..20] tail)
        #   3. What is the mode signature? (bytes 3, 4, 5, 6) — drift only.
        #   4. What is the intensity bucket? (byte 5) — drift only.
        #   5. What is the foot-speed bucket? (byte 7) when foot massage is on.
        # Zone bits in frame[21] and heat in frame[23] are NOT trusted.
        if not self.raw_frame or len(self.raw_frame) < config.FULL_FRAME_LENGTH:
            return
        b3, b4, b5, b6 = self.bytes_3_to_6
        full_payload_zero = all(value == 0x00 for value in self.raw_frame[2:])

        if full_payload_zero:
            if not self._field_muted_locked("power_on"):
                self._power_off_locked()
            self._update_drift_locked()
            return

        if [b3, b4, b5, b6] == [0x00, 0x00, 0x00, 0x00]:
            # Mode bytes cleared but payload non-zero: this is an overlay
            # flash (grawitacja_zero / oparcie_w_*). Do not change power_on,
            # do not clear zones, do not log drift — wait for state to
            # restore.
            return

        # Past both early-returns: this is a real running frame. Count it
        # toward the boot-settle gate; the gate only releases manual
        # zone/direction/scalar commands once the chair has streamed at
        # least `config.BOOT_SETTLE_FRAMES` of these.
        if self.boot_settle_started_at is not None:
            self._running_frames_since_boot += 1

        # First sync after connect / wake: if our model thinks the chair
        # is off but a non-zero frame arrives, the chair is actually
        # running. Read the actual mode from the frame instead of inventing
        # default-A. Manual file says default-A is what shows after a fresh
        # power press, but the chair may already be in some other state
        # when our process attaches (user pressed buttons via OEM panel,
        # or our prior session left the chair mid-run).
        if not self.power_on and not self._field_muted_locked("power_on"):
            self.power_on = True
            self.power_started_monotonic = self.frame_seen_monotonic or time.monotonic()
            if self.mode == "off" and not self._any_muted_locked(AUTO_PROFILE_FIELDS):
                if b4 == 0x0E and b3 == 0x00:
                    self.mode = "manual"
                    self.auto_profile = None
                elif b4 == 0x0E and b3 == 0x04:
                    self.mode = "manual"
                    self.auto_profile = None
                elif b4 == 0x0C:
                    self.mode = "manual"
                    self.auto_profile = None
                elif b3 == 0x04 and b4 == 0x02:
                    # Auto running: adopt profile from tail if known. If the
                    # tail is unknown, do not invent default-A; live traces show
                    # frame[17..20] also changes with czas/countdown state.
                    tail = (
                        tuple(self.full_frame_tail[:4]) if self.full_frame_tail else ()
                    )
                    detected = AUTO_TAIL_SIGNATURES.get(tail)
                    if detected:
                        self._apply_auto_profile_locked(detected)
                    else:
                        self._set_unknown_auto_locked()
                else:
                    self.mode = "auto"
                    self.auto_profile = None

        # Auto profile detection from tail bytes: adopt only when we are
        # in auto mode and the model has no profile yet (initial sync).
        # Otherwise log drift on mismatch.
        if b3 == 0x04 and b4 == 0x02 and b6 == 0x0F:
            tail = tuple(self.full_frame_tail[:4]) if self.full_frame_tail else ()
            detected = AUTO_TAIL_SIGNATURES.get(tail)
            if detected and not self._any_muted_locked(AUTO_PROFILE_FIELDS):
                detected_profile = detected
                if self.mode == "auto" and self.auto_profile is None:
                    self._apply_auto_profile_locked(detected_profile)

        # Bench + matrix file confirm: frame byte 5 IS the dedicated
        # intensity bucket (0x00=level 1, 0x0C=level 2, 0x0F=level 3).
        # Byte 11 (which an earlier indexing bug was reading as "byte 5")
        # is a separate shared-LVL register and is unreliable; we ignore
        # it. Adopt intensity_level from byte 5 in any running state where
        # the field is not muted by a recent press. Skip in overlay flash
        # (mode bytes zero) and power-off (payload zero).
        if not self._field_muted_locked("intensity_level"):
            if b6 == 0x0F:
                if b5 == 0x0F and self.intensity_level != 3:
                    self.intensity_level = 3
                elif b5 == 0x0C and self.intensity_level != 2:
                    self.intensity_level = 2
                elif b5 == 0x00 and self.intensity_level != 1:
                    self.intensity_level = 1

        # Manual back/neck speed uses byte 11 as the displayed Predkosc
        # level. The same byte is a shared/last-touched register in other
        # contexts, so only read it while the chair frame is in manual mode.
        if (
            not self._field_muted_locked("speed_level")
            and b4 in {0x0C, 0x0E}
            and len(self.raw_frame) > 11
        ):
            frame_speed = self._level_from_bucket_locked(self.raw_frame[11])
            if frame_speed is not UNKNOWN and self.speed_level != frame_speed:
                self.speed_level = frame_speed

        # foot_speed_level: byte 7 is the dedicated bucket (bench-confirmed
        # 2026-04-28). Reliable in any running state.
        if (
            not self._field_muted_locked("foot_speed_level")
            and len(self.raw_frame) > 7
            and self.foot_massage_on
        ):
            b7 = self.raw_frame[7]
            if b7 == 0x0F and self.foot_speed_level != 3:
                self.foot_speed_level = 3
            elif b7 == 0x0C and self.foot_speed_level != 2:
                self.foot_speed_level = 2
            elif b7 == 0x00 and self.foot_speed_level != 1:
                self.foot_speed_level = 1

        self._update_drift_locked()

    def _level_from_bucket_locked(self, value: int) -> int | object:
        if value == 0x0F:
            return 3
        if value == 0x0C:
            return 2
        if value == 0x00:
            return 1
        return UNKNOWN

    def _set_prompt_locked(
        self, text: str, seconds: float = config.PROMPT_SECONDS
    ) -> None:
        self.prompt_text = text
        self.prompt_until = monotonic_deadline(seconds)

    def _flash_locked(
        self, command: str, seconds: float = config.COMMAND_FLASH_SECONDS
    ) -> None:
        self.flash_command_until[command] = monotonic_deadline(seconds)

    def _flash_failed_locked(self, command: str) -> None:
        self.failed_command_until[command] = monotonic_deadline(
            config.FAILED_FLASH_SECONDS
        )

    def _reset_timer_to_locked(self, minutes: int, reason: str) -> None:
        now = time.monotonic()
        if (
            self._last_timer_reset_at is not None
            and now - self._last_timer_reset_at < 0.5
            and self.timer_minutes == minutes
            and self.remaining_seconds == minutes * 60
        ):
            self._remember_log_locked(
                f"timer reset skipped: duplicate within 500ms "
                f"minutes={minutes} reason={reason}"
            )
            return
        self.timer_minutes = minutes
        self.remaining_seconds = minutes * 60
        self.last_tick = now
        self._elapsed_accumulator = 0.0
        self._last_timer_reset_at = now

    def _set_default_auto_locked(self) -> None:
        self._apply_auto_profile_locked("A")
        self._reset_timer_to_locked(15, "default-auto")

    def _set_unknown_auto_locked(self) -> None:
        # The frame proves auto mode, but not a stable A/B/C/D profile.
        # On app attach, unknown timer/countdown tails can otherwise make
        # the UI claim default-A zones that the hardware LCD is not showing.
        self.mode = "auto"
        self.auto_profile = None
        self._auto_speed_program_started_at = None
        self.paused = False
        self._clear_zones_locked()
        self._mark_auto_direction_unknown_locked()

    def _apply_auto_profile_locked(self, profile: str) -> None:
        self.mode = "auto"
        self.auto_profile = profile
        self._auto_speed_program_started_at = time.monotonic()
        self.paused = False
        self._mark_auto_direction_unknown_locked()
        self.intensity_level = 2
        self.speed_level = 2
        self.foot_speed_level = 2
        self.heat_on = False
        if profile == "A":
            self.shoulders_on = True
            self.forearms_on = True
            self.legs_on = True
            self.buttocks_on = False
            self.foot_massage_on = True
            self.neck_on = True
            self.back_waist_on = True
        elif profile == "B":
            self.shoulders_on = True
            self.forearms_on = True
            self.legs_on = True
            self.buttocks_on = True
            self.foot_massage_on = True
            self.neck_on = True
            self.back_waist_on = True
        elif profile == "C":
            self.shoulders_on = True
            self.forearms_on = False
            self.legs_on = False
            self.buttocks_on = True
            self.foot_massage_on = False
            self.neck_on = True
            self.back_waist_on = True
        elif profile == "D":
            self.shoulders_on = True
            self.forearms_on = True
            self.legs_on = True
            self.buttocks_on = True
            self.foot_massage_on = True
            self.neck_on = True
            self.back_waist_on = True
        self.full_body_on = self.shoulders_on and self.forearms_on and self.legs_on

    def _power_off_locked(self) -> None:
        self.power_on = False
        self.mode = "off"
        self.auto_profile = None
        self._auto_speed_program_started_at = None
        self.power_started_monotonic = None
        self.paused = False
        self.heat_on = False
        self.shoulders_on = False
        self.forearms_on = False
        self.legs_on = False
        self.buttocks_on = False
        self.foot_massage_on = False
        self.neck_on = False
        self.back_waist_on = False
        self.full_body_on = False
        self.check_until = monotonic_deadline(config.POWER_OFF_TEXT_SECONDS)
        self.mask_until = self.check_until
        self.power_off_text_until = monotonic_deadline(config.POWER_OFF_TEXT_SECONDS)
        self.power_off_started_at = time.monotonic()
        self.prompt_text = ""
        self.prompt_until = 0.0
        self.back_direction_known = False
        self.neck_direction_known = False
        self.back_forward_cycle_1 = 0
        self.back_forward_cycle_2 = 0
        self.boot_settle_started_at = None
        self._running_frames_since_boot = 0
        self.hold_command = None
        self.hold_id = None
        self.hold_started_at = None
        self.hold_stop_requested_at = None

    def _begin_boot_settle_locked(self) -> None:
        self.boot_settle_started_at = time.monotonic()
        self._running_frames_since_boot = 0

    def _boot_settling_locked(self) -> bool:
        if self.boot_settle_started_at is None:
            return False
        now = time.monotonic()
        elapsed = now - self.boot_settle_started_at
        if elapsed >= config.BOOT_SETTLE_TIMEOUT_SECONDS:
            # Fail-open: do not pin the UI in "booting" if the chair never
            # starts streaming running frames.
            return False
        if (
            elapsed >= config.BOOT_SETTLE_MIN_SECONDS
            and self._running_frames_since_boot >= config.BOOT_SETTLE_FRAMES
        ):
            return False
        return True

    def begin_hold_command(
        self, command: str, hold_id: str, seq: int | None = None
    ) -> CommandOutcome:
        with self.lock:
            self._tick_locked()
            if command not in HOLD_TO_ADJUST_COMMANDS:
                raise KeyError(command)
            if self.hold_command is not None:
                reason = "backrest hold already active"
                self._remember_log_locked(
                    f"blocked hold_start seq={seq} command={command}: {reason}"
                )
                return CommandOutcome(False, set(), {}, reason)
            blocked_reason = self._blocked_reason_locked(command)
            if blocked_reason:
                self._remember_log_locked(
                    f"blocked hold_start seq={seq} command={command}: {blocked_reason}"
                )
                return CommandOutcome(False, set(), {}, blocked_reason)

            self.last_command = command
            self._remember_command_locked(command, seq)
            self.hold_command = command
            self.hold_id = hold_id
            self.hold_started_at = time.monotonic()
            self.hold_stop_requested_at = None
            self.check_until = monotonic_deadline(config.HOLD_MAX_SECONDS)
            self.mask_until = monotonic_deadline(config.HOLD_MAX_SECONDS)
            fields = set(COMMAND_MUTE_FIELDS.get(command, set()))
            self._mute_fields_locked(fields, config.HOLD_MAX_SECONDS)
            self._remember_log_locked(
                f"hold_start seq={seq} command={command} hold_id={hold_id}"
            )
            return CommandOutcome(True, fields, self._snapshot_fields_locked(fields))

    def request_hold_stop(
        self, command: str | None, hold_id: str | None, reason: str
    ) -> None:
        with self.lock:
            if self.hold_command is None:
                return
            if command is not None and command != self.hold_command:
                return
            if hold_id is not None and hold_id != self.hold_id:
                return
            if self.hold_stop_requested_at is None:
                self.hold_stop_requested_at = time.monotonic()
                self.check_until = 0.0
                self.mask_until = 0.0
                self._remember_log_locked(
                    f"hold_stop_requested command={self.hold_command} reason={reason}"
                )

    def finish_hold_command(
        self, command: str | None, hold_id: str | None, reason: str
    ) -> None:
        with self.lock:
            if self.hold_command is None:
                return
            if command is not None and command != self.hold_command:
                return
            if hold_id is not None and hold_id != self.hold_id:
                return
            prior = self.hold_command
            self.hold_command = None
            self.hold_id = None
            self.hold_started_at = None
            self.hold_stop_requested_at = None
            self.check_until = 0.0
            self.mask_until = 0.0
            self._remember_log_locked(f"hold_finished command={prior} reason={reason}")

    def apply_command(self, command: str, seq: int | None = None) -> CommandOutcome:
        with self.lock:
            self._tick_locked()
            if command not in COMMAND_INDEX:
                raise KeyError(command)
            blocked_reason = self._blocked_reason_locked(command)
            if blocked_reason:
                self._remember_log_locked(
                    f"blocked seq={seq} command={command}: {blocked_reason}"
                )
                return CommandOutcome(False, set(), {}, blocked_reason)

            self.last_command = command
            self._remember_command_locked(command, seq)
            fields = set(COMMAND_MUTE_FIELDS.get(command, set()))
            unknown_auto_profile_press = (
                command == "tryb_automatyczny"
                and self.mode == "auto"
                and self.auto_profile is None
            )

            if command == "power":
                if self.power_on:
                    self._power_off_locked()
                else:
                    self.power_on = True
                    self.power_started_monotonic = time.monotonic()
                    self.check_until = 0.0
                    self.mask_until = 0.0
                    self.power_off_text_until = 0.0
                    self._set_default_auto_locked()
                    self._begin_boot_settle_locked()
                self._mute_fields_locked(fields)
                return CommandOutcome(
                    True, fields, self._snapshot_fields_locked(fields)
                )

            if command in MOMENTARY_COMMANDS:
                self._flash_locked(command)

            if command in FRAME_OBSERVED_COMMANDS:
                return CommandOutcome(True, set(), {})

            if command in {"grawitacja_zero", "oparcie_w_gore", "oparcie_w_dol"}:
                self.check_until = monotonic_deadline(config.OVERLAY_SECONDS)
                self.mask_until = monotonic_deadline(config.OVERLAY_SECONDS)
                self._mute_fields_locked(fields)
                return CommandOutcome(
                    True, fields, self._snapshot_fields_locked(fields)
                )

            self._apply_non_power_command_locked(command)
            if unknown_auto_profile_press:
                # If app.py attached mid-session, UART can prove auto mode but
                # not the current A/B/C/D profile. A tryb_automatyczny press
                # advances from the real hardware profile, not from our
                # unknown placeholder, so do not guess B. Leave profile fields
                # unmuted so the first stable post-DONE tail can adopt the
                # actual resulting profile.
                fields = set()
            self._mute_fields_locked(fields)
            return CommandOutcome(True, fields, self._snapshot_fields_locked(fields))

    def _blocked_reason_locked(self, command: str) -> str | None:
        if command == "power":
            return None
        if self.hold_command is not None:
            if command == self.hold_command:
                return None
            return "backrest hold active"
        if not self.power_on:
            return "power is off"
        if command in BOOT_SETTLE_BLOCKED_COMMANDS and self._boot_settling_locked():
            return "chair booting"
        if self.paused and command != "pauza":
            return "paused"
        if self.mode == "auto" and command in AUTO_DEAD_COMMANDS:
            return "disabled in auto mode"
        if (
            command in {"sila_nacisku_plus", "sila_nacisku_minus"}
            and not self._show_intensity_ui_locked()
        ):
            return "intensity controls hidden"
        if (
            command == "predkosc_masazu_stop"
            and not self._can_adjust_foot_speed_locked()
        ):
            return "foot speed controls hidden"
        if command in {"predkosc_plus", "predkosc_minus"} and (
            self.mode != "manual" or not self._show_speed_ui_locked()
        ):
            return "speed controls hidden"
        if command == "do_przodu_do_tylu_1" and (
            self.mode != "manual" or not self.back_waist_on
        ):
            return "back direction unavailable"
        if command == "do_przodu_do_tylu_2" and (
            self.mode != "manual" or not self.neck_on
        ):
            return "neck direction unavailable"
        return None

    def _apply_non_power_command_locked(self, command: str) -> None:
        if command == "tryb_automatyczny":
            if self.mode != "auto":
                self._apply_auto_profile_locked("A")
                self._set_prompt_locked("F1")
                return
            if self.auto_profile is None:
                self.mode = "auto"
                self.auto_profile = None
                self.paused = False
                self._clear_zones_locked()
                self._mark_auto_direction_unknown_locked()
                return
            next_profile = AUTO_PROFILE_SEQUENCE[0]
            if self.auto_profile in AUTO_PROFILE_SEQUENCE:
                current_index = AUTO_PROFILE_SEQUENCE.index(self.auto_profile)
                next_profile = AUTO_PROFILE_SEQUENCE[
                    (current_index + 1) % len(AUTO_PROFILE_SEQUENCE)
                ]
            self._apply_auto_profile_locked(next_profile)
            self._set_prompt_locked(f"F{['A', 'B', 'C', 'D'].index(next_profile) + 1}")
            return
        if command == "czas":
            current_index = TIME_OPTIONS_MINUTES.index(self.timer_minutes)
            next_minutes = TIME_OPTIONS_MINUTES[
                (current_index + 1) % len(TIME_OPTIONS_MINUTES)
            ]
            self._reset_timer_to_locked(next_minutes, "czas")
            return
        if command == "pauza":
            self.paused = not self.paused
            return
        if command == "ogrzewanie":
            self.heat_on = not self.heat_on
            return
        if command == "masaz_stop":
            self.foot_massage_on = not self.foot_massage_on
            return
        if command == "masaz_posladkow":
            self.buttocks_on = not self.buttocks_on
            return
        if command == "masaz_calego_ciala":
            next_value = not self.full_body_on
            self.full_body_on = next_value
            self.shoulders_on = next_value
            self.forearms_on = next_value
            self.legs_on = next_value
            return
        if command == "ramiona":
            self.shoulders_on = not self.shoulders_on
            self.full_body_on = self.shoulders_on and self.forearms_on and self.legs_on
            return
        if command == "przedramiona":
            self.forearms_on = not self.forearms_on
            self.full_body_on = self.shoulders_on and self.forearms_on and self.legs_on
            return
        if command == "nogi":
            self.legs_on = not self.legs_on
            self.full_body_on = self.shoulders_on and self.forearms_on and self.legs_on
            return
        if command == "plecy_i_talia":
            if self.mode == "auto":
                self._switch_to_manual_zone_locked("plecy_i_talia")
            else:
                self._ensure_back_forward_counters_locked()
                self.back_waist_on = not self.back_waist_on
                self.mode = "manual"
                self.auto_profile = None
                self._auto_speed_program_started_at = None
                if self.back_waist_on:
                    if self.back_direction_known:
                        self._set_prompt_locked(f"b{self.back_forward_cycle_1}")
                    else:
                        self._clear_prompt_locked()
            return
        if command == "szyja":
            if self.mode == "auto":
                self._switch_to_manual_zone_locked("szyja")
            else:
                self._ensure_back_forward_counters_locked()
                self.neck_on = not self.neck_on
                self.mode = "manual"
                self.auto_profile = None
                self._auto_speed_program_started_at = None
                if self.neck_on:
                    if self.neck_direction_known:
                        self._set_prompt_locked(f"A{self.back_forward_cycle_2}")
                    else:
                        self._clear_prompt_locked()
            return
        if command == "predkosc_masazu_stop":
            self.foot_speed_level = (
                1 if self.foot_speed_level >= 3 else self.foot_speed_level + 1
            )
            return
        if command == "sila_nacisku_plus":
            self.intensity_level = clamp(self.intensity_level + 1, 1, 3)
            return
        if command == "sila_nacisku_minus":
            self.intensity_level = clamp(self.intensity_level - 1, 1, 3)
            return
        if command == "predkosc_plus":
            self.speed_level = clamp(self.speed_level + 1, 1, 3)
            return
        if command == "predkosc_minus":
            self.speed_level = clamp(self.speed_level - 1, 1, 3)
            return
        if command == "do_przodu_do_tylu_1":
            index = self._advance_back_cycle_locked()
            if index is None:
                self._clear_prompt_locked()
                self._remember_log_locked(
                    "direction prompt suppressed: back direction unknown"
                )
                return
            self._set_prompt_locked(f"b{index}")
            return
        if command == "do_przodu_do_tylu_2":
            index = self._advance_neck_cycle_locked()
            if index is None:
                self._clear_prompt_locked()
                self._remember_log_locked(
                    "direction prompt suppressed: neck direction unknown"
                )
                return
            self._set_prompt_locked(f"A{index}")

    def _snapshot_fields_locked(self, fields: set[str]) -> dict[str, Any]:
        return {field: getattr(self, field) for field in fields if hasattr(self, field)}

    def verify_command(
        self, command: str, seq: int, fields: set[str], expected: dict[str, Any]
    ) -> VerifyResult:
        with self.lock:
            disagreements: list[dict[str, Any]] = []
            checked = 0
            for field in sorted(fields):
                actual = self._frame_value_for_field_locked(field)
                if command == "predkosc_masazu_stop" and field == "foot_speed_level":
                    raw_b7 = None
                    if self.raw_frame and len(self.raw_frame) > 7:
                        raw_b7 = self.raw_frame[7]
                    expected_value = expected.get(field, UNKNOWN)
                    expected_log = (
                        "UNKNOWN" if expected_value is UNKNOWN else expected_value
                    )
                    actual_log = "UNKNOWN" if actual is UNKNOWN else actual
                    raw_log = "None" if raw_b7 is None else f"0x{raw_b7:02X}"
                    self._remember_log_locked(
                        "verify foot-speed "
                        f"seq={seq} expected={expected_log} actual={actual_log} "
                        f"raw_b7={raw_log} bytes_3_to_6={self.bytes_3_to_6}"
                    )
                if actual is UNKNOWN:
                    continue
                checked += 1
                expected_value = expected.get(field, UNKNOWN)
                if expected_value is UNKNOWN or actual != expected_value:
                    disagreements.append(
                        {
                            "field": field,
                            "expected": expected_value,
                            "actual": actual,
                        }
                    )
            if fields and checked == 0:
                if command in MODEL_ONLY_COMMANDS:
                    self._clear_mute_locked(fields)
                    self._remember_log_locked(
                        f"verify model-only ok seq={seq} command={command}: "
                        f"no confirmed frame mapping for {sorted(fields)}"
                    )
                    return VerifyResult(True, 0, [])
                self._remember_log_locked(
                    f"verify unverified seq={seq} command={command}: "
                    f"no confirmed field for {sorted(fields)}"
                )
                return VerifyResult(False, 0, [], unverified=True)
            if not disagreements:
                self._clear_mute_locked(fields)
                self._remember_log_locked(
                    f"verify ok seq={seq} command={command} checked={checked}"
                )
                return VerifyResult(True, checked, [])
            self._remember_log_locked(
                f"verify disagree seq={seq} command={command}: {disagreements}"
            )
            return VerifyResult(False, checked, disagreements)

    def note_unverified_command(self, command: str, seq: int, fields: set[str]) -> None:
        with self.lock:
            self._clear_mute_locked(fields)
            self.unverified_commands.append(
                {
                    "seq": seq,
                    "command": command,
                    "at": time.time(),
                    "fields": sorted(fields),
                }
            )
            if len(self.unverified_commands) > config.FAILED_COMMAND_LIMIT:
                self.unverified_commands = self.unverified_commands[
                    -config.FAILED_COMMAND_LIMIT :
                ]
            self._remember_log_locked(
                f"unverified seq={seq} command={command}: "
                f"no confirmed frame mapping for {sorted(fields)}"
            )

    def surrender_command(
        self,
        command: str,
        seq: int,
        fields: set[str],
        disagreements: list[dict[str, Any]],
    ) -> None:
        with self.lock:
            self._clear_mute_locked(fields)
            self.last_error = (
                f"command {command} seq={seq} not confirmed by chair frame"
            )
            self.failed_commands.append(
                {
                    "seq": seq,
                    "command": command,
                    "at": time.time(),
                    "disagreements": disagreements,
                }
            )
            if len(self.failed_commands) > config.FAILED_COMMAND_LIMIT:
                self.failed_commands = self.failed_commands[
                    -config.FAILED_COMMAND_LIMIT :
                ]
            self._flash_failed_locked(command)
            self._remember_log_locked(
                f"command failed seq={seq} command={command}: {disagreements}"
            )

    def _frame_value_for_field_locked(self, field: str) -> Any:
        # The frame answers four fields authoritatively. Everything else is
        # UNKNOWN — the model is the source of truth.
        if not self.raw_frame or len(self.raw_frame) < config.FULL_FRAME_LENGTH:
            return UNKNOWN
        b3, b4, b5, b6 = self.bytes_3_to_6
        payload_zero = all(value == 0x00 for value in self.raw_frame[2:])
        mode_bytes_zero = [b3, b4, b5, b6] == [0x00, 0x00, 0x00, 0x00]
        if field == "power_on":
            return not payload_zero
        if field == "mode":
            if payload_zero:
                return "off"
            if mode_bytes_zero:
                # Overlay flash: mode is indeterminate, do not contradict
                # the model.
                return UNKNOWN
            if b3 == 0x04 and b4 == 0x02:
                return "auto"
            if b4 in {0x0C, 0x0E}:
                return "manual"
            return UNKNOWN
        if field == "auto_profile":
            if b3 == 0x04 and b4 == 0x02 and b6 == 0x0F:
                detected = AUTO_TAIL_SIGNATURES.get(tuple(self.full_frame_tail[:4]))
                if detected:
                    return detected
            return UNKNOWN
        if field == "intensity_level":
            # Per matrix file + bench: frame byte 5 IS the intensity bucket
            # in any running state. Only return UNKNOWN when chair isn't
            # running (b6 != 0x0F).
            if payload_zero or mode_bytes_zero or b6 != 0x0F:
                return UNKNOWN
            if b5 == 0x0F:
                return 3
            if b5 == 0x0C:
                return 2
            if b5 == 0x00:
                return 1
            return UNKNOWN
        if field == "foot_speed_level":
            # Bench-confirmed 2026-04-28: byte 7 is a dedicated foot-speed
            # bucket. 0x00=1, 0x0C=2, 0x0F=3. User live checks on
            # 2026-04-30 showed false UNVERIFIED entries when byte 6 was
            # not 0x0F even though the UI and hardware were correct, so byte
            # 6 is not used as the gate here. Only verify when foot massage
            # is part of the current model; otherwise the bucket is not a
            # visible/meaningful state.
            if payload_zero or mode_bytes_zero or not self.foot_massage_on:
                return UNKNOWN
            b7 = self.raw_frame[7]
            if b7 == 0x0F:
                return 3
            if b7 == 0x0C:
                return 2
            if b7 == 0x00:
                return 1
            return UNKNOWN
        if field == "speed_level":
            # Bench-confirmed 2026-04-28 timer-level-display-sync:
            # in manual mode, byte 11 mirrors the visible Predkosc-LVL bucket
            # (0x00=1, 0x0C=2, 0x0F=3). Outside manual mode it is shared with
            # other level presses, so keep it UNKNOWN there.
            if payload_zero or mode_bytes_zero or b4 not in {0x0C, 0x0E}:
                return UNKNOWN
            return self._level_from_bucket_locked(self.raw_frame[11])
        return UNKNOWN

    def _timer_diagnostics_locked(
        self, now: float, effective_speed_level: int | object | None
    ) -> dict[str, Any]:
        power_elapsed = None
        if self.power_started_monotonic is not None:
            power_elapsed = round(max(0.0, now - self.power_started_monotonic), 3)
        auto_elapsed = None
        if self._auto_speed_program_started_at is not None and self.auto_profile in {
            "A",
            "B",
            "C",
            "D",
        }:
            auto_elapsed = self._auto_speed_elapsed_seconds_locked(
                self._auto_speed_program_started_at, now
            )
        raw_13_to_16: list[int] = []
        if self.raw_frame and len(self.raw_frame) >= 17:
            raw_13_to_16 = self.raw_frame[13:17]
        visible_speed = (
            effective_speed_level
            if isinstance(effective_speed_level, int)
            else self.speed_level
        )
        return {
            "monotonic": round(now, 3),
            "power_on_elapsed_seconds": power_elapsed,
            "web_remaining_seconds": self.remaining_seconds,
            "web_time_text": self._current_time_text_locked(),
            "timer_minutes": self.timer_minutes,
            "mode": self.mode,
            "profile": self.auto_profile,
            "auto_pattern_elapsed_seconds": auto_elapsed,
            "visible_levels": {
                "intensity": self.intensity_level,
                "speed": visible_speed,
                "foot_speed": self.foot_speed_level,
            },
            "raw_timer_candidates": {
                "bytes_3_to_6": list(self.bytes_3_to_6),
                "raw_13_to_16": raw_13_to_16,
                "full_frame_tail": list(self.full_frame_tail),
            },
            "verification": {
                "failed_count": len(self.failed_commands),
                "unverified_count": len(self.unverified_commands),
                "drift_count": len(self.drift),
                "last_error": self.last_error,
            },
        }

    def assert_frame_consistent(self) -> list[dict[str, Any]]:
        with self.lock:
            return self._update_drift_locked()

    def press_diagnostics(self, limit: int = 25) -> list[dict[str, Any]]:
        # Per-press triage: for each recent press, snapshot the chair frame
        # just before and just after the command's wall-time. Lets a tech see
        # whether the OEM control board reacted to byte X (mode bytes / zone
        # byte 21 / heat byte 23 changed), independently of whether the
        # downstream motor stage physically moved. If the OEM frame confirms
        # the toggle but a motor is silent, the fault is past the OEM board.
        with self.lock:
            commands = list(self.command_history)[-limit:]
            frames = list(self.frame_history)
            results: list[dict[str, Any]] = []
            for entry in commands:
                press_ts = float(entry.get("at", 0.0))
                command = entry.get("command", "")
                byte_code = (
                    COMMAND_INDEX[command].code if command in COMMAND_INDEX else None
                )
                before = self._latest_frame_at_or_before_locked(frames, press_ts)
                after = self._earliest_frame_strictly_after_locked(frames, press_ts)
                results.append(
                    {
                        "seq": entry.get("seq"),
                        "command": command,
                        "label": entry.get("label"),
                        "byte_code": byte_code,
                        "at_wall": press_ts,
                        "frame_before": self._diagnostic_frame_view_locked(before),
                        "frame_after": self._diagnostic_frame_view_locked(after),
                        "delta": self._frame_delta_locked(before, after),
                    }
                )
            return results

    def _latest_frame_at_or_before_locked(
        self, frames: list[FrameRecord], ts: float
    ) -> FrameRecord | None:
        candidate: FrameRecord | None = None
        for rec in frames:
            if rec.wall_time <= ts:
                candidate = rec
            else:
                break
        return candidate

    def _earliest_frame_strictly_after_locked(
        self, frames: list[FrameRecord], ts: float
    ) -> FrameRecord | None:
        for rec in frames:
            if rec.wall_time > ts:
                return rec
        return None

    def _diagnostic_frame_view_locked(
        self, frame: FrameRecord | None
    ) -> dict[str, Any] | None:
        if frame is None:
            return None
        raw = frame.raw
        byte21 = raw[21] if len(raw) > 21 else 0
        byte23 = raw[23] if len(raw) > 23 else 0
        return {
            "ts": frame.wall_time,
            "signature": frame.signature,
            "mode_bytes_3_to_6": list(frame.bytes_3_to_6),
            "zone_byte21": byte21,
            "zone_byte21_bits": {
                "shoulders_bit0": bool(byte21 & 0x01),
                "forearms_bit1": bool(byte21 & 0x02),
                "legs_bit2": bool(byte21 & 0x04),
            },
            "heat_byte23": byte23,
            "heat_active": bool(byte23 & 0x0C),
            "mode": frame.mode,
            "power_on": frame.power_on,
        }

    def _frame_delta_locked(
        self, before: FrameRecord | None, after: FrameRecord | None
    ) -> dict[str, Any] | None:
        if before is None or after is None:
            return None
        before_raw = before.raw
        after_raw = after.raw
        b21_before = before_raw[21] if len(before_raw) > 21 else 0
        b21_after = after_raw[21] if len(after_raw) > 21 else 0
        b23_before = before_raw[23] if len(before_raw) > 23 else 0
        b23_after = after_raw[23] if len(after_raw) > 23 else 0
        return {
            "mode_bytes_changed": before.bytes_3_to_6 != after.bytes_3_to_6,
            "mode_bytes_before": list(before.bytes_3_to_6),
            "mode_bytes_after": list(after.bytes_3_to_6),
            "zone_byte21_xor": b21_before ^ b21_after,
            "heat_byte23_xor": b23_before ^ b23_after,
            "signature_changed": before.signature != after.signature,
            "signature_before": before.signature,
            "signature_after": after.signature,
        }

    def _update_drift_locked(self) -> list[dict[str, Any]]:
        disagreements: list[dict[str, Any]] = []
        now = time.monotonic()
        for field in (
            "power_on",
            "mode",
            "intensity_level",
            "speed_level",
            "foot_speed_level",
        ):
            if self._field_muted_locked(field):
                self._drift_first_seen.pop(field, None)
                continue
            actual = self._frame_value_for_field_locked(field)
            if actual is UNKNOWN:
                self._drift_first_seen.pop(field, None)
                continue
            expected = getattr(self, field)
            if actual == expected:
                self._drift_first_seen.pop(field, None)
                continue
            first_seen = self._drift_first_seen.setdefault(field, now)
            item = {
                "field": field,
                "model": expected,
                "frame": actual,
                "age_seconds": round(now - first_seen, 3),
            }
            disagreements.append(item)
        self.drift = [
            item
            for item in disagreements
            if item["age_seconds"] >= config.DRIFT_SECONDS
        ]
        return disagreements

    def _current_time_text_locked(self) -> str:
        now = time.monotonic()
        if not self.power_on:
            if now < self.power_off_text_until:
                return "OF"
            return ""
        if now < self.prompt_until:
            return self.prompt_text
        if self.remaining_seconds <= 0:
            return "0"
        # Calibrated visible minute. The chair LCD stays on the start
        # minute longer than the canonical countdown implies. Tune
        # `config.TIMER_DISPLAY_OFFSET_SECONDS` for Czas-NUMBER only; auto
        # speed bars have a separate offset because live testing showed the
        # display offset made Predkosc-LVL changes too late.
        visible_total_seconds = (
            self.remaining_seconds + config.TIMER_DISPLAY_OFFSET_SECONDS
        )
        minutes = max(1, visible_total_seconds // 60)
        return str(minutes)

    def _show_overlay_locked(self) -> bool:
        return self.power_on and time.monotonic() < self.mask_until

    def _expand_cumulative_level_layers_locked(self, visible: set[str]) -> set[str]:
        expanded = set(visible)
        for prefix in (
            "Sila_nacisku-LVL",
            "Predkosc-LVL",
            "Predkosc_masazu_stop-LVL",
        ):
            for level in range(3, 0, -1):
                label = f"{prefix}{level}"
                if label in expanded:
                    for fill in range(1, level):
                        expanded.add(f"{prefix}{fill}")
        return expanded

    def _visible_layers_locked(self) -> list[str]:
        now = time.monotonic()
        if not self.power_on:
            # Live panel correction: after power-off the screen is pure black.
            return ["Background"]
        if self._show_overlay_locked():
            # Overlay flash per manual sections 13/15/16: only Body, Czas-TEXT,
            # Czas-NUMBER, and the black Background survive; SHAPE_CHECK-TEXT
            # pulses on top.
            visible = {"Background", "Body", "Czas-TEXT", "Czas-NUMBER"}
            if now < self.check_until:
                visible.add("SHAPE_CHECK-TEXT")
            return sorted(visible)
        visible = {"Background", "Body", "Czas-TEXT", "Czas-NUMBER"}
        if self.mode == "manual":
            visible.add("Tryb_manualny")
        else:
            visible.add("Tryb_automatyczny")
            if self.auto_profile:
                visible.add(f"Tryb_automatyczny-{self.auto_profile}")
        if self.shoulders_on:
            visible.add("Ramiona")
        if self.forearms_on:
            visible.add("Przedramiona")
        if self.legs_on:
            visible.add("Nogi")
        if self.buttocks_on:
            visible.add("Masaz_Posladkow")
        if self.foot_massage_on:
            visible.add("Masaz_Stop")
        if self.heat_on:
            visible.add("Ogrzewanie")
        if self.neck_on:
            visible.update({"Szyja", "? Szyja"})
        if self.back_waist_on:
            visible.update({"Plecy_i_talia", "? Plecy_i_talia"})
        if self._show_intensity_ui_locked():
            visible.add("Sila_nacisku-TEXT")
            visible.add(f"Sila_nacisku-LVL{self.intensity_level}")
        if self._show_speed_ui_locked():
            visible.add("PredkoscTEXT")
            visible.add(f"Predkosc-LVL{self._effective_speed_level_locked()}")
        if self._show_foot_speed_ui_locked():
            visible.add("Predkosc_masazu_stop")
            visible.add(f"Predkosc_masazu_stop-LVL{self.foot_speed_level}")
        if now < self.check_until:
            visible.add("SHAPE_CHECK-TEXT")
        return sorted(self._expand_cumulative_level_layers_locked(visible))

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self._tick_locked()
            now = time.monotonic()
            frame_age_ms = None
            frame_stale = True
            if self.frame_seen_at is not None:
                frame_age_ms = max(0, int((time.time() - self.frame_seen_at) * 1000))
            if self.frame_seen_monotonic is not None:
                frame_stale = (
                    now - self.frame_seen_monotonic > config.FRAME_STALE_SECONDS
                )
            flash_active = {
                command: True
                for command, deadline in self.flash_command_until.items()
                if deadline > now
            }
            failed_active = {
                command: True
                for command, deadline in self.failed_command_until.items()
                if deadline > now
            }
            self.flash_command_until = {
                command: deadline
                for command, deadline in self.flash_command_until.items()
                if deadline > now
            }
            self.failed_command_until = {
                command: deadline
                for command, deadline in self.failed_command_until.items()
                if deadline > now
            }
            active_buttons = {command: False for command in BUTTON_ORDER}
            active_buttons["power"] = self.power_on
            active_buttons["ogrzewanie"] = self.heat_on
            active_buttons["pauza"] = self.paused
            active_buttons["masaz_stop"] = self.foot_massage_on
            active_buttons["masaz_posladkow"] = self.buttocks_on
            active_buttons["masaz_calego_ciala"] = self.full_body_on
            active_buttons["ramiona"] = self.shoulders_on
            active_buttons["przedramiona"] = self.forearms_on
            active_buttons["nogi"] = self.legs_on
            active_buttons["plecy_i_talia"] = self.back_waist_on
            active_buttons["szyja"] = self.neck_on
            active_buttons["tryb_automatyczny"] = self.mode == "auto"
            for command in flash_active:
                active_buttons[command] = True
            hold_active = (
                self.hold_command is not None and self.hold_stop_requested_at is None
            )
            if hold_active and self.hold_command in active_buttons:
                active_buttons[self.hold_command] = True
            blocked_buttons = {
                command: bool(self._blocked_reason_locked(command))
                for command in BUTTON_ORDER
            }
            effective_speed_level = self._effective_speed_level_locked()
            boot_settling = self._boot_settling_locked()
            diagnostics = self._timer_diagnostics_locked(now, effective_speed_level)
            return {
                "connected": self.connected,
                "port_name": self.port_name,
                "listening": self.listening,
                "board_ready": self.board_ready,
                "last_error": self.last_error,
                "last_command": self.last_command,
                "power_on": self.power_on,
                "mode": self.mode,
                "auto_profile": self.auto_profile,
                "timer_minutes": self.timer_minutes,
                "remaining_seconds": self.remaining_seconds,
                "time_text": self._current_time_text_locked(),
                "boot_settling": boot_settling,
                "controls_ready": self.power_on and not boot_settling,
                "hold": {
                    "active": hold_active,
                    "command": self.hold_command,
                    "hold_id": self.hold_id,
                    "started_at": self.hold_started_at,
                    "stop_requested": self.hold_stop_requested_at is not None,
                },
                "levels": {
                    "intensity": self.intensity_level,
                    "speed": effective_speed_level,
                    "foot_speed": self.foot_speed_level,
                },
                "raw_frame": self.raw_frame,
                "frame_signature": self.frame_signature,
                "bytes_3_to_6": self.bytes_3_to_6,
                "full_frame_tail": self.full_frame_tail,
                "frame_seen_at": self.frame_seen_at,
                "frame_age_ms": frame_age_ms,
                "frame_stale": frame_stale,
                "drift": list(self.drift),
                "failed_commands": list(self.failed_commands),
                "unverified_commands": list(self.unverified_commands),
                "layers": {
                    "visible": self._visible_layers_locked(),
                    "text": {"Czas-NUMBER": self._current_time_text_locked()},
                },
                "sync": {
                    "frame_live": bool(
                        frame_age_ms is not None and frame_age_ms < 3000
                    ),
                    "time_source": "model",
                    "levels_source": (
                        "hybrid-frame-model" if frame_age_ms is not None else "model"
                    ),
                    "zones_source": "model",
                },
                "direction": {
                    "back_known": self.back_direction_known,
                    "neck_known": self.neck_direction_known,
                    "back_cycle": (
                        self.back_forward_cycle_1 if self.back_direction_known else None
                    ),
                    "neck_cycle": (
                        self.back_forward_cycle_2 if self.neck_direction_known else None
                    ),
                },
                "diagnostics": diagnostics,
                "buttons": {
                    command: {
                        "active": active_buttons[command],
                        "blocked": blocked_buttons[command],
                        "failed": bool(failed_active.get(command)),
                        "label": COMMAND_INDEX[command].label,
                    }
                    for command in BUTTON_ORDER
                },
                "zones": {
                    "ramiona": self.shoulders_on,
                    "przedramiona": self.forearms_on,
                    "nogi": self.legs_on,
                    "masaz_posladkow": self.buttocks_on,
                    "masaz_stop": self.foot_massage_on,
                    "szyja": self.neck_on,
                    "plecy_i_talia": self.back_waist_on,
                    "ogrzewanie": self.heat_on,
                },
                "command_history": list(self.command_history),
                "backend_log": list(self.backend_log),
                "poll_hint_ms": config.STATE_POLL_HINT_MS,
            }
