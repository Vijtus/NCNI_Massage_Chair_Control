from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandDef:
    key: str
    code: int
    label: str
    group: str


COMMANDS = [
    CommandDef("power", 0x01, "Power", "system"),
    CommandDef("predkosc_masazu_stop", 0x02, "Prędkość masażu stóp", "scalar"),
    CommandDef("ogrzewanie", 0x03, "Ogrzewanie", "toggle"),
    CommandDef("masaz_calego_ciala", 0x04, "Masaż całego ciała", "toggle"),
    CommandDef("tryb_automatyczny", 0x05, "Tryb automatyczny", "mode"),
    CommandDef("oparcie_w_dol", 0x06, "Oparcie w dół", "momentary"),
    CommandDef("czas", 0x07, "Czas", "scalar"),
    CommandDef("grawitacja_zero", 0x08, "Grawitacja zero", "momentary"),
    CommandDef("oparcie_w_gore", 0x09, "Oparcie w górę", "momentary"),
    CommandDef("pauza", 0x0B, "Pauza", "toggle"),
    CommandDef("masaz_stop", 0x0D, "Masaż stóp", "toggle"),
    CommandDef("masaz_posladkow", 0x0E, "Masaż pośladków", "toggle"),
    CommandDef("sila_nacisku_minus", 0x0F, "Siła nacisku -", "scalar"),
    CommandDef("sila_nacisku_plus", 0x10, "Siła nacisku +", "scalar"),
    CommandDef("nogi", 0x11, "Nogi", "toggle"),
    CommandDef("przedramiona", 0x12, "Przedramiona", "toggle"),
    CommandDef("ramiona", 0x13, "Ramiona", "toggle"),
    CommandDef("predkosc_minus", 0x14, "Prędkość -", "scalar"),
    CommandDef("predkosc_plus", 0x15, "Prędkość +", "scalar"),
    CommandDef("do_przodu_do_tylu_1", 0x16, "Do przodu / Do tyłu 1", "momentary"),
    CommandDef("plecy_i_talia", 0x17, "Plecy i talia", "toggle"),
    CommandDef("do_przodu_do_tylu_2", 0x18, "Do przodu / Do tyłu 2", "momentary"),
    CommandDef("szyja", 0x19, "Szyja", "toggle"),
]

COMMAND_INDEX = {command.key: command for command in COMMANDS}
BUTTON_ORDER = [command.key for command in COMMANDS]
CODE_TO_COMMAND = {command.code: command.key for command in COMMANDS}

# Current chair commands are button presses, not idempotent set-state requests.
# Retrying a toggle/cycle press can reverse a successful press that the frame
# has not reported yet, so commands must be explicitly proven safe before they
# get automatic resend.
RETRY_SAFE_COMMANDS: frozenset[str] = frozenset()

HOLD_TO_ADJUST_COMMANDS: frozenset[str] = frozenset(
    {
        "oparcie_w_gore",
        "oparcie_w_dol",
    }
)

TIME_OPTIONS_MINUTES = [15, 20, 25, 30]
AUTO_PROFILE_SEQUENCE = ["B", "C", "D", "A"]

AUTO_DEAD_COMMANDS = {
    "pauza",
    "predkosc_plus",
    "predkosc_minus",
    "do_przodu_do_tylu_1",
    "do_przodu_do_tylu_2",
}

# Commands that must be rejected during the chair's boot-settle window.
# Pressing these immediately after a local power-on press desynced the
# bridge's optimistic A/b prompt counter from the hardware LCD, because
# the chair's own state machine had not yet committed to a stable
# default-A. Blocking them until the chair reports stable running
# frames is the safe alternative to a parallel queue.
BOOT_SETTLE_BLOCKED_COMMANDS: frozenset[str] = frozenset(
    {
        "szyja",
        "plecy_i_talia",
        "do_przodu_do_tylu_1",
        "do_przodu_do_tylu_2",
        "predkosc_plus",
        "predkosc_minus",
        "sila_nacisku_plus",
        "sila_nacisku_minus",
        "predkosc_masazu_stop",
    }
)

MOMENTARY_COMMANDS = {
    "sila_nacisku_plus",
    "sila_nacisku_minus",
    "predkosc_plus",
    "predkosc_minus",
    "do_przodu_do_tylu_1",
    "do_przodu_do_tylu_2",
    "grawitacja_zero",
    "oparcie_w_gore",
    "oparcie_w_dol",
}

# Per spec (2026-04-28): the touch panel state machine is the source of truth
# for SVG visibility, since the bridge IS the touch panel. The chair frame is
# only authoritative for power-off (all-zero payload), auto-profile tail, mode
# signature, and intensity bucket. Every command applies optimistically to the
# model. No command is "frame observed" anymore.
FRAME_OBSERVED_COMMANDS: frozenset[str] = frozenset()

# These commands intentionally have no reliable frame field for their display
# state. The bridge is acting as the touch panel, so after ACK/DONE and a fresh
# post-DONE frame, the model is considered confirmed enough and should not leave
# a user-visible "unverified" warning behind.
#
# `predkosc_masazu_stop` is a deliberate safety net: byte 7 is normally the
# foot-speed bucket (0x00=1, 0x0C=2, 0x0F=3) and verification is still strict
# when byte 7 holds one of those known values. But protocol matrix Section 1
# notes "produced no stable frame change" for this command in auto scenarios,
# and live captures on 2026-04-30 showed transient byte-7 values where the UI
# and hardware were already correct yet the verifier returned UNKNOWN. Marking
# it model-only means the auto-pass path runs only when *no* frame field could
# be checked; a real disagree (e.g. b7=0x0C while model expects 3) still surfaces
# to `failed_commands`.
MODEL_ONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "ramiona",
        "przedramiona",
        "nogi",
        "masaz_calego_ciala",
        "masaz_posladkow",
        "masaz_stop",
        "ogrzewanie",
        "pauza",
        "czas",
        "predkosc_masazu_stop",
    }
)

ALL_MODEL_FIELDS = {
    "power_on",
    "mode",
    "auto_profile",
    "timer_minutes",
    "remaining_seconds",
    "paused",
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
}

ZONE_FIELDS = {
    "shoulders_on",
    "forearms_on",
    "legs_on",
    "buttocks_on",
    "foot_massage_on",
    "neck_on",
    "back_waist_on",
    "heat_on",
    "full_body_on",
}

COMMAND_MUTE_FIELDS = {
    "power": ALL_MODEL_FIELDS,
    "ramiona": {"shoulders_on", "full_body_on"},
    "przedramiona": {"forearms_on", "full_body_on"},
    "nogi": {"legs_on", "full_body_on"},
    "masaz_calego_ciala": {"shoulders_on", "forearms_on", "legs_on", "full_body_on"},
    "masaz_posladkow": {"buttocks_on"},
    "masaz_stop": {"foot_massage_on"},
    "ogrzewanie": {"heat_on"},
    "plecy_i_talia": {"mode", "back_waist_on", "neck_on"},
    "szyja": {"mode", "neck_on", "back_waist_on"},
    "tryb_automatyczny": {"mode", "auto_profile"} | ZONE_FIELDS,
    "pauza": {"paused"},
    "czas": {"timer_minutes", "remaining_seconds"},
    "sila_nacisku_plus": {"intensity_level"},
    "sila_nacisku_minus": {"intensity_level"},
    "predkosc_plus": {"speed_level"},
    "predkosc_minus": {"speed_level"},
    "predkosc_masazu_stop": {"foot_speed_level"},
    "do_przodu_do_tylu_1": set(),
    "do_przodu_do_tylu_2": set(),
    # Overlay flashes the chair frame to all-zero mode bytes for ~1s. Mute
    # power_on so the all-zero payload check in _sync_from_frame_locked does
    # not mistake the overlay for an actual power-off.
    "grawitacja_zero": {"power_on"},
    "oparcie_w_gore": {"power_on"},
    "oparcie_w_dol": {"power_on"},
}
