"""Unified backend: python -m backend.main from the repository root.

Also supports uvicorn main:app from inside backend/.
Both this entry point and backend.live expose the same application.
"""
if __package__:
    from .live import app
else:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.live import app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
