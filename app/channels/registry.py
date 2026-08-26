from __future__ import annotations

from app.channels.base import Channel
from app.channels.email_channel import EmailChannel
from app.channels.log_channel import LogChannel
from app.channels.voice import VoiceChannel

_CHANNELS: dict[str, Channel] = {
    "email": EmailChannel(),
    "voice": VoiceChannel(),
    "log": LogChannel(),
}


def get_channel(name: str) -> Channel:
    if name not in _CHANNELS:
        raise KeyError(f"unknown channel: {name!r}")
    return _CHANNELS[name]
