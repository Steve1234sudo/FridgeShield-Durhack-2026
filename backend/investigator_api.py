"""Investigator routes shared by the unified backend application."""
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter
from pydantic import BaseModel

# Resolve explicitly so both repository-root and backend-directory launches work.
load_dotenv(Path(__file__).resolve().with_name(".env"))

from .investigator import investigate

router = APIRouter()


class InvestigateRequest(BaseModel):
    question: str
    packet: dict


@router.get("/health")
def health():
    return {"ok": True, "model": os.getenv("GEMINI_MODEL") or "no model set in .env"}


@router.post("/investigate")
async def investigate_endpoint(req: InvestigateRequest):
    return await investigate(req.question, req.packet)
