"""Nothing private ships in this repository: no host path, no process id, no
Discord id, no e-mail address. Names cannot be listed here without publishing
them, so this guard covers what a pattern can catch."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
PRIVATE = {
    "host path": re.compile(r"(?<![\w.])/(?:opt|root|home|srv)/[\w.-]+"),
    "process id": re.compile(r"\bPID\s*[0-9]{2,}"),
    "Discord id": re.compile(r"(?<![\w.])[0-9]{17,20}(?![\w.])"),
    "e-mail address": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]*[a-z]\b"),
}


def shipped() -> list[Path]:
    here = Path(__file__).resolve()
    return sorted(p for p in ROOT.rglob("*")
                  if p.is_file() and p != here and not SKIP & set(p.relative_to(ROOT).parts))


def test_no_private_detail_in_any_shipped_file():
    found = []
    for path in shipped():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            found += [f"{path.relative_to(ROOT)}:{n}: {kind}" for kind, rx in PRIVATE.items() if rx.search(line)]
    assert found == []
