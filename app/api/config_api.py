"""Runtime configuration API.

The YAML files are the base; this endpoint stores a patch on top and reloads the live
config without a restart. Three properties are non-negotiable, because config is the
one thing that can silently disable the whole system:

  * **Validated before commit.** A proposed change is applied to a throwaway Config and
    run through the same `config.validate()` the process uses at boot. If it fails, the
    write is rejected and nothing changes. The boot-time "fail loud" guarantee becomes
    "reject the write" — it never becomes "accept a config that routes nothing to
    nobody at 3am".
  * **Authenticated.** Mutations require `X-Admin-Key`. If `OPS_ADMIN_API_KEY` is unset
    the endpoints refuse to write at all, rather than defaulting open.
  * **Audited.** Every accepted change is appended to the event log with the patch and
    the actor, exactly like an incident. A system whose premise is "no record is a
    failure" cannot have an unlogged back door into its own routing table.

  GET    /api/config              effective config + which scopes are overridden
  GET    /api/config/<scope>      one scope
  PATCH  /api/config/<scope>      RFC 7386 merge patch; null deletes a key
  DELETE /api/config/<scope>      drop the override, reverting to the YAML
"""

from __future__ import annotations

import hmac
import json
import os

from flask import Blueprint, current_app, jsonify, request

from .. import clock, config, db
from ..core import events

bp = Blueprint("config_api", __name__)


def _authorised() -> tuple[bool, str]:
    expected = os.environ.get("OPS_ADMIN_API_KEY", "")
    if not expected:
        return False, "OPS_ADMIN_API_KEY is not set — config writes are disabled"
    presented = request.headers.get("X-Admin-Key", "")
    if not presented or not hmac.compare_digest(presented, expected):
        return False, "bad or missing X-Admin-Key"
    return True, ""


def _actor() -> str:
    return request.headers.get("X-Admin-User", "api")


def _read_guard():
    """Reads need the key too. The effective config includes routing.people — every
    name and mobile number in the plant — so an unauthenticated GET is a phone-book
    leak the moment this is proxied anywhere."""
    ok, why = _authorised()
    return None if ok else (jsonify({"error": why}), 403)


@bp.get("/api/config")
def get_config():
    denied = _read_guard()
    if denied:
        return denied
    cfg = current_app.config["OPS_CFG"]
    overrides = config.load_overrides()
    return jsonify({
        "version": cfg.version,
        "department": cfg.department,
        "editable": list(config.EDITABLE_SCOPES),
        "overridden": sorted(overrides.keys()),
        "effective": {
            "reasons": cfg.reasons,
            "routing": cfg.routing,
            "escalation": cfg.escalation,
            "source": cfg.source,     # readable, but restart-only to change
        },
    })


@bp.get("/api/config/<scope>")
def get_scope(scope: str):
    denied = _read_guard()
    if denied:
        return denied
    cfg = current_app.config["OPS_CFG"]
    if scope not in ("reasons", "routing", "escalation", "source"):
        return jsonify({"error": f"unknown scope '{scope}'"}), 404
    return jsonify({"version": cfg.version, "scope": scope,
                    "effective": getattr(cfg, scope),
                    "overridden": scope in config.load_overrides()})


@bp.patch("/api/config/<scope>")
def patch_scope(scope: str):
    ok, why = _authorised()
    if not ok:
        return jsonify({"error": why}), 403
    if scope not in config.BOOT_SCOPES:
        return jsonify({"error": f"unknown scope '{scope}'",
                        "editable": list(config.BOOT_SCOPES)}), 404

    patch = request.get_json(silent=True)
    if not isinstance(patch, dict):
        return jsonify({"error": "body must be a JSON object (RFC 7386 merge patch)"}), 400

    cfg = current_app.config["OPS_CFG"]
    overrides = config.load_overrides()
    # compose_patch, not merge_patch: this is patch-onto-patch, so a null is an
    # instruction that must survive to be executed against the YAML base later.
    overrides[scope] = config.compose_patch(overrides.get(scope, {}), patch)

    try:
        config.validate(config.candidate(cfg, overrides))
    except config.ConfigError as exc:
        # Rejected, nothing written. This is the boot-time guarantee, moved.
        return jsonify({"error": "configuration would be invalid — not applied",
                        "problems": str(exc).splitlines()[1:]}), 422

    now = clock.now_iso()
    actor = _actor()
    with db.transaction() as c:
        c.execute(
            "INSERT INTO config_overrides(scope, patch, updated_at, updated_by)"
            " VALUES (?,?,?,?) ON DUPLICATE KEY UPDATE"
            " patch=VALUES(patch), updated_at=VALUES(updated_at),"
            " updated_by=VALUES(updated_by)",
            (scope, json.dumps(overrides[scope]), now, actor),
        )
        events.log(c, "config", 0, "config_changed", actor=actor,
                   detail={"scope": scope, "patch": patch},
                   department=cfg.department, at=now)

    if scope == "source":
        # Stored and validated, applied at the next boot. The feed adapter reads these
        # once when it is built; pretending a live swap happened would be the "setting
        # that quietly does nothing" failure this API exists to avoid.
        return jsonify({"ok": True, "scope": scope, "version": cfg.version,
                        "restart_required": True,
                        "note": "saved — takes effect when ops-core restarts"})
    if scope == "source":
        return jsonify({"ok": True, "scope": scope, "version": cfg.version,
                        "restart_required": True,
                        "note": "override removed — the YAML values return at the "
                                "next restart"})
    config.reload_into(cfg, overrides)
    return jsonify({"ok": True, "scope": scope, "version": cfg.version,
                    "effective": getattr(cfg, scope)})


@bp.delete("/api/config/<scope>")
def delete_scope(scope: str):
    ok, why = _authorised()
    if not ok:
        return jsonify({"error": why}), 403
    if scope not in config.BOOT_SCOPES:
        return jsonify({"error": f"unknown scope '{scope}'"}), 404

    cfg = current_app.config["OPS_CFG"]
    overrides = config.load_overrides()
    if scope not in overrides:
        return jsonify({"ok": True, "scope": scope, "note": "no override was set"})
    overrides.pop(scope)

    try:
        config.validate(config.candidate(cfg, overrides))
    except config.ConfigError as exc:
        return jsonify({"error": "reverting would leave an invalid configuration",
                        "problems": str(exc).splitlines()[1:]}), 422

    now, actor = clock.now_iso(), _actor()
    with db.transaction() as c:
        c.execute("DELETE FROM config_overrides WHERE scope=?", (scope,))
        events.log(c, "config", 0, "config_reverted", actor=actor,
                   detail={"scope": scope}, department=cfg.department, at=now)

    config.reload_into(cfg, overrides)
    return jsonify({"ok": True, "scope": scope, "version": cfg.version,
                    "effective": getattr(cfg, scope)})
