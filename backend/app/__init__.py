"""Penny: a cashback micro-investing web app. The model proposes; deterministic rules decide."""
import sys
from pathlib import Path

# Lets `python -m backend.app.seed` (run from the repo root) resolve the absolute `app.*` imports.
_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)
