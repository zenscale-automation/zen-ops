"""Admin API — the config API in the operator's language.

`config_api` is correct and complete, and nobody outside engineering can use it. To move
the supervisor's call from twenty minutes to twenty-five you must currently hand-write

    PATCH /api/config/escalation
    {"ladders": {"weaving.electrical": [null, {"after_minutes": 25}]}}

which requires knowing that ladders are arrays, that position is semantics, that
`after_minutes` is an absolute offset from when the loom stopped rather than a wait, and
that reason codes are namespaced. That is four pieces of engineering knowledge to change
one number, and the whole point of this layer is that the person doing it has none of them.

So this module speaks the plant's vocabulary and compiles down to the same merge patches:

    teams      -> routing.roles          "who fixes it"
    people     -> routing.people         "who we message"
    roster     -> roles.<team>.{A,B,C}   "who is on which shift"
    plans      -> escalation.ladders     "who to call, and when"

Two translations are load-bearing rather than cosmetic:

**Waits, not offsets.** A step carries `wait_minutes` — the gap since the previous step —
and the compiler accumulates them into the absolute `after_minutes` the engine wants.
Operators read [0, 20, 45] as "immediately, then 20 minutes, then 45 more" essentially
every time; it actually means T+0, T+20, T+45. Taking gaps in also makes a non-monotonic
ladder unrepresentable, so "shorten step 2 and the whole chain fires at once" stops being
something validation must catch and becomes something the format cannot express.

**Every shift key is always written.** `config.role_person_ids` falls back to shift A when
a shift key is *missing* but returns nobody when it is *present and empty* — two opposite
behaviours that look nearly identical in YAML, one of which pages the day electrician at
3am. Writing all three keys every time makes the ambiguous state unreachable.

Referential rules are hard blocks, never cascades: the API refuses and names what is in the
way, and the operator fixes it deliberately. Deleting the last electrician while electrical
faults still route to electricians is exactly the silent failure this system exists to
remove; doing it automatically on their behalf would be worse, not better.
"""

from __future__ import annotations

import json
import re

from flask import Blueprint, current_app, jsonify, request

from .. import clock, config, db
from ..core import events
from .config_api import _actor, _authorised

bp = Blueprint("admin_api", __name__)

SHIFT_KEYS = ("A", "B", "C")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,62}$")

# `owner` is reserved: escalation ladders use it to mean "whichever team owns this
# reason", so a team actually named `owner` would silently capture every ladder rung.
RESERVED_TEAM_IDS = {"owner"}
# Named built-ins. Neither may be deleted or saved empty: an empty `unknown` means the
# reason prompt is never sent again, and an empty `default` means tickets open with
# nobody paged. Both pass config.validate() today.
BUILTIN_PLANS = {"unknown": "no_reason_yet", "default": "fallback"}
PLAN_ALIASES = {v: k for k, v in BUILTIN_PLANS.items()}


# --------------------------------------------------------------------------- helpers

def _build_id() -> str:
    """Identifies the deployed page, so a stale browser cache and a deploy that never
    landed stop looking identical. Derived from the mtime of the file actually served."""
    try:
        import os
        from pathlib import Path
        f = Path(__file__).resolve().parent.parent / "static" / "admin.html"
        return str(int(os.stat(f).st_mtime))[-6:]
    except Exception:
        return "?"


def _cfg():
    return current_app.config["OPS_CFG"]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")


def _err(status: int, message: str, **extra):
    body = {"error": message}
    body.update(extra)
    return jsonify(body), status


def _read_guard():
    """Reads need the key too, not just writes.

    /api/admin/overview returns the roster — every person's name and mobile number — and
    /api/admin/simulate returns who would be called, with their number attached. Behind
    the SSH tunnel that is nobody's problem. The moment this is proxied for Zenscale it
    is the plant's phone book on the open internet, and a read-only leak is still a leak.
    Returns None when the caller may proceed, or a 403 response when they may not.
    """
    ok, why = _authorised()
    return None if ok else _err(403, why)


def _apply(scope: str, patch: dict, actor: str, summary: dict):
    """Validate a proposed patch, persist it, hot-reload, and audit it.

    Deliberately the same pipeline config_api.patch_scope uses — this layer only decides
    WHAT the patch is, never whether the safety rules apply to it.
    """
    cfg = _cfg()
    overrides = config.load_overrides()
    # compose_patch, not merge_patch: this is patch-onto-patch, so a null is an
    # instruction that must survive to be executed against the YAML base later.
    overrides[scope] = config.compose_patch(overrides.get(scope, {}), patch)

    try:
        config.validate(config.candidate(cfg, overrides))
    except config.ConfigError as exc:
        return _err(422, "that change would leave the configuration invalid — not applied",
                    problems=str(exc).splitlines()[1:])

    now = clock.now_iso()
    with db.transaction() as c:
        c.execute(
            "INSERT INTO config_overrides(scope, patch, updated_at, updated_by)"
            " VALUES (?,?,?,?) ON DUPLICATE KEY UPDATE"
            " patch=VALUES(patch), updated_at=VALUES(updated_at),"
            " updated_by=VALUES(updated_by)",
            (scope, json.dumps(overrides[scope]), now, actor),
        )
        events.log(c, "config", 0, "config_changed", actor=actor,
                   detail={"scope": scope, "via": "admin_api", "patch": patch,
                           **summary},
                   department=cfg.department, at=now)

    config.reload_into(cfg, overrides)
    return jsonify({"ok": True, "version": cfg.version, **summary})


def _teams_using(cfg, team: str) -> dict:
    """Everything that would dangle if `team` disappeared."""
    reasons = [c["code"] for c in cfg.codes if c.get("owner") == team]
    steps = []
    for name, rungs in (cfg.ladders or {}).items():
        for i, rung in enumerate(rungs or []):
            if rung.get("notify") == team:
                steps.append({"plan": BUILTIN_PLANS.get(name, name), "step": i + 1})
    return {"reasons": reasons, "plan_steps": steps}


def _targeted_teams(cfg) -> set:
    """Teams that something actually routes to, so emptying their roster matters.
    `owner` resolves to a reason's own team, so every reason-owning team counts."""
    out = {c.get("owner") for c in cfg.codes if c.get("owner")}
    for rungs in (cfg.ladders or {}).values():
        for rung in rungs or []:
            role = rung.get("notify")
            if role and role != "owner":
                out.add(role)
    return {r for r in out if r}


def _roster_of(cfg, team: str) -> dict:
    spec = (cfg.roles or {}).get(team, {}) or {}
    if "all" in spec:
        return {"all": list(spec["all"] or [])}
    return {k: list(spec.get(k) or []) for k in SHIFT_KEYS}


def _roster_patch(roster: dict) -> dict:
    """A roster as a merge patch that genuinely REPLACES what is there.

    roles.<team> is a dict, so merge_patch recurses and only the per-shift lists replace.
    Any bucket present in the stored config and absent here survives — and
    role_person_ids checks "all" before A/B/C, so a leftover 24/7 bucket silently wins
    over every named shift. Sending the absent buckets as explicit nulls is what makes
    "this is the roster now" mean what it says.
    """
    out = dict(roster)
    for bucket in ("all",) + SHIFT_KEYS:
        if bucket not in out:
            out[bucket] = None
    return out


def _empty_shifts(roster: dict) -> list:
    if "all" in roster:
        return [] if roster["all"] else ["all"]
    return [k for k in SHIFT_KEYS if not roster.get(k)]


# --------------------------------------------------------------------------- overview

@bp.get("/api/admin/overview")
def overview():
    """Everything the admin UI needs in one call, so the page has no waterfall."""
    denied = _read_guard()
    if denied:
        return denied
    cfg = _cfg()
    overrides = config.load_overrides()
    targeted = _targeted_teams(cfg)

    teams = []
    for team in sorted(cfg.roles or {}):
        roster = _roster_of(cfg, team)
        teams.append({
            "id": team,
            "roster": roster,
            "used_by": _teams_using(cfg, team),
            "targeted": team in targeted,
            "empty_shifts": _empty_shifts(roster),
        })

    people = []
    for pid, p in sorted((cfg.people or {}).items()):
        people.append({
            "id": pid,
            "name": p.get("name", pid),
            "whatsapp": p.get("whatsapp"),
            "gchat_space": p.get("gchat_space"),
            # which channel routing would actually pick — WhatsApp wins silently
            "channel": "whatsapp" if p.get("whatsapp")
                       else ("gchat" if p.get("gchat_space") else "log"),
            "placeholder": bool(p.get("placeholder")),
            "rostered_in": sorted(t for t, s in (cfg.roles or {}).items()
                                  if pid in {m for b in (s or {}).values() for m in (b or [])}),
        })

    return jsonify({
        "version": cfg.version,
        "build": _build_id(),
        # The person every unrouted notification falls back to. Not a mode — just the
        # last line before a fault reaches nobody.
        "backstop": cfg.routing.get("default_owner"),
        "department": cfg.department,
        "asset_type": cfg.asset_type,
        "writes_enabled": _authorised()[0] or "key required",
        "overridden_scopes": sorted(overrides.keys()),
        "teams": teams,
        "people": people,
        "plans": _plans_payload(cfg),
        "reasons": [{
            "code": c["code"],
            "label": cfg.label(c["code"]),
            "owner": c.get("owner"),
            "ticketable": bool(c.get("ticketable")),
            "in_prompt": bool(c.get("show_in_prompt")),
            "expected_minutes": c.get("expected_minutes"),
            "prompt_aliases": c.get("prompt_aliases") or [],
            "is_other": c.get("code") == cfg.other_code,
        } for c in cfg.codes],
        # So the roster grid can label each column with its actual hours. "Shift C" means
        # nothing to somebody deciding whether Ravi can cover it; "22:00-06:00" does.
        "shift_times": {k: (cfg.shifts or {}).get(k) for k in ("A", "B", "C")},
        "notes": {
            "prompt_list_locked": "Which reasons appear in the WhatsApp prompt is fixed by "
                                  "the approved template. Changing it needs a new template "
                                  "approved by Meta, so it is not editable here.",
        },
    })


# --------------------------------------------------------------------------- people

@bp.get("/api/admin/people")
def list_people():
    denied = _read_guard()
    if denied:
        return denied
    return jsonify({"people": overview().json["people"]})


@bp.post("/api/admin/people")
def create_person():
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return _err(400, "name is required")

    pid = (body.get("id") or _slug(name)).strip()
    if not _ID_RE.match(pid):
        return _err(400, "id must be lowercase letters, digits and underscores",
                    got=pid)
    if pid in (_cfg().people or {}):
        return _err(409, "a person with that id already exists", id=pid)

    person = {"name": name}
    if body.get("whatsapp"):
        person["whatsapp"] = str(body["whatsapp"]).strip()
    if body.get("gchat_space"):
        person["gchat_space"] = str(body["gchat_space"]).strip()
    if not person.get("whatsapp") and not person.get("gchat_space"):
        return _err(400, "give the person a whatsapp number or a chat space — without "
                         "one they can be rostered but never actually reached")

    return _apply("routing", {"people": {pid: person}}, _actor(),
                  {"created": "person", "id": pid})


@bp.patch("/api/admin/people/<pid>")
def update_person(pid: str):
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    cfg = _cfg()
    if pid not in (cfg.people or {}):
        return _err(404, "no such person", id=pid)

    body = request.get_json(silent=True) or {}
    patch = {}
    for field in ("name", "whatsapp", "gchat_space"):
        if field not in body:
            continue
        value = str(body[field] or "").strip()
        if not value:
            # Emptying a field is not how somebody leaves. Clearing the only contact
            # channel would leave a person who is still rostered and still resolves to a
            # recipient, but whose pages go to a log file — so an accidental blur on an
            # empty box would silently stop the plant being called.
            return _err(400, f"{field} cannot be emptied here",
                        hint="to remove somebody, take them off every shift and then "
                             "delete them — that path checks what depends on them first")
        patch[field] = value
    if not patch:
        return _err(400, "nothing to change",
                    editable=["name", "whatsapp", "gchat_space"])

    # A real contact replacing a placeholder clears the flag — that flag is what keeps
    # ops-core from booting live against invented numbers, so it must not linger once a
    # genuine number is in.
    merged = dict(cfg.people[pid], **{k: v for k, v in patch.items() if v is not None})
    if merged.get("whatsapp") and not str(merged["whatsapp"]).startswith("+9190000000"):
        patch["placeholder"] = None

    return _apply("routing", {"people": {pid: patch}}, _actor(),
                  {"updated": "person", "id": pid})


@bp.delete("/api/admin/people/<pid>")
def delete_person(pid: str):
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    cfg = _cfg()
    if pid not in (cfg.people or {}):
        return _err(404, "no such person", id=pid)

    rostered = sorted(t for t, s in (cfg.roles or {}).items()
                      if pid in {m for b in (s or {}).values() for m in (b or [])})
    if rostered:
        return _err(409, "that person is still on a team's roster — take them off first",
                    id=pid, rostered_in=rostered)
    if cfg.routing.get("default_owner") == pid:
        return _err(409, "that person is the default owner — every unrouted notification "
                         "falls back to them. Set a different default owner first", id=pid)

    return _apply("routing", {"people": {pid: None}}, _actor(),
                  {"deleted": "person", "id": pid})


# --------------------------------------------------------------------------- teams

@bp.post("/api/admin/teams")
def create_team():
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or body.get("id") or "").strip()
    if not name:
        return _err(400, "name is required")
    tid = (body.get("id") or _slug(name)).strip()
    if not _ID_RE.match(tid):
        return _err(400, "id must be lowercase letters, digits and underscores", got=tid)
    if tid in RESERVED_TEAM_IDS:
        return _err(409, f"'{tid}' is reserved — escalation steps use it to mean "
                         "'whichever team owns this reason'", id=tid)
    if tid in (_cfg().roles or {}):
        return _err(409, "a team with that id already exists", id=tid)

    members = body.get("members") or {}
    roster = _normalise_roster(members)
    if isinstance(roster, tuple):
        return roster

    return _apply("routing", {"roles": {tid: roster}}, _actor(),
                  {"created": "team", "id": tid, "roster": roster})


@bp.delete("/api/admin/teams/<tid>")
def delete_team(tid: str):
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    cfg = _cfg()
    if tid not in (cfg.roles or {}):
        return _err(404, "no such team", id=tid)

    used = _teams_using(cfg, tid)
    if used["reasons"] or used["plan_steps"]:
        return _err(409, "that team is still being routed to — move those over first",
                    id=tid, used_by=used)

    return _apply("routing", {"roles": {tid: None}}, _actor(),
                  {"deleted": "team", "id": tid})


# --------------------------------------------------------------------------- roster

def _normalise_roster(members):
    """Accept {"A": [...], "B": [...], "C": [...]} or {"all": [...]}, and always emit
    every key. See the module docstring for why a missing key is dangerous."""
    if not isinstance(members, dict):
        return _err(400, 'members must be an object: {"A": [...], "B": [...], "C": [...]} '
                         'or {"all": [...]}')
    cfg = _cfg()
    known = set(cfg.people or {})

    if "all" in members:
        ids = [str(x) for x in (members["all"] or [])]
        unknown = [i for i in ids if i not in known]
        if unknown:
            return _err(422, "unknown people", unknown=unknown)
        return {"all": ids}

    out = {}
    for k in SHIFT_KEYS:
        ids = [str(x) for x in (members.get(k) or [])]
        unknown = [i for i in ids if i not in known]
        if unknown:
            return _err(422, f"unknown people on shift {k}", unknown=unknown)
        out[k] = ids
    return out


@bp.put("/api/admin/roster/<tid>")
def set_roster(tid: str):
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    cfg = _cfg()
    if tid not in (cfg.roles or {}):
        return _err(404, "no such team", id=tid)

    roster = _normalise_roster((request.get_json(silent=True) or {}).get("members"))
    if isinstance(roster, tuple):
        return roster

    # Hard block: somebody must be on duty for every shift a team is actually paged on.
    # A supervisor covering two roles is fine; nobody covering one is the failure this
    # system exists to remove.
    empty = _empty_shifts(roster)
    if empty and tid in _targeted_teams(cfg):
        return _err(409,
                    "that would leave nobody on duty for a team that faults route to",
                    id=tid, empty_shifts=empty, used_by=_teams_using(cfg, tid),
                    hint="assign someone to every shift — one person may cover two teams")

    # roles.<tid> is a DICT, so merge_patch recurses into it and only the per-shift lists
    # replace. A bucket present in the base and absent here therefore SURVIVES — and
    # role_person_ids checks "all" before A/B/C, so splitting a 24/7 team into three
    # named shifts was accepted, echoed back, and silently ignored: the original
    # always-on person kept taking every page. The absent buckets are sent as explicit
    # nulls so the replacement is real.
    return _apply("routing", {"roles": {tid: _roster_patch(roster)}}, _actor(),
                  {"updated": "roster", "id": tid, "roster": roster})


@bp.post("/api/admin/roster/<tid>/<shift>")
def add_to_shift(tid: str, shift: str):
    """Put one person on one shift of one team.

    PUT /roster/<team> replaces the whole grid, which is right for a bulk edit and wrong
    for "Ravi is on nights now": the caller has to read the current roster, splice it,
    and write it all back, and any concurrent edit is silently lost in the round trip.

    Accepts an existing person_id, or a name and number to create one and assign them in
    the same call — because "add someone to this team" is one thought, and making it two
    API calls is how a half-finished roster happens.
    """
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    cfg = _cfg()
    if tid not in (cfg.roles or {}):
        return _err(404, "no such team", id=tid)
    roster = _roster_of(cfg, tid)
    bucket = "all" if "all" in roster else shift.upper()
    if bucket != "all" and bucket not in SHIFT_KEYS:
        return _err(400, "shift must be A, B or C", got=shift)

    body = request.get_json(silent=True) or {}
    pid = (body.get("person_id") or "").strip()
    people_patch = {}

    if not pid:
        name = (body.get("name") or "").strip()
        if not name:
            return _err(400, "give either person_id, or a name to create a new person")
        pid = (body.get("id") or _slug(name)).strip()
        if not _ID_RE.match(pid):
            return _err(400, "id must be lowercase letters, digits and underscores", got=pid)
        if pid in (cfg.people or {}):
            return _err(409, "a person with that id already exists — pass person_id to "
                             "add the existing one", id=pid)
        person = {"name": name[:120]}
        for field, cap in (("whatsapp", 32), ("gchat_space", 120)):
            if body.get(field):
                value = str(body[field]).strip()
                if len(value) > cap:
                    return _err(400, f"{field} is too long", max_length=cap)
                person[field] = value
        if not person.get("whatsapp") and not person.get("gchat_space"):
            return _err(400, "give the person a whatsapp number or a chat space — "
                             "without one they can be rostered but never reached")
        people_patch = {pid: person}
    elif pid not in (cfg.people or {}):
        return _err(404, "no such person", person_id=pid)

    if pid in roster.get(bucket, []):
        return _err(409, "already on that shift", person_id=pid, team=tid, shift=bucket)

    roster[bucket] = list(roster.get(bucket, [])) + [pid]
    patch = {"roles": {tid: _roster_patch(roster)}}
    if people_patch:
        patch["people"] = people_patch
    return _apply("routing", patch, _actor(),
                  {"added": pid, "to": tid, "shift": bucket,
                   "created_person": bool(people_patch)})


@bp.delete("/api/admin/roster/<tid>/<shift>/<pid>")
def remove_from_shift(tid: str, shift: str, pid: str):
    """Take one person off one shift. Refuses if it would leave that shift with nobody
    on a team faults actually route to — the same rule as the bulk edit, applied where
    the operator is far more likely to trip it."""
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    cfg = _cfg()
    if tid not in (cfg.roles or {}):
        return _err(404, "no such team", id=tid)
    roster = _roster_of(cfg, tid)
    bucket = "all" if "all" in roster else shift.upper()
    if pid not in roster.get(bucket, []):
        return _err(404, "that person is not on that shift",
                    person_id=pid, team=tid, shift=bucket)

    roster[bucket] = [x for x in roster[bucket] if x != pid]
    if not roster[bucket] and tid in _targeted_teams(cfg):
        return _err(409, "that is the last person on that shift, and faults route to "
                         "this team — nobody would be called",
                    team=tid, shift=bucket, used_by=_teams_using(cfg, tid),
                    hint="add a replacement first; one person may cover two teams")

    return _apply("routing", {"roles": {tid: _roster_patch(roster)}}, _actor(),
                  {"removed": pid, "from": tid, "shift": bucket})


# --------------------------------------------------------------------------- plans

def _plans_payload(cfg) -> list:
    """Ladders as chains of waits. Both representations are returned: the operator edits
    `wait_minutes`, and `after_minutes` is shown alongside so the absolute timing the
    engine uses is never a hidden translation."""
    out = []
    for name, rungs in sorted((cfg.ladders or {}).items()):
        steps, prev = [], 0
        for i, rung in enumerate(rungs or []):
            after = int(rung.get("after_minutes", 0))
            steps.append({
                "step": i + 1,
                "notify": rung.get("notify"),
                "wait_minutes": max(0, after - prev),
                "after_minutes": after,
                "action": rung.get("action"),
            })
            prev = after
        out.append({
            "id": BUILTIN_PLANS.get(name, name),
            "key": name,
            "builtin": name in BUILTIN_PLANS,
            "applies_to": (f"any stopped {cfg.asset_type} with no reason given yet"
                           if name == "unknown"
                           else "any fault without its own plan" if name == "default"
                           else cfg.label(name)),
            "steps": steps,
        })
    return out


@bp.get("/api/admin/plans")
def list_plans():
    denied = _read_guard()
    if denied:
        return denied
    return jsonify({"version": _cfg().version, "plans": _plans_payload(_cfg())})


@bp.put("/api/admin/plans/<plan_id>")
def set_plan(plan_id: str):
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    cfg = _cfg()
    key = PLAN_ALIASES.get(plan_id, plan_id)
    if key not in (cfg.ladders or {}):
        return _err(404, "no such plan", id=plan_id,
                    known=[p["id"] for p in _plans_payload(cfg)])

    steps = (request.get_json(silent=True) or {}).get("steps")
    if not isinstance(steps, list) or not steps:
        return _err(400, "steps must be a non-empty list",
                    hint="an empty plan means nobody is ever called — "
                         "delete the reason instead if that is what you want")

    teams = set(cfg.roles or {})
    rungs, running = [], 0
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            return _err(400, f"step {i + 1} must be an object")
        notify = (s.get("notify") or "").strip()
        if notify != "owner" and notify not in teams:
            return _err(422, f"step {i + 1} calls a team that does not exist",
                        notify=notify, known_teams=sorted(teams) + ["owner"])
        try:
            wait = int(s.get("wait_minutes", 0))
        except (TypeError, ValueError):
            return _err(400, f"step {i + 1}: wait_minutes must be a whole number")
        if wait < 0:
            return _err(400, f"step {i + 1}: wait_minutes cannot be negative")
        # Bounded because this reaches clock.plus_minutes, and a plausible-looking large
        # number raises OverflowError deep inside the ticker transaction. A week is far
        # beyond any real escalation and still leaves room for a deliberate long tail.
        if wait > 7 * 24 * 60:
            return _err(422, f"step {i + 1}: {wait} minutes is longer than a week",
                        hint="escalation steps are minutes to hours, not days")
        running += wait
        rung = {"after_minutes": running, "notify": notify}
        action = s.get("action")
        if action:
            # Allow-list rather than pass-through: this lands in escalations.action
            # VARCHAR(32), and the engine only understands these two. An arbitrary string
            # either truncates or fires a rung that does nothing anybody asked for.
            if action not in ("ask_reason", "ask_eta", "notify"):
                return _err(422, f"step {i + 1}: unknown action",
                            action=str(action)[:60],
                            known=["ask_reason", "ask_eta", "notify"])
            rung["action"] = action
        rungs.append(rung)

    # The reason prompt is driven entirely by the `unknown` plan's ask_reason steps.
    # A plan that keeps the name but loses the action stops asking, silently.
    if key == "unknown" and not any(r.get("action") == "ask_reason" for r in rungs):
        return _err(422, "the 'no reason yet' plan must keep at least one step that asks "
                         f"for the reason — without it a stopped {cfg.asset_type} is "
                         "never queried",
                    hint='add "action": "ask_reason" to a step')

    return _apply("escalation", {"ladders": {key: rungs}}, _actor(),
                  {"updated": "plan", "id": plan_id, "steps": len(rungs)})


# --------------------------------------------------------------------------- reasons

@bp.patch("/api/admin/reasons/<path:code>")
def update_reason(code: str):
    """Everything about a fault type that the approved WhatsApp template does NOT
    freeze: the owning team, the typical repair time, whether it opens a ticket, and
    the reply aliases. The label, whether it appears in the question, and its position
    stay locked — those are the template's, and changing them here would leave the
    prompt and the config disagreeing with no way to tell from outside."""
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    cfg = _cfg()
    codes = {c["code"]: c for c in cfg.codes}
    if code not in codes:
        return _err(404, "no such fault type", code=code)
    current = codes[code]
    body = request.get_json(silent=True) or {}

    locked = [k for k in body if k not in
              ("owner", "expected_minutes", "ticketable", "prompt_aliases")]
    if locked:
        return _err(409, "those fields are fixed by the approved WhatsApp template",
                    rejected=locked,
                    editable=["owner", "expected_minutes", "ticketable", "prompt_aliases"])

    updated = dict(current)
    summary = {"updated": "reason", "code": code}

    if "ticketable" in body:
        want = bool(body["ticketable"])
        if not want and current.get("ticketable"):
            open_now = db.query_one(
                "SELECT COUNT(*) n FROM tickets WHERE code=? AND status!='closed'",
                (code,))["n"]
            if open_now:
                return _err(409, "tickets are open against this fault right now — "
                                 "closing the door behind them would strand them",
                            open_tickets=open_now)
            updated.pop("owner", None)
        updated["ticketable"] = want
        summary["ticketable"] = want

    if "owner" in body:
        owner = (body["owner"] or "").strip()
        if not updated.get("ticketable") and owner:
            return _err(409, "this fault does not open a ticket, so it has no owning "
                             "team — turn ticketable on first", code=code)
        if owner:
            if owner not in (cfg.roles or {}):
                return _err(422, "no such team", owner=owner,
                            known_teams=sorted(cfg.roles or {}))
            empty = _empty_shifts(_roster_of(cfg, owner))
            if empty:
                return _err(409, "that team has nobody on duty for some shifts",
                            owner=owner, empty_shifts=empty,
                            hint="staff it before routing faults to it")
            updated["owner"] = owner
            summary["owner"] = owner

    if "expected_minutes" in body:
        try:
            mins = int(body["expected_minutes"])
        except (TypeError, ValueError):
            return _err(400, "expected_minutes must be a whole number")
        if not (0 <= mins <= 24 * 60):
            return _err(422, "expected_minutes out of range", minimum=0, maximum=1440)
        updated["expected_minutes"] = mins
        summary["expected_minutes"] = mins

    if "prompt_aliases" in body:
        aliases = body["prompt_aliases"]
        if not isinstance(aliases, list) or                 not all(isinstance(a, str) and a.strip() for a in aliases):
            return _err(400, "prompt_aliases must be a list of non-empty strings")
        if len(aliases) > 8:
            return _err(422, "too many aliases", maximum=8)
        updated["prompt_aliases"] = [a.strip()[:60] for a in aliases]
        summary["aliases"] = len(aliases)

    # codes is a LIST, so the whole list is rewritten with this one entry changed —
    # merge_patch cannot address a list element, and order is the digit mapping.
    new_codes = [updated if c["code"] == code else c for c in cfg.codes]
    return _apply("reasons", {"codes": new_codes}, _actor(), summary)


@bp.post("/api/admin/reasons")
def create_reason():
    """A new fault type — as long as it stays OUT of the WhatsApp question, whose
    option list is frozen in the approved template. Reachable through reply aliases,
    the panel, or auto-classification; a supervisor can still type its name."""
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    cfg = _cfg()
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name or len(name) > 60:
        return _err(400, "name is required (up to 60 characters)")
    code = f"{cfg.department}.{_slug(name)}"
    if any(c["code"] == code for c in cfg.codes):
        return _err(409, "a fault type with that name already exists", code=code)
    if body.get("show_in_prompt"):
        return _err(409, "the WhatsApp question's options are frozen in the approved "
                         "template — a new fault type cannot appear there without a "
                         "new template being approved",
                    hint="create it without show_in_prompt; supervisors can reply with "
                         "its name, and it can own tickets and auto-classify rules")

    entry: dict = {"code": code, "label": {"en": name}}
    ticketable = bool(body.get("ticketable"))
    entry["ticketable"] = ticketable
    if ticketable:
        owner = (body.get("owner") or "").strip()
        if owner not in (cfg.roles or {}):
            return _err(422, "a fault that opens tickets needs an owning team",
                        known_teams=sorted(cfg.roles or {}))
        if _empty_shifts(_roster_of(cfg, owner)):
            return _err(409, "that team has nobody on duty for some shifts", owner=owner)
        entry["owner"] = owner
    try:
        entry["expected_minutes"] = int(body.get("expected_minutes", 30))
    except (TypeError, ValueError):
        return _err(400, "expected_minutes must be a whole number")

    return _apply("reasons", {"codes": list(cfg.codes) + [entry]}, _actor(),
                  {"created": "reason", "code": code, "ticketable": ticketable})


@bp.delete("/api/admin/reasons/<path:code>")
def delete_reason(code: str):
    """Remove a fault type — only one that history has never touched. A code with
    tickets or reasons recorded against it must stay, or every past report shows a raw
    slug where its name was; the ticketable toggle is how a live one is retired."""
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    cfg = _cfg()
    entry = next((c for c in cfg.codes if c["code"] == code), None)
    if entry is None:
        return _err(404, "no such fault type", code=code)
    if entry.get("show_in_prompt"):
        return _err(409, "this fault is a button in the approved WhatsApp question — "
                         "it cannot be removed while the template shows it")
    if code == cfg.other_code:
        return _err(409, "this is the catch-all every prompt offers — it cannot go")
    used = db.query_one(
        "SELECT (SELECT COUNT(*) FROM tickets WHERE code=?) t,"
        " (SELECT COUNT(*) FROM incident_reasons WHERE code=?) r", (code, code))
    if used["t"] or used["r"]:
        return _err(409, "history has this fault recorded against it — removing the "
                         "definition would leave raw codes in every past report",
                    tickets=used["t"], reasons_recorded=used["r"],
                    hint="turn ticketable off instead; that retires it from new use")
    if any(r.get("code") == code for r in cfg.auto_classify):
        return _err(409, "a detection rule still files stops under this fault",
                    hint="remove or repoint that rule first")
    if code in (cfg.ladders or {}):
        return _err(409, "an escalation plan is keyed to this fault",
                    hint="delete that plan first")

    return _apply("reasons",
                  {"codes": [c for c in cfg.codes if c["code"] != code]},
                  _actor(), {"deleted": "reason", "code": code})




# --------------------------------------------------------------------------- simulate

@bp.get("/api/admin/simulate")
def simulate():
    """Who would actually be called, right now, for a given reason — resolved through the
    live shift calendar. The single most useful thing in the UI: it turns an abstract
    roster edit into a list of names before anyone's phone rings."""
    denied = _read_guard()
    if denied:
        return denied
    from ..core import routing

    cfg = _cfg()
    code = request.args.get("reason") or None
    when = request.args.get("at") or clock.now_iso()
    shift = routing.current_shift(cfg, when)

    key = code if code in (cfg.ladders or {}) else "default"
    if code is None:
        key = "unknown"
    owner_role = cfg.owner_role(code) if code else None

    out = []
    prev = 0
    for i, rung in enumerate(cfg.ladders.get(key, []) or []):
        after = int(rung.get("after_minutes", 0))
        people = routing.resolve(cfg, rung.get("notify"), when_iso=when,
                                 owner_role=owner_role)
        out.append({
            "step": i + 1,
            "notify": rung.get("notify"),
            "wait_minutes": max(0, after - prev),
            "after_minutes": after,
            "action": rung.get("action"),
            "recipients": [{"id": r.person_id, "name": r.name,
                            "channel": r.channel, "address": r.address} for r in people],
            "unrouted": not people,
        })
        prev = after

    return jsonify({
        "reason": code, "plan": BUILTIN_PLANS.get(key, key), "at": when, "shift": shift,
        "steps": out,
        "any_unrouted": any(s["unrouted"] for s in out),
    })


# --------------------------------------------------------------------------- shifts

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _parse_span(span: str):
    """"06:00-14:00" -> (360, 840) in minutes from midnight. Night shifts wrap."""
    parts = (span or "").split("-")
    if len(parts) != 2:
        return None
    out = []
    for t in parts:
        m = _TIME_RE.match(t.strip())
        if not m:
            return None
        out.append(int(m.group(1)) * 60 + int(m.group(2)))
    return tuple(out)


@bp.get("/api/admin/shifts")
def get_shifts():
    denied = _read_guard()
    if denied:
        return denied
    cfg = _cfg()
    shifts = cfg.shifts or {}
    return jsonify({
        "version": cfg.version,
        "timezone": shifts.get("timezone"),
        "shifts": {k: shifts.get(k) for k in SHIFT_KEYS},
        "note": "Shift times decide who is on duty at 3am AND are what the changeover "
                "auto-classify rule matches against. One source of truth for both, so a "
                "change here moves both at once.",
    })


@bp.put("/api/admin/shifts")
def set_shifts():
    """The shift calendar. Rejected unless the three spans tile a full 24 hours with no
    gap and no overlap.

    A gap is an hour where clock.resolve_shift returns nothing, so role lookups fall
    through to whichever shift is listed first and the wrong person is paged. An overlap
    is the same fault wearing the opposite hat. Neither is caught by config.validate,
    and neither is visible in the YAML — you have to add the numbers up.
    """
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    body = request.get_json(silent=True) or {}
    spans = body.get("shifts") or {}

    parsed = {}
    for k in SHIFT_KEYS:
        span = spans.get(k)
        p = _parse_span(span)
        if p is None:
            return _err(400, f"shift {k} must look like \"06:00-14:00\"", got=span)
        parsed[k] = p

    total = 0
    for start, end in parsed.values():
        total += (end - start) % (24 * 60) or (24 * 60)
    if total != 24 * 60:
        return _err(422, "the three shifts must cover exactly 24 hours between them",
                    covered_minutes=total,
                    hint="gaps leave hours with nobody rostered; overlaps page two "
                         "shifts for the same fault")

    # Each shift must begin where the previous one ends, or the total can be right while
    # the coverage is wrong — two overlapping shifts and a matching gap sum to 24h too.
    order = sorted(SHIFT_KEYS, key=lambda k: parsed[k][0])
    for i, k in enumerate(order):
        nxt = order[(i + 1) % len(order)]
        if parsed[k][1] % (24 * 60) != parsed[nxt][0] % (24 * 60):
            return _err(422, "the shifts must run back to back with no gap or overlap",
                        after=k, before=nxt,
                        detail=f"{k} ends at {spans[k].split('-')[1]} but "
                               f"{nxt} starts at {spans[nxt].split('-')[0]}")

    patch = {"shifts": {k: spans[k] for k in SHIFT_KEYS}}
    return _apply("routing", patch, _actor(), {"updated": "shifts", "shifts": patch["shifts"]})


# --------------------------------------------------------------------------- settings

def _gap_after_first_ask(cfg) -> int | None:
    """Minutes between the first reason question and whatever happens next — the number
    the outgoing message quotes back to the supervisor."""
    ladder = cfg.ladders.get("unknown") or []
    for i, rung in enumerate(ladder):
        if rung.get("action") == "ask_reason":
            if i + 1 < len(ladder):
                return int(ladder[i + 1].get("after_minutes", 0)) - int(rung.get("after_minutes", 0))
            every = (ladder[-1] or {}).get("repeat_every_minutes")
            return int(every) if every else None
    return None


@bp.get("/api/admin/settings")
def get_settings():
    denied = _read_guard()
    if denied:
        return denied
    cfg = _cfg()
    rec = cfg.recurrence or {}
    unknown = cfg.ladders.get("unknown") or []
    first_ask = next((int(r.get("after_minutes", 0)) for r in unknown
                      if r.get("action") == "ask_reason"), None)
    return jsonify({
        "version": cfg.version,
        "short_stop_seconds": cfg.min_duration_seconds,
        "recurrence": {
            "window_hours": float(rec.get("window_hours", 8)),
            "threshold": int(rec.get("threshold", 3)),
            "jump_to_step": int(rec.get("rung", 0)) + 1,
        },
        # Both derived from the ladder that actually runs, so the message and the timer
        # cannot disagree. This used to compare a separate config key against the ladder
        # and warn when they drifted; the key is gone and the drift is now unrepresentable.
        "question_asked_after": first_ask,
        "then_asked_again_after": _gap_after_first_ask(cfg),
        "warnings": [],
    })


@bp.put("/api/admin/settings")
def set_settings():
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    cfg = _cfg()
    body = request.get_json(silent=True) or {}

    reasons_patch, escalation_patch, summary = {}, {}, {}

    if "short_stop_seconds" in body:
        try:
            secs = int(body["short_stop_seconds"])
        except (TypeError, ValueError):
            return _err(400, "short_stop_seconds must be a whole number of seconds")
        if secs < 0:
            return _err(400, "short_stop_seconds cannot be negative")
        # Above the first ask, every stop is auto-filed as a short stop before anyone is
        # ever asked about it — the prompt becomes unreachable and nothing is escalated.
        unknown = cfg.ladders.get("unknown") or []
        first_ask = next((int(r.get("after_minutes", 0)) for r in unknown
                          if r.get("action") == "ask_reason"), None)
        if first_ask is not None and secs >= first_ask * 60:
            return _err(422, "that is longer than the wait before the reason question, "
                             "so every stop would be filed as a short stop and nobody "
                             "would ever be asked",
                        short_stop_seconds=secs, question_asked_after_minutes=first_ask)
        reasons_patch.setdefault("defaults", {})["min_duration_seconds"] = secs
        summary["short_stop_seconds"] = secs

    rec = body.get("recurrence")
    if isinstance(rec, dict):
        patch = {}
        if "window_hours" in rec:
            try:
                patch["window_hours"] = float(rec["window_hours"])
            except (TypeError, ValueError):
                return _err(400, "recurrence.window_hours must be a number")
            if patch["window_hours"] <= 0:
                return _err(400, "recurrence.window_hours must be positive")
        if "threshold" in rec:
            try:
                patch["threshold"] = int(rec["threshold"])
            except (TypeError, ValueError):
                return _err(400, "recurrence.threshold must be a whole number")
            if patch["threshold"] < 2:
                return _err(422, "a threshold below 2 means every single fault is "
                                 "treated as a repeat, so the override never stops firing",
                            threshold=patch["threshold"])
        if "jump_to_step" in rec:
            try:
                step = int(rec["jump_to_step"])
            except (TypeError, ValueError):
                return _err(400, "recurrence.jump_to_step must be a whole number")
            longest = max((len(v or []) for v in (cfg.ladders or {}).values()), default=1)
            if step < 1 or step > longest:
                return _err(422, "that step does not exist in any plan",
                            jump_to_step=step, longest_plan_steps=longest)
            patch["rung"] = step - 1        # steps are 1-based for the operator
        if patch:
            escalation_patch["recurrence"] = patch
            summary["recurrence"] = patch

    if not reasons_patch and not escalation_patch:
        return _err(400, "nothing to change",
                    editable=["short_stop_seconds", "recurrence.window_hours",
                              "recurrence.threshold", "recurrence.jump_to_step"])

    # Two scopes may both change. Apply reasons first: if escalation then fails
    # validation the first is already committed, so keep each patch independently valid
    # rather than pretending this is one transaction.
    if reasons_patch:
        r = _apply("reasons", reasons_patch, _actor(), {"updated": "settings", **summary})
        if isinstance(r, tuple):
            return r
    if escalation_patch:
        return _apply("escalation", escalation_patch, _actor(),
                      {"updated": "settings", **summary})
    return jsonify({"ok": True, "version": _cfg().version, "updated": "settings", **summary})


# --------------------------------------------------------------------------- off plan

@bp.get("/api/admin/assets")
def list_assets():
    """Every loom, and whether it is currently expected to be running.

    The one thing nobody on the floor tracks today. Without it, "83% downtime" is two
    different facts added together and neither can be acted on.
    """
    denied = _read_guard()
    if denied:
        return denied
    from ..core import offplan

    cfg = _cfg()
    live = offplan.active_map()
    rows = db.query(
        "SELECT a.id, a.asset_ref, a.active,"
        " (SELECT COUNT(*) FROM incidents i WHERE i.asset_id=a.id"
        "   AND i.status IN ('open','resolving')) open_incidents"
        " FROM assets a WHERE a.department=? ORDER BY a.asset_ref",
        (cfg.department,),
    )
    out = []
    for r in rows:
        off = live.get(r["id"])
        out.append({
            "asset_ref": r["asset_ref"],
            "in_service": bool(r["active"]),
            "stopped_now": bool(r["open_incidents"]),
            "off_plan": None if not off else {
                "reason": off["reason"], "note": off["note"],
                "until": off["until_at"], "set_by": off["set_by"],
            },
        })
    return jsonify({"asset_type": cfg.asset_type, "assets": out,
                    "reasons": list(offplan.REASONS)})


@bp.put("/api/admin/assets/<asset_ref>/offplan")
def set_offplan(asset_ref: str):
    """Mark a loom as deliberately not in production until a given time.

    Set once, covers hours — asking per incident would be the extra work this exists to
    remove. `until` is required: a loom parked indefinitely is a genuine fault waiting to
    be missed, so the flag has to expire on its own.
    """
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    from ..core import incidents as inc_mod, offplan

    cfg = _cfg()
    body = request.get_json(silent=True) or {}
    reason = (body.get("reason") or "").strip()
    if reason not in offplan.REASONS:
        return _err(400, "unknown reason", got=reason, known=list(offplan.REASONS))
    until = (body.get("until") or "").strip()
    if not until:
        return _err(400, "until is required",
                    hint=f"a {cfg.asset_type} parked with no end date hides a real fault — "
                         f"give the "
                         "time you expect it back, you can always extend it")
    try:
        until_dt = clock.parse(until)
    except Exception:
        return _err(400, "until must be an ISO timestamp", got=until[:40])
    if until_dt <= clock.now():
        return _err(400, "until is in the past", got=until)

    try:
        aid = inc_mod.asset_id_for(cfg, asset_ref)
    except Exception:
        return _err(404, "no such asset", asset_ref=asset_ref)

    actor = _actor()
    now = clock.now_iso()
    closed = 0
    with db.transaction() as c:
        offplan.set_offplan(c, aid, reason, clock.to_iso(until_dt),
                            note=(body.get("note") or None), actor=actor, at=now)
        # Anything already open for this loom is planned downtime as of now, not a fault
        # nobody attended. Leaving it open would keep escalating about a machine the
        # operator has just told us is deliberately stopped.
        rows = c.execute(
            "SELECT id FROM incidents WHERE asset_id=? AND status IN ('open','resolving')",
            (aid,),
        ).fetchall()
        for r in rows:
            c.execute("UPDATE incidents SET status='resolved', resolved_at=?,"
                      " duration_s=NULL WHERE id=?", (now, r["id"]))
            c.execute("UPDATE escalations SET status='cancelled'"
                      " WHERE status='pending' AND incident_id=?", (r["id"],))
            events.log(c, "incident", r["id"], "resolved", actor=actor,
                       detail={"close_reason": "off_plan", "offplan_reason": reason},
                       department=cfg.department, at=now)
            closed += 1

    return jsonify({"ok": True, "asset_ref": asset_ref, "reason": reason,
                    "until": clock.to_iso(until_dt),
                    "incidents_closed_as_planned": closed})


@bp.delete("/api/admin/assets/<asset_ref>/offplan")
def clear_offplan(asset_ref: str):
    """Back in production. The next stop is a fault again."""
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    from ..core import incidents as inc_mod, offplan

    cfg = _cfg()
    try:
        aid = inc_mod.asset_id_for(cfg, asset_ref)
    except Exception:
        return _err(404, "no such asset", asset_ref=asset_ref)
    with db.transaction() as c:
        n = offplan.clear(c, aid)
    return jsonify({"ok": True, "asset_ref": asset_ref, "was_off_plan": bool(n)})




# --------------------------------------------------------------------------- report

@bp.get("/api/admin/report")
def accountability_report():
    """The numbers the plant head enforces with.

    Everything here is computed from records written at the moment things happened —
    promises made (eta_set), promises broken (eta_missed), reasons given, machines
    resumed — never from anybody's recollection. "Who is the biggest defaulter" is the
    first question this exists to answer, so it is the first key in the response.

    ?days=N bounds the window (default 30).
    """
    denied = _read_guard()
    if denied:
        return denied
    cfg = _cfg()
    try:
        days = max(1, min(365, int(request.args.get("days", 30))))
    except (TypeError, ValueError):
        return _err(400, "days must be a whole number")
    since = clock.plus_seconds(-days * 86400)

    people = cfg.people or {}
    name_of = lambda pid: (people.get(pid) or {}).get("name", pid)  # noqa: E731

    # --- promises, per person ----------------------------------------------------
    tickets = db.query(
        "SELECT t.id, t.code, t.owner_role, t.opened_at, t.closed_at, t.close_reason,"
        " t.status, t.eta_hours, t.eta_due_at, t.eta_by, t.eta_misses, a.asset_ref"
        " FROM tickets t JOIN incidents i ON i.id=t.incident_id"
        " JOIN assets a ON a.id=i.asset_id"
        " WHERE t.opened_at>=?", (since,))

    by_person: dict = {}
    unanswered_by_team: dict = {}
    for t in tickets:
        if t["eta_by"]:
            row = by_person.setdefault(t["eta_by"], {
                "person": name_of(t["eta_by"]), "promises": 0, "misses": 0,
                "kept": 0, "open_now": 0, "overrun_minutes": 0})
            row["promises"] += 1
            row["misses"] += t["eta_misses"] or 0
            if t["status"] == "closed" and t["close_reason"] == "asset_resumed":
                if not t["eta_misses"]:
                    row["kept"] += 1
                elif t["eta_due_at"] and t["closed_at"] and t["closed_at"] > t["eta_due_at"]:
                    over = (clock.parse(t["closed_at"])
                            - clock.parse(t["eta_due_at"])).total_seconds() / 60
                    row["overrun_minutes"] += max(0, round(over))
            elif t["status"] != "closed":
                row["open_now"] += 1
        else:
            # Asked and never answered — a defaulter statistic of its own, attributed to
            # the team because no individual ever put their name on it.
            unanswered_by_team[t["owner_role"]] = \
                unanswered_by_team.get(t["owner_role"], 0) + 1

    defaulters = sorted(by_person.values(),
                        key=lambda r: (-r["misses"], -r["overrun_minutes"]))
    for r in defaulters:
        r["kept_pct"] = round(100 * r["kept"] / r["promises"]) if r["promises"] else None

    # --- downtime, per reason and per machine -------------------------------------
    def rollup(group_sql, key):
        rows = db.query(
            f"SELECT {group_sql} k, COUNT(*) n, COALESCE(SUM(i.duration_s),0) s"
            "  FROM incidents i LEFT JOIN incident_reasons ir ON ir.incident_id=i.id"
            "  JOIN assets a ON a.id=i.asset_id"
            " WHERE i.opened_at>=? AND i.status='resolved'"
            f" GROUP BY {group_sql} ORDER BY s DESC", (since,))
        return [{key: r["k"] or "(no reason given)", "stops": r["n"],
                 "downtime_minutes": round(r["s"] / 60)} for r in rows]

    by_reason = rollup("ir.code", "reason")
    for r in by_reason:
        if r["reason"] != "(no reason given)":
            r["label"] = cfg.label(r["reason"])
    by_machine = rollup("a.asset_ref", "machine")

    # --- how fast reasons come back (supervisor-side accountability) --------------
    ask = db.query(
        "SELECT i.id, i.opened_at, MIN(ir.at) reason_at"
        "  FROM incidents i JOIN incident_reasons ir ON ir.incident_id=i.id"
        " WHERE i.opened_at>=? AND ir.method='reply'"
        " GROUP BY i.id, i.opened_at", (since,))
    waits = sorted(
        (clock.parse(r["reason_at"]) - clock.parse(r["opened_at"])).total_seconds() / 60
        for r in ask if r["reason_at"])
    reason_response = None
    if waits:
        reason_response = {
            "replies": len(waits),
            "median_minutes": round(waits[len(waits) // 2]),
            "p90_minutes": round(waits[int(len(waits) * 0.9)]),
        }

    return jsonify({
        "window_days": days,
        "since": since,
        "defaulters": defaulters,
        "asked_but_never_estimated": unanswered_by_team,
        "downtime_by_reason": by_reason,
        "downtime_by_machine": by_machine,
        "reason_response": reason_response,
        "notes": {
            "defaulters": "Sorted worst first: most missed estimates, then most minutes "
                          "over their own promise. Computed from events recorded when "
                          "each promise was made and broken.",
            "asked_but_never_estimated": "Tickets where the estimate question got no "
                                         "answer at all, per team — nobody put their "
                                         "name on a time.",
        },
    })


# --------------------------------------------------------------------------- feed

_FEED_FIELDS = {
    # panel field       -> (yaml path under settings, type, min, max)
    "poll_seconds":        ("poll_seconds", int, 5, 600),
    "rpm_threshold":       (None, float, 1, 10000),   # writes stop_when.lt AND resolve_when.gte
    "offline_after_seconds": ("offline_after_seconds", int, 30, 3600),
    "expected_count":      ("expected_count", int, 0, 500),
    "resolve_grace_seconds": ("resolve_grace_seconds", int, 0, 600),
}


@bp.get("/api/admin/feed")
def get_feed():
    """The connection to the machines, shown honestly in two columns: what is RUNNING
    (read at boot, immutable until restart) and what is PENDING (stored, waiting).
    These are behind dev mode in the panel because a wrong rpm threshold silently
    redefines what a "stop" is — every number downstream inherits the mistake."""
    denied = _read_guard()
    if denied:
        return denied
    cfg = _cfg()
    running_settings = cfg.source.get("settings", {}) or {}
    pending = (config.load_overrides().get("source") or {})
    pending_settings = (pending.get("settings") or {})

    def view(settings):
        return {
            "base_url": settings.get("base_url"),
            "poll_seconds": settings.get("poll_seconds"),
            "rpm_threshold": (settings.get("stop_when") or {}).get("lt"),
            "offline_after_seconds": settings.get("offline_after_seconds"),
            "expected_count": settings.get("expected_count"),
            "resolve_grace_seconds": settings.get("resolve_grace_seconds"),
        }

    running = view(running_settings)
    merged = view(config.merge_patch(cfg.source, pending).get("settings", {}) or {}) \
        if pending else None
    return jsonify({
        "running": running,
        "pending": merged,
        "restart_required": bool(merged and merged != running),
        "note": "The feed adapter reads these once at boot. Saved changes wait for a "
                "restart — they are never silently live and never silently inert.",
    })


@bp.put("/api/admin/feed")
def set_feed():
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    body = request.get_json(silent=True) or {}
    if body.get("dev_mode") is not True:
        return _err(403, "feed settings are behind dev mode",
                    hint='confirm dev mode in the panel — the request must carry '
                         '"dev_mode": true')

    settings_patch: dict = {}
    summary: dict = {}
    for field, (path, cast, lo, hi) in _FEED_FIELDS.items():
        if field not in body:
            continue
        try:
            value = cast(body[field])
        except (TypeError, ValueError):
            return _err(400, f"{field} must be a number")
        if not (lo <= value <= hi):
            return _err(422, f"{field} out of range", minimum=lo, maximum=hi, got=value)
        if field == "rpm_threshold":
            # One field, both sides. stop_when.lt and resolve_when.gte must stay equal
            # or a dead band opens between "stopped" and "running" where a machine is
            # neither — the YAML comment calls this out and the panel cannot recreate it.
            settings_patch["stop_when"] = {"field": "rpm", "lt": value}
            settings_patch["resolve_when"] = {"field": "rpm", "gte": value}
        else:
            settings_patch[path] = value
        summary[field] = value

    if "base_url" in body:
        url = str(body["base_url"] or "").strip()
        if not url.startswith(("http://", "https://")):
            return _err(422, "base_url must start with http:// or https://", got=url[:60])
        settings_patch["base_url"] = url
        summary["base_url"] = url

    if not settings_patch:
        return _err(400, "nothing to change",
                    editable=sorted(list(_FEED_FIELDS) + ["base_url"]))

    result = _apply("source", {"settings": settings_patch}, _actor(),
                    {"updated": "feed", **summary})
    if isinstance(result, tuple):
        return result
    payload = result.get_json()
    payload["restart_required"] = True
    payload["note"] = "saved — takes effect when ops-core restarts"
    return jsonify(payload)


# --------------------------------------------------------------------------- backstop

@bp.put("/api/admin/backstop")
def set_backstop():
    """Who unroutable faults fall back to. The last line before a fault reaches nobody,
    so it must be a real, reachable person."""
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    cfg = _cfg()
    pid = ((request.get_json(silent=True) or {}).get("person_id") or "").strip()
    person = (cfg.people or {}).get(pid)
    if not person:
        return _err(404, "no such person", person_id=pid,
                    known=sorted((cfg.people or {}).keys()))
    if not (person.get("whatsapp") or person.get("gchat_space")):
        return _err(422, "that person has no contact channel — a backstop who cannot "
                         "be reached is not a backstop", person_id=pid)
    return _apply("routing", {"default_owner": pid}, _actor(),
                  {"updated": "backstop", "person_id": pid})


# --------------------------------------------------------------------------- detection

_RULE_KINDS = {
    "fleet_stop": ("within_seconds", 1, 120),
    "fleet_stop_at_boundary": ("window_minutes", 1, 120),
    "duration_under": ("seconds", 10, 3600),
}


@bp.get("/api/admin/detection")
def get_detection():
    denied = _read_guard()
    if denied:
        return denied
    cfg = _cfg()
    active = db.query_one(
        "SELECT COUNT(*) n FROM assets WHERE department=? AND active=1",
        (cfg.department,))["n"]
    frac = float(cfg.reasons.get("fleet_fraction", 1.0))
    import math
    return jsonify({
        "version": cfg.version,
        "asset_type": cfg.asset_type,
        "fleet_fraction": frac,
        "active_machines": active,
        "fleet_threshold_now": max(2, math.ceil(active * frac)),
        "rules": [{"code": r.get("code"), "label": cfg.label(r.get("code", "")),
                   "kind": r.get("rule"),
                   "window": next((r[k] for k in ("within_seconds", "window_minutes",
                                                  "seconds") if k in r), None)}
                  for r in cfg.auto_classify],
        "rule_kinds": {k: v[0] for k, v in _RULE_KINDS.items()},
    })


@bp.put("/api/admin/detection")
def set_detection():
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    cfg = _cfg()
    body = request.get_json(silent=True) or {}
    patch: dict = {}
    summary: dict = {}

    if "asset_type" in body:
        word = str(body["asset_type"] or "").strip().lower()
        if not word.isalpha() or len(word) > 24:
            return _err(400, "asset_type must be a single word, e.g. vat or machine")
        patch["asset_type"] = word
        summary["asset_type"] = word

    if "fleet_fraction" in body:
        try:
            frac = float(body["fleet_fraction"])
        except (TypeError, ValueError):
            return _err(400, "fleet_fraction must be a number between 0 and 1")
        if not (0.1 <= frac <= 1.0):
            return _err(422, "fleet_fraction must be between 0.1 and 1.0", got=frac)
        # The failure mode worth being paranoid about, from the YAML's own analysis: a
        # low fraction on a small fleet collapses the threshold to 2, and any two
        # machines stopping in the same poll get auto-coded power_failure — which pages
        # NOBODY, so two real faults are silently swallowed. A false negative here is
        # invisible, so it needs an explicit acknowledgement, not just a save.
        import math
        active = db.query_one(
            "SELECT COUNT(*) n FROM assets WHERE department=? AND active=1",
            (cfg.department,))["n"]
        threshold = max(2, math.ceil(active * frac))
        if threshold <= 2 and active > 2 and \
                "fleet_threshold_collapses" not in (body.get("acknowledge") or []):
            return _err(409,
                        f"at {frac} with {active} machines, any 2 stopping in the same "
                        "poll would be classed as a power failure — which pages nobody, "
                        "so two real faults would be silently swallowed",
                        acknowledge_with="fleet_threshold_collapses",
                        hint="raise the fraction, or acknowledge if this is deliberate")
        patch["fleet_fraction"] = frac
        summary["fleet_fraction"] = frac

    if "rules" in body:
        rules = body["rules"]
        if not isinstance(rules, list):
            return _err(400, "rules must be a list")
        codes = {c["code"] for c in cfg.codes}
        out = []
        for i, r in enumerate(rules):
            kind = (r or {}).get("kind")
            if kind not in _RULE_KINDS:
                return _err(422, f"rule {i + 1}: unknown kind", kind=str(kind)[:40],
                            known=sorted(_RULE_KINDS))
            code = (r or {}).get("code")
            if code not in codes:
                return _err(422, f"rule {i + 1}: unknown fault code", code=str(code)[:60])
            key, lo, hi = _RULE_KINDS[kind]
            try:
                window = float(r.get("window"))
            except (TypeError, ValueError):
                return _err(400, f"rule {i + 1}: window must be a number")
            if not (lo <= window <= hi):
                return _err(422, f"rule {i + 1}: window out of range",
                            minimum=lo, maximum=hi, got=window)
            out.append({"code": code, "rule": kind,
                        key: int(window) if window == int(window) else window})
        patch["auto_classify"] = out
        summary["rules"] = len(out)

    if not patch:
        return _err(400, "nothing to change",
                    editable=["asset_type", "fleet_fraction", "rules"])
    return _apply("reasons", patch, _actor(), {"updated": "detection", **summary})
