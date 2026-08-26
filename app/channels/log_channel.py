from __future__ import annotations

import logging

from app.channels.base import Channel, ChannelResult

logger = logging.getLogger("recovery.channels.log")


class LogChannel(Channel):
    """Dev/demo channel — every other channel falls back to this when its
    credentials aren't configured (NFR-7), so the full engine is runnable
    end-to-end without any live Twilio/SendGrid account."""

    name = "log"

    def send(self, *, to: str, cc: str | None, subject: str, body: str, html: str | None = None) -> ChannelResult:
        logger.info("[log-channel] to=%s cc=%s subject=%r body=%r", to, cc, subject, body)
        return ChannelResult(status="logged", detail=f"logged to={to}")
