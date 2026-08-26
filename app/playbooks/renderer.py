"""Flat {{token}} substitution — no loops, no conditionals. Any tabular
content (e.g. an ageing summary) is built by the caller into a single HTML
string and passed in as one token, never assembled inside the template."""

from __future__ import annotations

import re

_TOKEN_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


class MissingTokenError(Exception):
    pass


def render(template: str, context: dict) -> str:
    def replace(match: re.Match) -> str:
        key = match.group(1)
        if key not in context:
            raise MissingTokenError(f"missing token '{{{{{key}}}}}' in template")
        return str(context[key])

    return _TOKEN_RE.sub(replace, template)


def resolve_recipient(rule: str, case_context: dict) -> str:
    """Static value, a case-context field name, or a role keyword."""
    if rule in case_context:
        return str(case_context[rule])
    return rule
