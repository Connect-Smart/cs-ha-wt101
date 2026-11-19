"""Decode raw WT101 uplink payloads (user guide 6.1 / 6.2).

Used for TTN hubs, where HA reads the raw ``frm_payload`` from the TTN
Storage Integration instead of relying on a payload formatter. The output
uses the same keys as the official Milesight decoder, with the status fields
as their numeric codes (the sensor entities map codes and strings alike).

Pure Python, no Home Assistant imports.
"""
from __future__ import annotations

import struct
from typing import Any

# (channel, type) -> (key, struct format, scale)
_FIELDS: dict[tuple[int, int], tuple[str, str, float]] = {
    (0x01, 0x75): ("battery", "<B", 1),
    (0x03, 0x67): ("temperature", "<h", 10),
    (0x04, 0x67): ("target_temperature", "<h", 10),
    (0x05, 0x92): ("valve_opening", "<B", 1),
    (0x06, 0x00): ("tamper_status", "<B", 1),
    (0x07, 0x00): ("window_detection", "<B", 1),
    (0x08, 0xE5): ("motor_calibration_result", "<B", 1),
    (0x09, 0x90): ("motor_stroke", "<H", 1),
    (0x0A, 0x00): ("freeze_protection", "<B", 1),
    (0x0B, 0x90): ("motor_position", "<H", 1),
}

# Basic information (ff xx) sent after joining: only skipped, by length.
_INFO_LENGTHS: dict[int, int] = {
    0x01: 1,  # protocol version
    0x09: 2,  # hardware version
    0x0A: 2,  # software version
    0x0B: 1,  # power on
    0x0F: 1,  # device class
    0x16: 8,  # serial number
    0xFF: 2,  # TSL version
}


def decode_uplink(payload: bytes) -> dict[str, Any]:
    """Decode the channels this integration uses.

    Parsing stops at the first unknown channel; everything decoded before it
    is returned (an unknown channel's length cannot be known).
    """
    out: dict[str, Any] = {}
    i = 0
    while i + 2 <= len(payload):
        channel, kind = payload[i], payload[i + 1]
        i += 2
        field = _FIELDS.get((channel, kind))
        if field is not None:
            key, fmt, scale = field
            size = struct.calcsize(fmt)
            if i + size > len(payload):
                break
            (value,) = struct.unpack_from(fmt, payload, i)
            out[key] = value / scale if scale != 1 else value
            i += size
            continue
        if channel == 0xFF and kind in _INFO_LENGTHS:
            i += _INFO_LENGTHS[kind]
            continue
        break
    return out
