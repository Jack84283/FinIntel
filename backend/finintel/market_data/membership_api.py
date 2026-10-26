"""Read-only, point-in-time index constituent API for the local workstation."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from finintel.market_data.membership import (
    index_constituents,
    list_indexes,
    symbol_index_memberships,
)
from finintel.storage.db import StorageError

router = APIRouter(prefix="/api/v1/market-data/indexes", tags=["index-membership"])
IndexCode = Literal["SP500", "DJIA", "NDX", "RUT"]


class IndexRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: IndexCode
    name: str
    requested_as_of: str
    snapshot_as_of: str | None
    source: str | None
    members: int | None
    status: Literal["available", "not_imported"]


class Constituent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    name: str
    asset_type: str
    member_indexes: list[IndexCode]


class ConstituentPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: IndexCode
    name: str
    requested_as_of: str
    snapshot_as_of: str | None
    source: str | None
    snapshot_members: int | None
    total: int
    limit: int
    offset: int
    items: list[Constituent]


class SymbolIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: IndexCode
    name: str
    requested_as_of: str
    snapshot_as_of: str | None
    source: str | None
    members: int | None
    status: Literal["available", "not_imported"]
    is_member: bool | None


class SymbolIndexes(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    requested_as_of: str
    indexes: list[SymbolIndex]


@router.get("", response_model=list[IndexRecord])
def get_indexes(as_of: date | None = None) -> list[dict]:
    try:
        return list_indexes(as_of=as_of.isoformat() if as_of else None)
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/symbol/{symbol}/memberships", response_model=SymbolIndexes)
def get_symbol_indexes(symbol: str, as_of: date | None = None) -> dict:
    try:
        return symbol_index_memberships(symbol, as_of=as_of.isoformat() if as_of else None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/{code}/constituents", response_model=ConstituentPage)
def get_index_constituents(
    code: IndexCode,
    as_of: date | None = None,
    q: str = Query(default="", max_length=80),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    try:
        return index_constituents(
            code, as_of=as_of.isoformat() if as_of else None,
            query=q, limit=limit, offset=offset
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except StorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
