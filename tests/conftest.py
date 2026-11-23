"""Ignore site-packages built for another Python.

A PYTHONPATH inherited from another Hermes environment (python3.14 here,
pytest on 3.11) makes `import numpy` load C extensions it cannot use.
"""

import re
import sys
import types

import pytest

_OWN = f"python{sys.version_info.major}.{sys.version_info.minor}"
sys.path[:] = [p for p in sys.path if not (m := re.search(r"/python\d+\.\d+/", p)) or m.group(0)[1:-1] == _OWN]


@pytest.fixture
def host_yaml(monkeypatch):
    """Hermes's YAML reader for one test: the real one inside a Hermes
    environment, else PyYAML's safe loader standing in. Never process-wide:
    a stand-in would let a real `hermes_cli.config` import, and the tests must
    never reach a real profile's secrets. Hermes modules a test imported
    through it are dropped again."""

    try:
        import hermes_yaml  # noqa: F401
    except ImportError:
        import yaml

        stand_in = types.ModuleType("hermes_yaml")
        setattr(stand_in, "safe_load", yaml.safe_load)
        monkeypatch.setitem(sys.modules, "hermes_yaml", stand_in)
    before = set(sys.modules)
    yield
    for name in set(sys.modules) - before:
        if name.split(".")[0] in {"hermes_cli", "gateway", "agent"}:
            del sys.modules[name]
