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
