"""Root, health and status endpoints."""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/")
def read_root():
    return {"message": "Welcome to STORCITO API. Use POST /run-dynamic or POST /run-static to trigger jobs."}


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/status")
def status():
    return {"status": "ok"}
