"""Regression: the desktop backend must start even without optional calendar imports."""

import builtins
import importlib

from fastapi.testclient import TestClient


def test_backend_import_does_not_require_exchange_calendars(monkeypatch):
    real_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == "exchange_calendars" or name.startswith("exchange_calendars."):
            raise ModuleNotFoundError("No module named 'exchange_calendars'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked_import)
    from finintel import main

    importlib.reload(main)
    response = TestClient(main.app).get("/openapi.json")
    assert response.status_code == 200
    assert "/api/v1/validation/status" in response.json()["paths"]
