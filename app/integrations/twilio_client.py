from __future__ import annotations

import os

_client = None


def is_configured() -> bool:
    return bool(os.getenv("TWILIO_ACCOUNT_SID") and os.getenv("TWILIO_AUTH_TOKEN"))


def get_client():
    global _client
    if _client is None:
        from twilio.rest import Client

        _client = Client(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    return _client


def validate_request_signature(url: str, params: dict, signature: str) -> bool:
    from twilio.request_validator import RequestValidator

    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not token:
        return False
    return RequestValidator(token).validate(url, params, signature)
