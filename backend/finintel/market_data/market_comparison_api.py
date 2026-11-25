"""Bounded structured read. Managed process authentication is handled by the app."""

import sqlite3

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from finintel.market_data.market_comparison import (
    PATH,
    ComparisonError,
    ComparisonRequest,
    ComparisonResponse,
    run_market_comparison,
)
from finintel.storage.db import StorageError

router = APIRouter(tags=["local-analytics"])


@router.post(PATH, response_model=ComparisonResponse)
async def compare_market(request: Request) -> Response:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > 4096:
            raise HTTPException(status_code=413, detail="resource_limit")
        body.extend(chunk)
    try:
        selection = ComparisonRequest.model_validate_json(body)
        result = await run_in_threadpool(run_market_comparison, selection)
        encoded = result.model_dump_json().encode()
        if len(encoded) > 4 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="resource_limit")
        return Response(content=encoded, media_type="application/json")
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="invalid_input") from exc
    except ComparisonError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (StorageError, sqlite3.Error) as exc:
        raise HTTPException(status_code=503, detail="backend_unavailable") from exc
