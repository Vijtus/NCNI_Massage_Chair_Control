from __future__ import annotations

import re

from . import config

FRAME_BYTE_RE = re.compile(r"RX:\s+0x([0-9A-Fa-f]{2})")
FRAME_LINE_RE = re.compile(r"^FRAME\s+([0-9A-Fa-f]{66})$")


class FullFrameParser:
    def __init__(self, frame_length: int = config.FULL_FRAME_LENGTH) -> None:
        self.frame_length = frame_length
        self.buffer: list[int] = []

    def reset(self) -> None:
        self.buffer = []

    def feed(self, value: int) -> list[list[int]]:
        self.buffer.append(value & 0xFF)
        parsed: list[list[int]] = []

        while len(self.buffer) >= 2:
            start = self._find_header()
            if start < 0:
                self.buffer = self.buffer[-1:] if self.buffer[-1] == 0xAA else []
                break
            if start:
                del self.buffer[:start]
            if len(self.buffer) < self.frame_length:
                break

            candidate = self.buffer[: self.frame_length]
            if candidate[:2] == [0xAA, 0x55] and candidate[-4:] == config.FRAME_TRAILER:
                parsed.append(candidate)
                del self.buffer[: self.frame_length]
                continue

            del self.buffer[0]

        return parsed

    def _find_header(self) -> int:
        for index in range(0, len(self.buffer) - 1):
            if self.buffer[index] == 0xAA and self.buffer[index + 1] == 0x55:
                return index
        return -1
