"""Loads and schema-validates every *.json file in app/playbooks/configs/.
Dropping in a new schema-valid file registers it automatically — no code
change required (FR-11)."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import ValidationError, validate

from app.playbooks.schema import PLAYBOOK_SCHEMA

CONFIGS_DIR = Path(__file__).parent / "configs"


class PlaybookLoadError(Exception):
    pass


def load_playbooks(configs_dir: Path = CONFIGS_DIR) -> dict[str, dict]:
    registry: dict[str, dict] = {}
    for path in sorted(configs_dir.glob("*.json")):
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        try:
            validate(instance=data, schema=PLAYBOOK_SCHEMA)
        except ValidationError as exc:
            field = "/".join(str(p) for p in exc.absolute_path) or "<root>"
            raise PlaybookLoadError(f"{path.name}: invalid field '{field}': {exc.message}") from exc
        registry[data["name"]] = data
    return registry


_REGISTRY: dict[str, dict] | None = None


def get_registry() -> dict[str, dict]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = load_playbooks()
    return _REGISTRY


def get_playbook(name: str) -> dict:
    registry = get_registry()
    if name not in registry:
        raise KeyError(f"unknown playbook: {name!r}")
    return registry[name]
