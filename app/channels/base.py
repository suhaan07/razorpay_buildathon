from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ChannelResult:
    status: str  # "sent" | "logged" | "failed"
    detail: str


class Channel:
    """Channel-agnostic dispatch interface. A new channel implementation
    only needs to satisfy send() — nothing in the playbook engine or the
    decision layer changes when one is added or swapped."""

    name: str = "base"

    def send(self, *, to: str, cc: str | None, subject: str, body: str, html: str | None = None) -> ChannelResult:
        raise NotImplementedError
