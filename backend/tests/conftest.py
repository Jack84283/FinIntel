"""Hermetic test defaults for credentials and other host-owned state."""

import keyring
import pytest


@pytest.fixture(autouse=True)
def isolate_os_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a developer's real vault credentials affect test outcomes."""
    monkeypatch.setattr(keyring, "get_password", lambda _service, _name: None)
    monkeypatch.setattr(keyring, "set_password", lambda _service, _name, _value: None)
    monkeypatch.setattr(keyring, "delete_password", lambda _service, _name: None)
