from __future__ import annotations

import logging
import os

from app.channels.base import Channel, ChannelResult
from app.channels.log_channel import LogChannel
from app.integrations import twilio_client

logger = logging.getLogger("recovery.channels.voice")


class VoiceChannel(Channel):
    """Scripted TTS call via Twilio Voice + <Say voice="Polly.Aditi">
    (see /voice/twiml/{case_id}). A future conversational agent only needs
    to replace this implementation — nothing upstream changes."""

    name = "voice"

    def send(self, *, to: str, cc: str | None, subject: str, body: str, html: str | None = None) -> ChannelResult:
        if not twilio_client.is_configured():
            return LogChannel().send(to=to, cc=cc, subject=subject, body=body, html=html)

        try:
            client = twilio_client.get_client()
            from_number = os.getenv("TWILIO_VOICE_FROM")
            twiml = f'<Response><Say voice="Polly.Aditi">{body}</Say></Response>'
            call = client.calls.create(to=to, from_=from_number, twiml=twiml)
            return ChannelResult(status="sent", detail=f"twilio call sid={call.sid}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Voice call failed")
            return ChannelResult(status="failed", detail=str(exc))
