"""Hermes loads this file by path. Keep the package import local to this folder."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from talk_with_hermes import register

__all__ = ["register"]
