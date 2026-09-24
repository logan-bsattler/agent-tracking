"""Result contracts, one per task kind.

A contract is a dict of field name -> field spec. complete_task validates the
worker's result against it and rejects anything off-shape, so prose cannot
reach the lead's context. Adding a kind is a dict entry here; nothing else
changes.

Field spec keys:
    type      "str" | "int" | "bool" | "enum" | "list" | "dict"
    required  bool (default False)
    max       str: max characters; list: max items
    values    enum: allowed values
    items     list: field spec for each element
    fields    dict: nested contract
"""

from __future__ import annotations

import json
from typing import Any

CONTRACT_VERSION = 1

_FILE = {
    "path": {"type": "str", "max": 400, "required": True},
    "added": {"type": "int"},
    "removed": {"type": "int"},
}

_FINDING = {
    "severity": {"type": "enum", "values": ["blocker", "major", "minor", "nit"], "required": True},
    "file": {"type": "str", "max": 400, "required": True},
    "line": {"type": "int"},
    "note": {"type": "str", "max": 200, "required": True},
}

# Next steps and open questions, as data rather than prose. A finished task
# whose result only *mentions* remaining work leaves nothing on the board to
# say so; each item here becomes a follow_ups row the master must resolve.
_FOLLOW_UPS = {
    "type": "list", "max": 10,
    "items": {"type": "dict", "fields": {
        "who": {"type": "str", "max": 40, "required": True},
        "what": {"type": "str", "max": 200, "required": True},
    }},
}

CONTRACTS: dict[str, dict[str, dict[str, Any]]] = {
    "code_change": {
        "done": {"type": "bool", "required": True},
        "summary": {"type": "str", "max": 300, "required": True},
        "files_changed": {
            "type": "list", "max": 30, "required": True,
            "items": {"type": "dict", "fields": _FILE},
        },
        "tests": {
            "type": "dict",
            "fields": {
                "ran": {"type": "int", "required": True},
                "passed": {"type": "int", "required": True},
                "failed": {"type": "int", "required": True},
            },
        },
        "branch": {"type": "str", "max": 100},
        "notes": {"type": "str", "max": 300},
        "follow_ups": _FOLLOW_UPS,
    },
    "investigation": {
        "done": {"type": "bool", "required": True},
        "verdict": {"type": "str", "max": 200, "required": True},
        "confidence": {"type": "enum", "values": ["low", "medium", "high"], "required": True},
        "evidence": {
            "type": "list", "max": 10, "required": True,
            "items": {"type": "str", "max": 200},
        },
        "pointers": {"type": "list", "max": 20, "items": {"type": "str", "max": 400}},
        "follow_ups": _FOLLOW_UPS,
    },
    "review": {
        "done": {"type": "bool", "required": True},
        "verdict": {"type": "enum", "values": ["approve", "request_changes", "block"], "required": True},
        "summary": {"type": "str", "max": 300, "required": True},
        "findings": {"type": "list", "max": 20, "items": {"type": "dict", "fields": _FINDING}},
        "follow_ups": _FOLLOW_UPS,
    },
    "data_pull": {
        "done": {"type": "bool", "required": True},
        "output_path": {"type": "str", "max": 400, "required": True},
        "rows": {"type": "int", "required": True},
        "columns": {"type": "list", "max": 50, "items": {"type": "str", "max": 80}},
        "summary": {"type": "str", "max": 300},
        "follow_ups": _FOLLOW_UPS,
    },
}

# Any kind may report failure with this shape instead of its contract.
FAILURE: dict[str, dict[str, Any]] = {
    "done": {"type": "bool", "required": True},
    "reason": {"type": "str", "max": 200, "required": True},
    "retryable": {"type": "bool", "required": True},
}


def known_kinds() -> list[str]:
    return sorted(CONTRACTS)


class ValidationError(ValueError):
    def __init__(self, kind: str, errors: list[str]):
        self.kind, self.errors = kind, errors
        super().__init__("; ".join(errors))

    def render(self) -> str:
        """Everything the worker needs to fix and resend in one turn."""
        lines = [f"Result rejected for kind '{self.kind}'. Nothing was written."]
        lines += [f"  - {e}" for e in self.errors]
        lines.append("Expected shape:")
        lines.append(json.dumps(expected_shape(self.kind), indent=2))
        lines.append("Or, to report failure: " + json.dumps(_shape(FAILURE)))
        lines.append(
            "Caps are per field, in characters. Do not move overflow into another "
            "field; put long output in a file and reference its path."
        )
        return "\n".join(lines)


def _describe(spec: dict[str, Any]) -> Any:
    t = spec["type"]
    if t == "str":
        return f"str<={spec['max']}" if "max" in spec else "str"
    if t == "enum":
        return "enum " + "|".join(spec["values"])
    if t == "list":
        inner = _describe(spec["items"]) if "items" in spec else "any"
        return [f"<={spec.get('max', '?')} items of", inner]
    if t == "dict":
        return _shape(spec["fields"])
    return t


def _shape(contract: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {(k if v.get("required") else f"{k}?"): _describe(v) for k, v in contract.items()}


def expected_shape(kind: str) -> dict[str, Any]:
    """Human-readable contract. A '?' suffix marks optional fields."""
    if kind not in CONTRACTS:
        raise ValueError(f"unknown task kind '{kind}'. Known kinds: {known_kinds()}")
    return _shape(CONTRACTS[kind])


def _check(path: str, spec: dict[str, Any], val: Any, errors: list[str]) -> Any:
    t = spec["type"]
    if t == "bool":
        if not isinstance(val, bool):
            errors.append(f"{path}: expected bool, got {type(val).__name__}")
        return val
    if t == "int":
        if not isinstance(val, int) or isinstance(val, bool):
            errors.append(f"{path}: expected int, got {type(val).__name__}")
        return val
    if t == "str":
        if not isinstance(val, str):
            errors.append(f"{path}: expected str, got {type(val).__name__}")
        elif "max" in spec and len(val) > spec["max"]:
            errors.append(f"{path}: {len(val)} chars, cap is {spec['max']}")
        return val
    if t == "enum":
        if val not in spec["values"]:
            errors.append(f"{path}: {val!r} is not one of {spec['values']}")
        return val
    if t == "list":
        if not isinstance(val, list):
            errors.append(f"{path}: expected list, got {type(val).__name__}")
            return val
        if "max" in spec and len(val) > spec["max"]:
            errors.append(f"{path}: {len(val)} items, cap is {spec['max']}")
        if "items" in spec:
            return [_check(f"{path}[{i}]", spec["items"], v, errors) for i, v in enumerate(val)]
        return val
    if t == "dict":
        if not isinstance(val, dict):
            errors.append(f"{path}: expected object, got {type(val).__name__}")
            return val
        return _check_fields(f"{path}.", spec["fields"], val, errors)
    errors.append(f"{path}: contract bug, unknown type {t}")
    return val


def _check_fields(
    prefix: str, contract: dict[str, dict[str, Any]], obj: dict[str, Any], errors: list[str]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, spec in contract.items():
        if k in obj:
            out[k] = _check(f"{prefix}{k}", spec, obj[k], errors)
        elif spec.get("required"):
            errors.append(f"{prefix}{k}: required")
    extra = sorted(set(obj) - set(contract))
    if extra:
        errors.append(f"{prefix or 'result'}: unexpected field(s) {extra}, not in the contract")
    return out


def validate(kind: str, result: dict[str, Any]) -> dict[str, Any]:
    """Return the validated result or raise ValidationError listing every problem."""
    if kind not in CONTRACTS:
        raise ValueError(f"unknown task kind '{kind}'. Known kinds: {known_kinds()}")
    if not isinstance(result, dict):
        raise ValidationError(kind, ["result must be an object"])
    contract = FAILURE if result.get("done") is False else CONTRACTS[kind]
    errors: list[str] = []
    out = _check_fields("", contract, result, errors)
    if errors:
        raise ValidationError(kind, errors)
    return out
