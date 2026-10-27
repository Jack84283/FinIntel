"""Native desktop provider credential API. No read response contains secrets."""

import os
from hmac import compare_digest
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from finintel.settings.connection_test import test_connection
from finintel.settings.credentials import (
    FIELDS,
    CredentialStoreError,
    delete_secret,
    set_secret,
    stored,
)
from finintel.storage.backup import create_backup, inspect_backup, list_backups, restore_backup
from finintel.storage.db import StorageError

router = APIRouter(prefix="/api/v1/settings", tags=["desktop-settings"])


class SecretInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = Field(min_length=1, max_length=4096)


def _require_managed(request: Request) -> None:
    token = os.getenv("FININTEL_PROCESS_TOKEN", "")
    if not token or not compare_digest(request.headers.get("x-finintel-process-token", ""), token):
        raise HTTPException(status_code=403, detail="Managed desktop required")


@router.get("/providers")
def provider_status() -> dict[str, dict[str, bool]]:
    try:
        return {
            provider: {field: stored(provider, field) for field in fields}
            for provider, fields in FIELDS.items()
        }
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.put("/providers/{provider}/{field}")
def save_provider_secret(
    provider: str, field: str, payload: SecretInput, request: Request
) -> dict[str, bool]:
    _require_managed(request)
    try:
        set_secret(provider, field, payload.value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"configured": True}


@router.delete("/providers/{provider}/{field}")
def remove_provider_secret(provider: str, field: str, request: Request) -> dict[str, bool]:
    _require_managed(request)
    try:
        delete_secret(provider, field)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"configured": False}


@router.post("/providers/{provider}/test")
def test_provider_connection(provider: str, request: Request) -> dict:
    """Explicit read-only external provider request; no price or metadata persistence."""
    _require_managed(request)
    if provider not in FIELDS:
        raise HTTPException(status_code=422, detail="Unsupported provider")
    try:
        return test_connection(provider)
    except CredentialStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class RestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backup_id: str = Field(min_length=1, max_length=120)
    mode: Literal["merge", "replace"]


@router.get("/backups")
def database_backups(request: Request) -> dict:
    _require_managed(request)
    return {"items": list_backups()}


@router.post("/backups")
def database_backup_now(request: Request) -> dict:
    _require_managed(request)
    try:
        return create_backup()
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/backups/{backup_id}")
def database_backup_preview(backup_id: str, request: Request) -> dict:
    _require_managed(request)
    try:
        return inspect_backup(backup_id)
    except StorageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/backups/restore")
def database_restore(payload: RestoreRequest, request: Request) -> dict:
    _require_managed(request)
    try:
        return restore_backup(payload.backup_id, payload.mode)
    except (StorageError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
