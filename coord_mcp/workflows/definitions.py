"""Workflow definitions: one JSON file per workflow under defs/.

JSON, not Python, because a definition is data that master, Ben and the
dashboard all read: it cannot run code, it diffs cleanly, and this loader
checks every file against the rules below, so a bad edit fails a test rather
than an engine pass.

A definition is ordered steps. Each step says:
  key, name   stable id and the label /workflows shows
  who         agent | code | ben: who does the step today
  fallback    optional, on a code step only: "ben" when the step is Ben's
              whenever the code rule is off or declines (check-in
              auto-approval, checkin_auto.py). Such a step counts as Ben's
              while he has a loose end open on the case.
  kinds       task kinds that carry the step on the board (empty for a step
              no task carries: Ben's, or one code does without a task)
  match       optional {"spec_has": field}: the task's spec must name it
              (a CVC check-in is a code_change whose spec has checkin_of)
  gate        what must hold before the step counts
  next        what starts the next step
  passive     optional, true on a Ben step where he only reads or uses the
              result: its loose ends close themselves (passive.py)
  sla         optional time limit at the step: {"business_hours": n} or
              {"business_days": n} (exactly one, n > 0), plus an optional
              "why". Business time skips Saturdays and Sundays (sla.py).
              Past it, the minder lists the case (sla.minder_owed).
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
  auto        optional, on a Ben step only: {"rule": <AUTO_RULES>, "fallback":
              "ben"}. Code settles the step when the rule holds; otherwise it
              stays Ben's, as `who` says. estimate_band: a sizing inside the
              band closes his approval item (coord_mcp/estimate.py).
  optional    optional true: cases often go past the step without it, so
              the case-step check (checks.py) does not warn when it is
              skipped. Set by an adopted drift proposal (propose.py).

A definition may also carry `tag` {title_re, kinds?, sources?}: the rule
that puts a root task (or an intent, by its summary) in the workflow when no
built-in rule in tag.py does. Adopted proposals for new workflows bring one.
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
AUTO_RULES = ("estimate_band",)
# The only kinds the engine may create. A draft_reply is a draft (nothing
# sends); a code_change it creates is CVC-bound and dev-compile only; a
# check-in, a promotion or a tracker post is never in reach.
ENGINE_KINDS = ("investigation", "data_pull", "draft_reply", "code_change", "deliverable")


SLA_UNITS = ("business_hours", "business_days")


def sla_errors(v: Any) -> list[str]:
    """Problems with one step's sla; empty when it is sound."""
    if not isinstance(v, dict):
        return ["sla must be an object"]
    units = [k for k in v if k in SLA_UNITS]
    extra = [k for k in v if k not in SLA_UNITS and k != "why"]
    errs = []
    if len(units) != 1:
        errs.append(f"sla names exactly one of {SLA_UNITS}")
    for k in units:
        n = v[k]
        if isinstance(n, bool) or not isinstance(n, (int, float)) or n <= 0:
            errs.append(f"sla {k} must be a positive number")
    if extra:
        errs.append(f"sla has unknown fields {extra}")
    if "why" in v and not (isinstance(v["why"], str) and v["why"].strip()):
        errs.append("sla why must be text")
    return errs


def tag_errors(v: Any) -> list[str]:
    """Problems with a definition's tag rule; empty when it is sound."""
    if not isinstance(v, dict):
        return ["tag must be an object"]
    errs = []
    extra = set(v) - {"title_re", "kinds", "sources"}
    if extra:
        errs.append(f"tag has unknown fields {sorted(extra)}")
    try:
        re.compile(v.get("title_re") or "")
    except re.error as e:
        errs.append(f"tag title_re is not a regex ({e})")
    if not (v.get("title_re") or "").strip():
        errs.append("tag needs a title_re")
    for k in ("kinds", "sources"):
        if k in v and not (isinstance(v[k], list) and all(isinstance(x, str) for x in v[k])):
            errs.append(f"tag {k} must be a list of text")
    return errs


def validate(d: dict[str, Any], kinds: tuple[str, ...] | list[str] | None = None,
             catalog: dict[str, tuple[str, str]] | None = None) -> list[str]:
    """Problems with one definition; empty when it is sound. `catalog`
    defaults to CATALOG; a proposal for a new workflow passes one that also
    holds its own code (propose.py)."""
    if kinds is None:
        from ..contracts import CONTRACTS
        kinds = tuple(CONTRACTS)
    catalog = CATALOG if catalog is None else catalog
    errs: list[str] = []
    code = d.get("code")
    if code not in catalog:
        errs.append(f"code {code!r} is not in the catalog")
    elif d.get("name") != catalog[code][0]:
        errs.append(f"{code}: name {d.get('name')!r} differs from the catalog's {catalog[code][0]!r}")
    if "tag" in d:
        errs.extend(f"{code}: {e}" for e in tag_errors(d["tag"]))
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
        if "fallback" in st and (st.get("who") != "code" or st["fallback"] not in ("ben", "agent")):
            errs.append(f"{where}: fallback goes on a code step and names ben or agent")
        if "optional" in st and not isinstance(st["optional"], bool):
            errs.append(f"{where}: optional must be true or false")
        if "passive" in st and (st["passive"] is not True or st.get("who") != "ben"):
            errs.append(f"{where}: passive is true or absent, and only on a Ben step")
        if "auto" in st:
            au = st["auto"]
            if st.get("who") != "ben":
                errs.append(f"{where}: auto is for a Ben step (code settles it, Ben is the fallback)")
            if not isinstance(au, dict) or au.get("rule") not in AUTO_RULES:
                errs.append(f"{where}: auto.rule must be one of {AUTO_RULES}")
            elif au.get("fallback") != "ben":
                errs.append(f"{where}: auto.fallback must be \"ben\"")
        if "sla" in st:
            errs.extend(f"{where}: {e}" for e in sla_errors(st["sla"]))
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
def _load(path: str, stamp: float = 0.0) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(Path(path).glob("*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if f.stem != d.get("code"):
            raise ValueError(f"{f.name}: file name and code {d.get('code')!r} differ")
        out[d["code"]] = d
    return out


def load(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Every definition by code. Validated by tests, not here, so a bad file
    never stops the dashboard: get() falls back to an empty step list.

    Cached per directory mtime: an adopted proposal (propose.adopt) writes
    its file by rename, which moves the mtime, so every process sees it on
    its next read without a restart."""
    p = Path(path or DEFS_DIR)
    try:
        stamp = p.stat().st_mtime
    except OSError:
        stamp = 0.0
    return _load(str(p), stamp)


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
