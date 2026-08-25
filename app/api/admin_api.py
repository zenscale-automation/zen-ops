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
    overrides[scope] = config.merge_patch(overrides.get(scope, {}), patch)

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


def _pilot_mode(cfg) -> dict:
    """route_all_to_default sends EVERY page to one person regardless of which team owns
    the fault. It is the right setting before a real roster exists — one accountable
    person is honest, per-role routing into invented names is not — but while it is on,
    every team and roster edit made here is STORED AND INERT. An admin screen that lets
    someone rewire the call-out chain and shows no sign that nothing will change is worse
    than one that refuses the edit, so this is reported on every read."""
    on = bool(cfg.route_all_to_default)
    owner = cfg.routing.get("default_owner")
    person = (cfg.people or {}).get(owner) or {}
    return {
        "route_all_to_default": on,
        "default_owner": owner,
        "default_owner_name": person.get("name", owner),
        "explanation": (
            "Pilot mode is ON: every notification goes to %s no matter which team owns "
            "the fault. Team and roster changes are saved but will not change who is "
            "called until pilot mode is turned off." % (person.get("name", owner) or "the default owner")
        ) if on else None,
    }


def _roster_of(cfg, team: str) -> dict:
    spec = (cfg.roles or {}).get(team, {}) or {}
    if "all" in spec:
        return {"all": list(spec["all"] or [])}
    return {k: list(spec.get(k) or []) for k in SHIFT_KEYS}


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
        "department": cfg.department,
        "asset_type": cfg.asset_type,
        "shadow_mode": cfg.shadow_mode,
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
        } for c in cfg.codes],
        "pilot_mode": _pilot_mode(cfg),
        "notes": {
            "prompt_list_locked": "Which reasons appear in the WhatsApp prompt is fixed by "
                                  "the approved template. Changing it needs a new template "
                                  "approved by Meta, so it is not editable here.",
            "shadow_mode": "No message leaves the box while shadow mode is on.",
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
        if field in body:
            patch[field] = str(body[field]).strip() if body[field] else None
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

    # roles.<tid> is replaced wholesale, not merged: a merge patch cannot remove a person
    # from a shift list, and half-applying a roster is worse than refusing it.
    return _apply("routing", {"roles": {tid: roster}}, _actor(),
                  {"updated": "roster", "id": tid, "roster": roster})


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
            "applies_to": ("any stopped loom with no reason given yet" if name == "unknown"
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
        running += wait
        rung = {"after_minutes": running, "notify": notify}
        if s.get("action"):
            rung["action"] = s["action"]
        rungs.append(rung)

    # The reason prompt is driven entirely by the `unknown` plan's ask_reason steps.
    # A plan that keeps the name but loses the action stops asking, silently.
    if key == "unknown" and not any(r.get("action") == "ask_reason" for r in rungs):
        return _err(422, "the 'no reason yet' plan must keep at least one step that asks "
                         "for the reason — without it a stopped loom is never queried",
                    hint='add "action": "ask_reason" to a step')

    return _apply("escalation", {"ladders": {key: rungs}}, _actor(),
                  {"updated": "plan", "id": plan_id, "steps": len(rungs)})


# --------------------------------------------------------------------------- reasons

@bp.patch("/api/admin/reasons/<path:code>")
def set_reason_owner(code: str):
    """Only the owning team is editable. The reason's label, whether it appears in the
    prompt, and the order it appears in are all frozen into the approved WhatsApp
    template — changing any of them here would leave the prompt and the config disagreeing
    with no way to tell from the outside."""
    ok, why = _authorised()
    if not ok:
        return _err(403, why)
    cfg = _cfg()
    codes = {c["code"]: c for c in cfg.codes}
    if code not in codes:
        return _err(404, "no such reason", code=code)

    body = request.get_json(silent=True) or {}
    locked = [k for k in body if k not in ("owner",)]
    if locked:
        return _err(409, "only the owning team can be changed here",
                    rejected=locked,
                    detail="labels, prompt visibility and ordering are fixed by the "
                           "approved WhatsApp template and need a new template approved "
                           "before they can change")
    owner = (body.get("owner") or "").strip()
    if owner not in (cfg.roles or {}):
        return _err(422, "no such team", owner=owner, known_teams=sorted(cfg.roles or {}))
    if not codes[code].get("ticketable"):
        return _err(409, "that reason does not open a ticket, so it has no owning team",
                    code=code)

    # A team with an empty shift is legal only while nothing routes to it. Pointing a
    # fault at one is what MAKES it routed, so the emptiness check belongs here too —
    # otherwise a brand-new team silently becomes the owner of a fault nobody is on call
    # for, which is the failure the roster block exists to prevent, entered by the back
    # door.
    empty = _empty_shifts(_roster_of(cfg, owner))
    if empty:
        return _err(409, "that team has nobody on duty for some shifts — staff it before "
                         "routing faults to it",
                    owner=owner, empty_shifts=empty,
                    hint="set the roster for " + owner + " first")

    # codes is a LIST in reasons.yaml, so a merge patch cannot address one entry — the
    # whole list is rewritten with just this owner changed.
    new_codes = [dict(c, owner=owner) if c["code"] == code else c for c in cfg.codes]
    return _apply("reasons", {"codes": new_codes}, _actor(),
                  {"updated": "reason_owner", "code": code, "owner": owner})


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
                                 owner_role=owner_role,
                                 for_prompt=(rung.get("action") == "ask_reason"))
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
        "shadow_mode": cfg.shadow_mode,
        "pilot_mode": _pilot_mode(cfg),
        "steps": out,
        "any_unrouted": any(s["unrouted"] for s in out),
    })
