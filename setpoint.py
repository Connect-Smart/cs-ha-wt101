"""Arbitrate the WT101 target temperature between HA and the knob.

LoRaWAN Class A makes every change slow to round-trip: a queued downlink is
only delivered on the device's next uplink, and the target reported in that
same uplink is still the old one. Naively mirroring both directions (HA →
device and device → HA) therefore ping-pongs between the two values.

This arbiter keeps the value HA asked for as *pending* until an uplink
confirms it and ignores stale reports meanwhile. A mismatch that remains once
the downlink must have been delivered is classified as either:

- a lost downlink (device still at the value from before) → retry, or
- a local change on the knob → apply the configured priority.

It holds no Home Assistant objects so the logic can be reasoned about (and
tested) on its own; the climate entity feeds it events and executes the
returned ``Action``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .const import PRIORITY_SOURCE, PRIORITY_SYNC

# The downlink rides the first uplink after it was queued; the second uplink
# is the first one that can report the new target.
_UPLINKS_TO_CONFIRM = 2
_MAX_RETRIES = 2


def quantize(value: float) -> float:
    """The WT101 stores its target as whole °C (INT8), same rounding as ff b1."""
    return float(int(round(value)))


@dataclass(frozen=True)
class Action:
    """What the caller should do after feeding the arbiter an event."""

    send: float | None = None  # queue ff b1 with this target
    retry: bool = False  # the send is a retry of the pending value
    write_source: float | None = None  # write this value into the source
    gave_up: float | None = None  # pending value abandoned after retries


NO_ACTION = Action()


class SetpointArbiter:
    """State machine for one thermostat's target temperature."""

    def __init__(self, priority: str, grace: timedelta) -> None:
        self.priority = priority
        self.grace = grace
        self.reported: float | None = None  # last target the device reported
        self.pending: float | None = None  # target queued, not yet confirmed
        self._before_send: float | None = None
        self._sent_at: datetime | None = None
        self._uplinks = 0
        self._retries = 0
        self._gave_up_on: float | None = None

    @property
    def target(self) -> float | None:
        """Target to show in HA: what we asked for, else what the device has."""
        return self.pending if self.pending is not None else self.reported

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------
    def request(self, value: float) -> Action:
        """HA (source entity or UI) wants the device at ``value``."""
        value = quantize(value)
        self._gave_up_on = None
        if self.pending is not None:
            return NO_ACTION if value == self.pending else Action(send=value)
        if value == self.reported:
            return NO_ACTION
        return Action(send=value)

    def mark_sent(self, value: float, now: datetime, *, retry: bool = False) -> None:
        """The downlink for ``value`` was queued successfully."""
        if self.pending is None:
            self._before_send = self.reported
        if not retry:
            self._retries = 0
        self.pending = quantize(value)
        self._sent_at = now
        self._uplinks = 0

    def forget_pending(self) -> None:
        """The queued downlink was flushed from the LNS (calibration)."""
        self._clear_pending()

    def on_uplink(self) -> None:
        """Any uplink from the device arrived (ChirpStack webhook only)."""
        if self.pending is not None:
            self._uplinks += 1

    def on_report(self, value: float, now: datetime, source: float | None) -> Action:
        """The device reported its current target."""
        previous = self.reported
        value = quantize(value)
        self.reported = value

        if self.pending is not None:
            if value == self.pending:
                self._clear_pending()
                return NO_ACTION
            if self._awaiting_delivery(now):
                return NO_ACTION  # stale: our downlink has not been applied yet
            return self._resolve(value, source)

        if previous is None:
            return self._initial_sync(source)
        if value != previous:
            return self._local_change(value, source)
        return self._enforce(source)

    def on_tick(self, now: datetime, source: float | None) -> Action:
        """Periodic check so a pending value cannot hang without uplinks."""
        if self.pending is None or self._awaiting_delivery(now):
            return NO_ACTION
        if self.reported is not None and self.reported != self._before_send:
            return self._resolve(self.reported, source)
        return self._retry_or_give_up()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _awaiting_delivery(self, now: datetime) -> bool:
        if self._sent_at is None:
            return False
        return self._uplinks < _UPLINKS_TO_CONFIRM and now - self._sent_at < self.grace

    def _resolve(self, value: float, source: float | None) -> Action:
        """Pending value not confirmed although it should have been delivered."""
        if value == self._before_send:
            return self._retry_or_give_up()
        # Device moved to a third value after our command: knob was turned.
        self._clear_pending()
        return self._local_change(value, source)

    def _retry_or_give_up(self) -> Action:
        if self._retries < _MAX_RETRIES:
            self._retries += 1
            return Action(send=self.pending, retry=True)
        abandoned = self.pending
        self._gave_up_on = abandoned
        self._clear_pending()
        return Action(gave_up=abandoned)

    def _initial_sync(self, source: float | None) -> Action:
        """First report since (re)start or reconfigure: align to the source.

        Applies in every priority — a knob change made while HA was down
        cannot be told apart from a stale value, and after configuring a
        source the device is expected to follow it right away.
        """
        if source is None:
            return NO_ACTION
        wanted = quantize(source)
        if wanted == self.reported:
            return NO_ACTION
        return Action(send=wanted)

    def _local_change(self, value: float, source: float | None) -> Action:
        """The knob (or another controller) changed the target on the device."""
        if self.priority == PRIORITY_SYNC:
            if source is not None and quantize(source) != value:
                return Action(write_source=value)
            return NO_ACTION
        return self._enforce(source)

    def _enforce(self, source: float | None) -> Action:
        """In source priority, restore the source value on the device."""
        if self.priority != PRIORITY_SOURCE or source is None:
            return NO_ACTION
        wanted = quantize(source)
        if wanted == self.reported or wanted == self._gave_up_on:
            return NO_ACTION
        return Action(send=wanted)

    def _clear_pending(self) -> None:
        self.pending = None
        self._sent_at = None
        self._uplinks = 0
        self._retries = 0
