"""Everything the plugin imports comes with Python, with Hermes, or is declared."""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Hermes's own modules and its core dependencies.
HERMES = {"agent", "gateway", "hermes_cli", "hermes_constants", "hermes_yaml", "fastapi", "websockets"}
#: Hermes modules its plugin API documents (hermes_cli/data/plugin_api.json).
PUBLIC = {"agent.secret_scope", "gateway.config", "gateway.platform_registry", "gateway.session_context",
          "hermes_cli.config", "hermes_constants", "hermes_yaml"}
#: The two seams Hermes has no public surface for yet, each imported once and
#: declared in the README and the catalog submission (catalog rule 9).
SEAMS = [("gateway.run", "_async_profile_runtime_scope"), ("gateway.run", "_gateway_runner_ref")]
#: Brought by Hermes's Discord platform; only the Discord half imports them, inside functions.
DISCORD = {"discord", "aiohttp"}


def sources() -> list[Path]:
    return sorted([ROOT / "__init__.py", *(ROOT / "talk_with_hermes").glob("*.py"), *(ROOT / "dashboard").glob("*.py")])


def roots(nodes) -> list[str]:
    out = []
    for node in nodes:
        if isinstance(node, ast.Import):
            out += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append(node.module.split(".")[0])
    return out


def declared() -> set[str]:
    from hermes_yaml import safe_load

    manifest = safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8")) or {}
    return {re.split(r"[<>=!~\[ ;]", spec, maxsplit=1)[0].lower() for spec in manifest.get("python_dependencies") or []}


def test_every_import_is_pythons_hermess_or_declared(host_yaml):
    allowed = set(sys.stdlib_module_names) | HERMES | DISCORD | declared() | {"talk_with_hermes"}
    stray = {}
    for path in sources():
        for name in roots(ast.walk(ast.parse(path.read_text(encoding="utf-8")))):
            if name not in allowed:
                stray.setdefault(name, set()).add(path.name)
    assert stray == {}


def test_private_hermes_imports_are_the_two_declared_seams():
    found = []
    for path in sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found += [(alias.name, "") for alias in node.names
                          if alias.name.split(".")[0] in HERMES - {"fastapi", "websockets"} and alias.name not in PUBLIC]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] in HERMES - {"fastapi", "websockets"} and node.module not in PUBLIC:
                    found += [(node.module, alias.name) for alias in node.names]
    assert sorted(found) == SEAMS


def test_the_desktop_half_loads_without_discord():
    for path in sources():
        top = roots(ast.parse(path.read_text(encoding="utf-8")).body)
        assert not DISCORD & set(top), path.name


def test_dependencies_have_an_upper_bound(host_yaml):
    from hermes_yaml import safe_load

    manifest = safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8")) or {}
    for spec in manifest.get("python_dependencies") or []:
        assert "<" in spec, spec
