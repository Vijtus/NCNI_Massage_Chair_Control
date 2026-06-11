from __future__ import annotations

# Transcribed from user bench observation on 2026-04-29.

PROFILE_A_SPEED_TIMELINE = [
    # (elapsed_seconds, speed_level)
    (0, 2),
    (8, 3),
    (76, 1),
    (79, 3),
    (82, 1),
    (85, 3),
    (94, 1),
    (97, 3),
    (100, 1),
    (103, 3),
    (118, 1),
    (121, 3),
    (140, 2),
    (150, 3),
    (224, 1),
    (227, 3),
    (237, 1),
    (240, 3),
    (243, 1),
    (246, 3),
    (260, 1),
    (263, 3),
    (283, 2),
    (292, 3),
    (316, 1),
    (318, 3),
    (328, 1),
    (331, 3),
    (334, 1),
    (337, 3),
    (351, 1),
    (354, 3),
    (374, 2),
    (384, 3),
    (458, 1),
    (461, 3),
    (470, 1),
    (473, 3),
    (476, 1),
    (479, 3),
    (494, 1),
    (497, 3),
    (516, 2),
    (526, 3),
    (600, 1),
    (603, 3),
    (613, 1),
    (616, 3),
    (619, 1),
    (621, 3),
    (636, 1),
    (639, 3),
    (659, 2),
    (668, 3),
    (742, 1),
    (745, 3),
    (755, 1),
    (758, 3),
    (761, 1),
    (764, 3),
    (778, 1),
    (781, 3),
    (801, 2),
    (810, 3),
    (827, None),  # TIME OVER, profile ends, chair powers off
]

PROFILE_BCD_SHARED_SPEED_TIMELINE = [
    # Same as A through 02:30, then diverges. B/C/D share these transitions
    # through 13:52, but their observed end behavior differs.
    (0, 2),
    (8, 3),
    (76, 1),
    (79, 3),
    (82, 1),
    (85, 3),
    (94, 1),
    (97, 3),
    (100, 1),
    (103, 3),
    (118, 1),
    (121, 3),
    (140, 2),
    (150, 3),
    (218, 1),
    (222, 3),
    (225, 1),
    (227, 3),
    (237, 1),
    (240, 3),
    (243, 1),
    (246, 3),
    (260, 1),
    (263, 3),
    (283, 2),
    (292, 3),
    (361, 1),
    (364, 3),
    (366, 1),
    (369, 3),
    (379, 1),
    (382, 3),
    (385, 1),
    (388, 3),
    (403, 1),
    (405, 3),
    (425, 2),
    (435, 3),
    (503, 1),
    (506, 3),
    (509, 1),
    (512, 3),
    (521, 1),
    (524, 3),
    (527, 1),
    (530, 3),
    (545, 1),
    (548, 3),
    (567, 2),
    (577, 3),
    (645, 1),
    (648, 3),
    (651, 1),
    (654, 3),
    (664, 1),
    (667, 3),
    (669, 1),
    (673, 3),
    (687, 1),
    (690, 3),
    (710, 2),
    (719, 3),
    (788, 1),
    (791, 3),
    (793, 1),
    (796, 3),
    (806, 1),
    (809, 3),
    (812, 1),
    (815, 3),
    (829, 1),
    (832, 3),
]

PROFILE_B_SPEED_TIMELINE = PROFILE_BCD_SHARED_SPEED_TIMELINE + [
    (833, None),  # TIME OVER
]

PROFILE_C_SPEED_TIMELINE = PROFILE_BCD_SHARED_SPEED_TIMELINE + [
    (838, None),  # TIME OVER
]

PROFILE_D_SPEED_TIMELINE = PROFILE_BCD_SHARED_SPEED_TIMELINE + [
    (852, 2),
    (855, None),  # TIME OVER
]


def speed_at_elapsed(profile: str, elapsed_seconds: int) -> int | None:
    """Return speed level 1..3, or None after the chair's auto program ends."""
    if profile == "A":
        table = PROFILE_A_SPEED_TIMELINE
    elif profile == "B":
        table = PROFILE_B_SPEED_TIMELINE
    elif profile == "C":
        table = PROFILE_C_SPEED_TIMELINE
    else:
        table = PROFILE_D_SPEED_TIMELINE
    current = 2
    for ts, level in table:
        if elapsed_seconds < ts:
            return current
        if level is None:
            return None
        current = level
    return current
