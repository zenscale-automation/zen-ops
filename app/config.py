"""Configuration loading + validation.

The core carries no domain judgement; all of it lives in per-department YAML so a
Shingora engineer can edit it without a redeploy (design doc section 6). This module
loads those files and validates them **loudly at boot** — a typo like
`owner: electricain` must crash the process on start, not silently route nothing to
nobody at 3am.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger("ops.config")

# Scopes the config API may edit at runtime. `source` is deliberately excluded: its
# settings are read once when the source adapter is constructed, so changing them
# without rebuilding the adapter would appear to work and silently do nothing — and
# rebuilding it mid-shift discards the API cursor and the per-loom state. Source
# changes stay a restart.
EDITABLE_SCOPES = ("reasons", "routing", "escalation")

_reload_lock = threading.Lock()

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    load_dotenv = None


class ConfigError(Exception):
    """Raised with a human-readable list of everything wrong in the config."""


REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


@dataclass
class Config:
    department: str
    config_dir: Path
    host: str
    port: int
    default_channel: str
    # MySQL / phpMyAdmin datastore
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    db_name: str
    table_prefix: str
    shadow_mode: bool = True
    reasons: dict = field(default_factory=dict)
    routing: dict = field(default_factory=dict)
    escalation: dict = field(default_factory=dict)
    source: dict = field(default_factory=dict)
    version: int = 0

    def db_params(self) -> dict:
        return {
            "host": self.db_host,
            "port": self.db_port,
            "user": self.db_user,
            "password": self.db_password,
            "database": self.db_name,
        }

    # ---- accessors used across the core ----

    @property
    def defaults(self) -> dict:
        return self.reasons.get("defaults", {})

    @property
    def min_duration_seconds(self) -> int:
        return int(self.defaults.get("min_duration_seconds", 120))

    @property
    def prompt_after_minutes(self) -> float:
        return float(self.defaults.get("prompt_after_minutes", 15))

    @property
    def reprompt_after_minutes(self) -> float:
        return float(self.defaults.get("reprompt_after_minutes", 15))

    @property
    def auto_classify(self) -> list[dict]:
        return self.reasons.get("auto_classify", []) or []

    @property
    def codes(self) -> list[dict]:
        return self.reasons.get("codes", []) or []

    def code_map(self) -> dict[str, dict]:
        return {c["code"]: c for c in self.codes}

    def code(self, code: str) -> dict | None:
        return self.code_map().get(code)

    def is_ticketable(self, code: str) -> bool:
        c = self.code(code)
        return bool(c and c.get("ticketable"))

    def owner_role(self, code: str) -> str | None:
        c = self.code(code)
        return c.get("owner") if c else None

    def expected_minutes(self, code: str) -> int:
        c = self.code(code)
        return int(c.get("expected_minutes", 0)) if c else 0

    def label(self, code: str, lang: str = "en") -> str:
        c = self.code(code) or {}
        lab = c.get("label", {})
        if isinstance(lab, dict):
            return lab.get(lang) or lab.get("en") or code
        return str(lab or code)

    @property
    def other_code(self) -> str:
        """The catch-all reason appended to every prompt. Derived from the department
        (`weaving.other`, `dyeing.other`) or set explicitly as `defaults.other_code`.
        It was hardcoded to "weaving.other" in core/prompts.py, which meant a second
        department would silently record its catch-all under weaving's namespace."""
        return self.defaults.get("other_code") or f"{self.department}.other"

    @property
    def asset_type(self) -> str:
        """What one asset is called, for UI labels: loom, vat, machine."""
        return self.reasons.get("asset_type", "asset")

    def prompt_codes(self) -> list[dict]:
        """Reason options shown in a WhatsApp prompt — only codes explicitly flagged
        show_in_prompt: true, in file order. 'Other' is appended by prompts.options().
        Kept short on purpose (design doc 3.4: a short, unambiguous list)."""
        return [c for c in self.codes if c.get("show_in_prompt", False)]

    # routing
    @property
    def roles(self) -> dict:
        return self.routing.get("roles", {})

    @property
    def people(self) -> dict:
        return self.routing.get("people", {})

    @property
    def shifts(self) -> dict:
        return self.routing.get("shifts", {})

    def person(self, person_id: str) -> dict | None:
        return self.people.get(person_id)

    @property
    def default_owner(self) -> str | None:
        """One named person who catches anything a role does not resolve to. Without
        this, a reason whose role has nobody on the current shift routes to nowhere and
        the fault is silently unassigned — the exact defect the system exists to remove."""
        return self.routing.get("default_owner")

    @property
    def route_all_to_default(self) -> bool:
        """Pilot switch: send EVERY notification to default_owner regardless of role,
        for running before the real per-role roster exists."""
        return bool(self.routing.get("route_all_to_default", False))

    def role_person_ids(self, role: str, shift: str | None) -> list[str]:
        spec = self.roles.get(role, {})
        if "all" in spec:
            return list(spec["all"])
        if shift and shift in spec:
            return list(spec[shift])
        # fall back to any defined shift
        for k in ("A", "B", "C"):
            if k in spec:
                return list(spec[k])
        return []

    # escalation
    @property
    def ladders(self) -> dict:
        return self.escalation.get("ladders", {})

    def ladder_for(self, code: str | None) -> list[dict]:
        lads = self.ladders
        if code and code in lads:
            return lads[code]
        return lads.get("default", [])

    @property
    def unknown_ladder(self) -> list[dict]:
        return self.ladders.get("unknown", [])

    @property
    def recurrence(self) -> dict:
        return self.escalation.get("recurrence", {}) or {}

    @property
    def quiet_hours(self):
        return self.escalation.get("quiet_hours")

    # source
    def source_setting(self, key: str, default=None):
        return (self.source.get("settings", {}) or {}).get(key, default)

    def source_api_key(self) -> str | None:
        """Key for whatever the department's source talks to. The env var NAME comes
        from source.yaml (`api_key_env`); the value never appears in config."""
        env_name = self.source_setting("api_key_env", "LOOM_API_KEY")
        return _env(env_name)

    # Kept so an existing adapter keeps working; prefer source_api_key().
    loom_api_key = source_api_key


def merge_patch(target, patch):
    """RFC 7386 JSON merge patch. Objects merge recursively, null deletes a key, and
    anything else — including lists — replaces wholesale. Lists are replaced rather than
    merged on purpose: an escalation ladder is an ordered sequence, and 'merging' two
    ladders element-wise produces a rung order nobody asked for."""
    if not isinstance(patch, dict):
        return patch
    if not isinstance(target, dict):
        target = {}
    out = dict(target)
    for key, value in patch.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = merge_patch(out.get(key), value)
    return out


def load_overrides() -> dict:
    """Patches stored by the config API. Missing table (pre-migration) is not fatal."""
    from . import db
    try:
        rows = db.query("SELECT scope, patch FROM config_overrides")
    except Exception:
        return {}
    out = {}
    for r in rows:
        try:
            out[r["scope"]] = json.loads(r["patch"])
        except (TypeError, ValueError):
            log.warning("config override for '%s' is not valid JSON — ignored", r["scope"])
    return out


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"missing config file: {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"config file is not a mapping: {path}")
    return data


def load() -> Config:
    if load_dotenv:
        load_dotenv(REPO_ROOT / ".env")

    department = _env("OPS_DEPARTMENT")
    if not department:
        # No default on purpose: a department name baked into the core is the very
        # coupling this layer exists to avoid, and silently defaulting would let a
        # misconfigured second deployment load the wrong department's rules.
        available = sorted(
            p.name for p in (REPO_ROOT / _env("OPS_CONFIG_DIR", "departments")).glob("*")
            if p.is_dir()
        ) if (REPO_ROOT / _env("OPS_CONFIG_DIR", "departments")).exists() else []
        raise ConfigError(
            "OPS_DEPARTMENT is not set — refusing to guess which department's rules to "
            "load. Set it in .env"
            + (f". Available: {', '.join(available)}" if available else "")
        )
    config_dir = REPO_ROOT / _env("OPS_CONFIG_DIR", "departments")
    dept_dir = config_dir / department

    cfg = Config(
        department=department,
        config_dir=config_dir,
        host=_env("OPS_HOST", "127.0.0.1"),
        port=int(_env("OPS_PORT", "8000")),
        default_channel=_env("OPS_DEFAULT_CHANNEL", "log"),
        db_host=_env("OPS_DB_HOST", "127.0.0.1"),
        db_port=int(_env("OPS_DB_PORT", "3306")),
        db_user=_env("OPS_DB_USER", "opsuser"),
        db_password=_env("OPS_DB_PASSWORD", ""),
        db_name=_env("OPS_DB_NAME", "ops_core"),
        table_prefix=_env("OPS_TABLE_PREFIX", ""),
        shadow_mode=str(_env("OPS_SHADOW_MODE", "true")).strip().lower()
        not in ("0", "false", "no", "off"),
        reasons=_read_yaml(dept_dir / "reasons.yaml"),
        routing=_read_yaml(dept_dir / "routing.yaml"),
        escalation=_read_yaml(dept_dir / "escalation.yaml"),
        source=_read_yaml(dept_dir / "source.yaml"),
    )
    apply_overrides(cfg, load_overrides())
    validate(cfg)
    return cfg


def apply_overrides(cfg: Config, overrides: dict) -> None:
    """Layer stored patches over the YAML, in place."""
    for scope in EDITABLE_SCOPES:
        patch = overrides.get(scope)
        if patch:
            setattr(cfg, scope, merge_patch(getattr(cfg, scope), patch))


def candidate(cfg: Config, overrides: dict) -> Config:
    """A throwaway Config with `overrides` applied, for validating a proposed change
    BEFORE it is committed. Nothing about the live config is touched."""
    dept_dir = cfg.config_dir / cfg.department
    trial = Config(
        department=cfg.department, config_dir=cfg.config_dir, host=cfg.host,
        port=cfg.port, default_channel=cfg.default_channel, db_host=cfg.db_host,
        db_port=cfg.db_port, db_user=cfg.db_user, db_password=cfg.db_password,
        db_name=cfg.db_name, table_prefix=cfg.table_prefix,
        shadow_mode=cfg.shadow_mode,
        reasons=_read_yaml(dept_dir / "reasons.yaml"),
        routing=_read_yaml(dept_dir / "routing.yaml"),
        escalation=_read_yaml(dept_dir / "escalation.yaml"),
        source=_read_yaml(dept_dir / "source.yaml"),
    )
    apply_overrides(trial, overrides)
    return trial


def reload_into(cfg: Config, overrides: dict) -> None:
    """Swap the live config's documents for the overridden ones.

    The SAME Config object is mutated rather than replaced, because workers and Flask
    captured this instance at startup. Every accessor reads through to these dicts on
    each call, so replacing them whole (never mutating in place) makes the change take
    effect on the next tick with no plumbing and no torn reads.
    """
    with _reload_lock:
        trial = candidate(cfg, overrides)
        validate(trial)                      # never install a config that fails
        cfg.reasons = trial.reasons
        cfg.routing = trial.routing
        cfg.escalation = trial.escalation
        cfg.version += 1


def validate(cfg: Config) -> None:
    problems: list[str] = []

    # Shadow mode is a DECISION, not the accident of an unfilled credential field.
    # The mock roster carries validly-formatted Indian mobile numbers; the moment a BSP
    # token is set they become live targets and ops-core starts texting strangers.
    # Leaving shadow mode with placeholder people in routing.yaml is therefore a boot
    # failure, in the same spirit as every other config check here.
    if not cfg.shadow_mode:
        placeholders = sorted(
            pid for pid, person in cfg.people.items() if person.get("placeholder")
        )
        if placeholders:
            problems.append(
                "OPS_SHADOW_MODE is off but routing.yaml still contains placeholder "
                f"people: {', '.join(placeholders)}. Replace them with the real roster "
                "and remove `placeholder: true`, or set OPS_SHADOW_MODE=true."
            )

    if not cfg.codes:
        problems.append("reasons.yaml: no codes defined")

    role_names = set(cfg.roles.keys())
    people = cfg.people
    code_names = {c.get("code") for c in cfg.codes}

    # every ticketable code needs an owner role that exists
    for c in cfg.codes:
        code = c.get("code", "<unnamed>")
        if not code or "." not in code and code not in {
            "power_failure", "shift_change", "short_stop",
        }:
            # namespacing is required so 'electrical' in dyeing != weaving.electrical
            problems.append(f"reasons.yaml: code '{code}' should be namespaced (dept.name)")
        if c.get("ticketable"):
            owner = c.get("owner")
            if not owner:
                problems.append(f"reasons.yaml: ticketable code '{code}' has no owner role")
            elif owner not in role_names:
                problems.append(
                    f"reasons.yaml: code '{code}' owner '{owner}' is not a role in routing.yaml"
                )
        if "expected_minutes" not in c:
            problems.append(f"reasons.yaml: code '{code}' missing expected_minutes")

    # auto_classify codes must exist as codes
    for rule in cfg.auto_classify:
        rc = rule.get("code")
        if rc and rc not in code_names:
            problems.append(f"reasons.yaml: auto_classify references unknown code '{rc}'")
        if "rule" not in rule:
            problems.append(f"reasons.yaml: auto_classify entry for '{rc}' has no rule")

    # routing: roles reference people that exist
    for role, spec in cfg.roles.items():
        buckets = spec.values() if isinstance(spec, dict) else []
        for bucket in buckets:
            for pid in (bucket or []):
                if pid not in people:
                    problems.append(
                        f"routing.yaml: role '{role}' references unknown person '{pid}'"
                    )

    # the default owner must be a real person, or the backstop silently isn't one
    if cfg.default_owner and cfg.default_owner not in people:
        problems.append(
            f"routing.yaml: default_owner '{cfg.default_owner}' is not a person in "
            "routing.yaml"
        )
    if cfg.route_all_to_default and not cfg.default_owner:
        problems.append(
            "routing.yaml: route_all_to_default is on but no default_owner is set"
        )

    # shifts present
    if not cfg.shifts:
        problems.append("routing.yaml: no shifts defined")

    # escalation ladders reference roles that exist ("owner" is a reserved symbolic
    # role meaning the ticket's own owner_role).
    reserved_roles = {"owner"}
    for lad_name, rungs in cfg.ladders.items():
        if not isinstance(rungs, list):
            problems.append(f"escalation.yaml: ladder '{lad_name}' is not a list")
            continue
        for i, rung in enumerate(rungs):
            role = rung.get("notify")
            if role and role not in role_names and role not in reserved_roles:
                problems.append(
                    f"escalation.yaml: ladder '{lad_name}' rung {i} notifies unknown role '{role}'"
                )
            if "after_minutes" not in rung:
                problems.append(
                    f"escalation.yaml: ladder '{lad_name}' rung {i} missing after_minutes"
                )
    if "default" not in cfg.ladders:
        problems.append("escalation.yaml: a 'default' ladder is required")
    if "unknown" not in cfg.ladders:
        problems.append("escalation.yaml: an 'unknown' ladder is required (Phase-1 reason prompt)")

    # the catch-all reason must actually exist, or every prompt offers a dead option
    if cfg.codes and cfg.other_code not in {c.get("code") for c in cfg.codes}:
        problems.append(
            f"reasons.yaml: catch-all code '{cfg.other_code}' is not defined. Add it, "
            "or set defaults.other_code to the code you want appended to every prompt."
        )

    # source adapter named
    if not cfg.source.get("adapter"):
        problems.append("source.yaml: no adapter named")

    if problems:
        raise ConfigError(
            "Configuration is invalid — refusing to start:\n  - "
            + "\n  - ".join(problems)
        )
