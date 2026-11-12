"""Entry point (app layer): exposes `app` for `uvicorn backend.run:app` and runs it for `python backend/run.py`."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT") or settings.port), log_level=settings.log_level.lower())
