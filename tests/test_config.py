"""Config validation must fail LOUDLY at boot (a typo must not silently route nothing
to nobody at 3am)."""

import copy

import pytest

from app import config


def test_valid_config_loads():
    c = config.load()
    assert c.department == "weaving"
    assert c.is_ticketable("weaving.electrical")
    assert c.owner_role("weaving.electrical") == "electrician"


def test_bad_owner_role_fails_validation():
    c = config.load()
    bad = copy.deepcopy(c.reasons)
    # typo the electrician owner, exactly the "owner: electricain" case from the doc
    for code in bad["codes"]:
        if code["code"] == "weaving.electrical":
            code["owner"] = "electricain"
    c.reasons = bad
    with pytest.raises(config.ConfigError) as exc:
        config.validate(c)
    assert "electricain" in str(exc.value)


def test_escalation_unknown_role_fails():
    c = config.load()
    bad = copy.deepcopy(c.escalation)
    bad["ladders"]["default"][1]["notify"] = "nonexistent_role"
    c.escalation = bad
    with pytest.raises(config.ConfigError):
        config.validate(c)


def test_missing_unknown_ladder_fails():
    c = config.load()
    bad = copy.deepcopy(c.escalation)
    del bad["ladders"]["unknown"]
    c.escalation = bad
    with pytest.raises(config.ConfigError) as exc:
        config.validate(c)
    assert "unknown" in str(exc.value)


# --- shadow mode --------------------------------------------------------------

def test_shadow_mode_forces_every_channel_to_the_log_notifier(cfg):
    """Shadow mode must not depend on credentials being blank: even with a BSP token
    and a Chat webhook configured, nothing may leave the box."""
    import os
    from app.notifiers import get_notifier, reset_cache
    from app.notifiers.log import LogNotifier

    os.environ["WHATSAPP_BSP_BASE_URL"] = "https://bsp.example.com"
    os.environ["WHATSAPP_BSP_TOKEN"] = "live-token"
    os.environ["GCHAT_WEBHOOK_BASE_URL"] = "https://chat.googleapis.com"
    try:
        cfg.shadow_mode = True
        reset_cache()
        for channel in ("whatsapp", "gchat", "log"):
            assert isinstance(get_notifier(cfg, channel), LogNotifier), channel

        cfg.shadow_mode = False      # live mode picks the real notifiers back up
        reset_cache()
        assert type(get_notifier(cfg, "whatsapp")).__name__ == "WhatsAppNotifier"
        assert type(get_notifier(cfg, "gchat")).__name__ == "GChatNotifier"
    finally:
        for k in ("WHATSAPP_BSP_BASE_URL", "WHATSAPP_BSP_TOKEN", "GCHAT_WEBHOOK_BASE_URL"):
            os.environ.pop(k, None)
        cfg.shadow_mode = True
        reset_cache()


def test_live_mode_with_a_placeholder_roster_refuses_to_start(cfg):
    """The dummy numbers are validly-formatted Indian mobiles. Going live while they
    are still in routing.yaml must be a boot failure, not a text to a stranger."""
    import pytest
    from app import config as appconfig

    cfg.shadow_mode = False
    with pytest.raises(appconfig.ConfigError) as exc:
        appconfig.validate(cfg)
    msg = str(exc.value)
    assert "placeholder" in msg and "ravi_k" in msg
    cfg.shadow_mode = True
    appconfig.validate(cfg)          # shadow mode: same roster validates fine


# --- migrations must survive a half-finished run -------------------------------

def test_migrate_recovers_when_tables_exist_but_bookkeeping_is_missing(cfg):
    """MySQL commits each DDL statement implicitly, so a migration can never be atomic.
    If it dies partway — or the schema was imported by hand via phpMyAdmin — the
    schema_migrations row is never written and the next boot re-runs the file. That
    must be harmless, not `(1050, "Table 'opscore_assets' already exists")`.
    """
    from app import db as appdb

    appdb.migrate()                                   # normal first run
    appdb.execute("DELETE FROM schema_migrations")    # simulate the interrupted run

    applied = appdb.migrate()                         # must not raise
    assert "001_init.sql" in applied
    assert appdb.query_one("SELECT COUNT(*) n FROM schema_migrations")["n"] == 1
    assert appdb.migrate() == []                      # and is idempotent thereafter


def test_exported_schema_records_itself_so_a_hand_import_is_recognised(cfg):
    """deploy/schema.mysql.sql is offered as a phpMyAdmin alternative to letting the app
    migrate. If it doesn't write its own bookkeeping row the two paths disagree and the
    app re-runs the migration on top of the imported tables."""
    from pathlib import Path
    sql = (Path(__file__).resolve().parent.parent / "deploy" / "schema.mysql.sql").read_text()
    assert "schema_migrations" in sql, "export must create the bookkeeping table"
    assert "INSERT IGNORE INTO" in sql and "001_init.sql" in sql, \
        "export must record each migration it contains"
    assert "CREATE TABLE IF NOT EXISTS" in sql, "export must be re-runnable"


# --- department separation ------------------------------------------------------

def test_core_contains_no_domain_vocabulary(cfg):
    """The premise of the design is that a second department is four YAML files. That
    only holds if nothing weaving-specific has leaked into the department-blind layers.
    Docstrings may use looms as illustration; executable lines may not."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    domain = re.compile(r"\b(loom|looms|weaving|weft|warp|picks)\b", re.I)
    layers = ["app/core", "app/notifiers", "app/api"] + [
        "app/config.py", "app/db.py", "app/clock.py", "app/workers.py", "app/main.py"]

    offenders = []
    for target in layers:
        path = root / target
        files = sorted(path.rglob("*.py")) if path.is_dir() else [path]
        for f in files:
            in_doc = False
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.count('"""') == 1:
                    in_doc = not in_doc
                    continue
                if in_doc or stripped.startswith("#") or stripped.startswith('"""'):
                    continue
                code = line.split("#", 1)[0]
                if domain.search(code):
                    offenders.append(f"{f.relative_to(root)}:{i}: {stripped}")

    allowed = ("loom_api_key",)   # back-compat alias for the existing weaving adapter
    offenders = [o for o in offenders if not any(a in o for a in allowed)]
    assert not offenders, "domain vocabulary in department-blind code:\n  " + \
        "\n  ".join(offenders)


def test_catch_all_reason_comes_from_config_not_a_constant(cfg):
    """`OTHER_CODE = "weaving.other"` used to live in core/prompts.py, so a second
    department would have silently recorded its catch-all in weaving's namespace."""
    from app.core import prompts

    assert cfg.other_code == "weaving.other"
    assert prompts.options(cfg)[-1]["code"] == cfg.other_code

    cfg.reasons = dict(cfg.reasons)
    cfg.reasons["defaults"] = {**cfg.defaults, "other_code": "dyeing.other"}
    cfg.reasons["codes"] = cfg.codes + [
        {"code": "dyeing.other", "label": {"en": "Other"}, "expected_minutes": 0}]
    assert prompts.options(cfg)[-1]["code"] == "dyeing.other"
