from __future__ import annotations

PLAYBOOK_SCHEMA = {
    "type": "object",
    "required": ["name", "version", "type", "levels", "onExhausted"],
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "version": {"type": "integer", "minimum": 1},
        "type": {"type": "string", "enum": ["escalation", "follow_up", "notification"]},
        "levels": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["channel", "recipients", "message"],
                "additionalProperties": False,
                "properties": {
                    "channel": {"type": "string", "enum": ["email", "voice", "log"]},
                    "recipients": {"type": "string", "minLength": 1},
                    "cc": {"type": "string", "description": "comma-separated resolver roles, e.g. 'manager,spoc'"},
                    "message": {"type": "string", "minLength": 1},
                },
            },
        },
        "onExhausted": {
            "type": "object",
            "required": ["status"],
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string", "minLength": 1},
            },
        },
    },
}
