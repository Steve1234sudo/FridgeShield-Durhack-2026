"""FridgeShield backend.

Run from the backend folder, with the venv active:
    uvicorn main:app --reload
Then open http://localhost:8000/docs to try the endpoints.
"""
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

load_dotenv()  # reads backend/.env

from investigator import investigate  # noqa: E402  (needs the .env loaded first)

app = FastAPI(title="FridgeShield backend")

# Let the Vite dev server (port 5173) call this server (port 8000).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class InvestigateRequest(BaseModel):
    question: str
    packet: dict


@app.get("/health")
def health():
    return {"ok": True, "model": os.getenv("GEMINI_MODEL") or "no model set in .env"}


@app.post("/investigate")
async def investigate_endpoint(req: InvestigateRequest):
    return await investigate(req.question, req.packet)