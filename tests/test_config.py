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
