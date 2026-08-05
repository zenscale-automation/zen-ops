"""Source registry. build_source(cfg) instantiates the adapter named in source.yaml.
A hand-written dictionary, not a plugin registry — sufficient for the next three
departments and replaceable in two minutes (design doc 8).
"""

from __future__ import annotations

from .manual import ManualSource
from .weaving_loom_api import WeavingLoomApiSource

_ADAPTERS = {
    "weaving_loom_api": WeavingLoomApiSource,
    "manual": ManualSource,
}


def build_source(cfg):
    name = cfg.source.get("adapter")
    if name not in _ADAPTERS:
        raise ValueError(
            f"unknown source adapter '{name}'. Known: {sorted(_ADAPTERS)}"
        )
    return _ADAPTERS[name](cfg)
