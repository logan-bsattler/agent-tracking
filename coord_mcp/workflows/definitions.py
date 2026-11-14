"""Workflow definitions: one JSON file per workflow under defs/.

JSON, not Python, because a definition is data that master, Ben and the
dashboard all read: it cannot run code, it diffs cleanly, and this loader
checks every file against the rules below, so a bad edit fails a test rather
than an engine pass.

A definition is ordered steps. Each step says:
  key, name   stable id and the label /workflows shows
  who         agent | code | ben: who does the step today
  kinds       task kinds that carry the step on the board (empty for a step
              no task carries: Ben's, or one code does without a task)
  match       optional {"spec_has": field}: the task's spec must name it
              (a CVC check-in is a code_change whose spec has checkin_of)
  gate        what must hold before the step counts
  next        what starts the next step
  spawn       optional, for the engine (engine.py): how the step's task is
              created from the case when the step before it lands.
              on: "result" (the step before finished done) or
                  "client_follow_up" (it named a next step for the client)
              after: the step that must have landed (default: the one before)
              kind, title, spec: the task to create; {placeholders} filled
              takes_follow_up: regex; a client loose end it matches is
                  resolved as tasked by the new task
              cvc: true marks CVC-bound code (dev-compile only, no check-in)
              spec_defaults: spec fields set when the template lacks them
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import CATALOG

DEFS_DIR = Path(__file__).with_name("defs")
WHO = ("agent", "code", "ben")
SPAWN_ON = ("result", "client_follow_up")
# The only kinds the engine may create. A draft_reply is a draft (nothing
# sends); a code_change it creates is CVC-bound and dev-compile only; a
# check-in, a promotion or a tracker post is never in reach.
ENGINE_KINDS = ("investigation", "data_pull", "draft_reply", "code_change", "deliverable")


def validate(d: dict[str, Any], kinds: tuple[str, ...] | list[str] | None = None) -> list[str]:
    """Problems with one definition; empty when it is sound."""
    if kinds is None:
        from ..contracts import CONTRACTS
        kinds = tuple(CONTRACTS)
    errs: list[str] = []
    code = d.get("code")
    if code not in CATALOG:
        errs.append(f"code {code!r} is not in the catalog")
    elif d.get("name") != CATALOG[code][0]:
        errs.append(f"{code}: name {d.get('name')!r} differs from the catalog's {CATALOG[code][0]!r}")
    steps = d.get("steps") or []
    if not steps:
        errs.append(f"{code}: no steps")
    seen: list[str] = []
    for st in steps:
        k = st.get("key")
        where = f"{code}.{k}"
        if not k or k in seen:
            errs.append(f"{where}: missing or repeated key")
        if st.get("who") not in WHO:
            errs.append(f"{where}: who must be one of {WHO}")
        if st.get("who") == "agent" and not st.get("kinds"):
            errs.append(f"{where}: an agent step names the task kinds that carry it")
        for kd in st.get("kinds") or []:
            if kd not in kinds:
                errs.append(f"{where}: unknown task kind {kd!r}")
        for f in ("name", "gate", "next"):
            if not st.get(f):
                errs.append(f"{where}: no {f}")
        sp = st.get("spawn")
        if sp:
            if st.get("who") != "agent":
                errs.append(f"{where}: only an agent step may spawn; Ben's and code's steps never do")
            if sp.get("on") not in SPAWN_ON:
                errs.append(f"{where}: spawn.on must be one of {SPAWN_ON}")
            if sp.get("kind") not in ENGINE_KINDS or sp.get("kind") not in (st.get("kinds") or []):
                errs.append(f"{where}: spawn.kind must be one of the step's kinds and in {ENGINE_KINDS}")
            if sp.get("kind") == "code_change" and not sp.get("cvc"):
                errs.append(f"{where}: an engine code_change is CVC-bound; set cvc true")
            if (st.get("match") or {}).get("spec_has") == "checkin_of":
                errs.append(f"{where}: the engine never creates a check-in")
            after = sp.get("after")
            if after is not None and after not in seen:
                errs.append(f"{where}: spawn.after {after!r} is not an earlier step")
            if not seen:
                errs.append(f"{where}: the first step cannot spawn")
            if not isinstance(sp.get("spec"), dict) or not sp.get("title"):
                errs.append(f"{where}: spawn needs a title and a spec object")
            if sp.get("takes_follow_up"):
                try:
                    re.compile(sp["takes_follow_up"])
                except re.error as e:
                    errs.append(f"{where}: takes_follow_up is not a regex ({e})")
        seen.append(k)
    if d.get("engine") and not any(st.get("spawn") for st in steps):
        errs.append(f"{code}: engine is true but no step spawns")
    return errs


@lru_cache(maxsize=4)
def _load(path: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(Path(path).glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if f.stem != d.get("code"):
            raise ValueError(f"{f.name}: file name and code {d.get('code')!r} differ")
        out[d["code"]] = d
    return out


def load(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Every definition by code. Validated by tests, not here, so a bad file
    never stops the dashboard: get() falls back to an empty step list."""
    return _load(str(path or DEFS_DIR))


def get(code: str | None) -> dict[str, Any] | None:
    return load().get(code or "")


def step_index(d: dict[str, Any], key: str | None) -> int:
    for i, st in enumerate(d.get("steps") or []):
        if st["key"] == key:
            return i
    return -1


def step_for(d: dict[str, Any] | None, kind: str, spec: dict[str, Any] | None, after: int = -1) -> str | None:
    """The step a task of this kind and spec carries: the first matching step
    after `after` (its parent's step), else the parent's own step (a loop,
    such as a fix of a fix), else the first matching at all. None when no
    step of the workflow takes that kind."""
    if not d:
        return None
    spec = spec or {}

    def fits(st: dict[str, Any]) -> bool:
        if kind not in (st.get("kinds") or []):
            return False
        need = (st.get("match") or {}).get("spec_has")
        if need:
            return bool(spec.get(need))
        # A check-in is only ever the check-in step.
        return not spec.get("checkin_of") or not any(
            (s.get("match") or {}).get("spec_has") == "checkin_of" for s in d["steps"])

    steps = d.get("steps") or []
    for i in range(after + 1, len(steps)):
        if fits(steps[i]):
            return steps[i]["key"]
    if 0 <= after < len(steps) and fits(steps[after]):
        return steps[after]["key"]
    for st in steps:
        if fits(st):
            return st["key"]
    return None
