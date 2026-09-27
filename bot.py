"""bot.py — Submission entrypoint for the magicpin AI Challenge judge.

Re-exports the FastAPI app from app.main so the judge can run:
    uvicorn bot:app --host 0.0.0.0 --port 8080
"""

from app.main import app

__all__ = ["app"]
