"""OS credential-vault access: no secrets in SQLite or frontend read responses."""

import os

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

SERVICE = "app.finintel.desktop.providers"
FIELDS = {
    "alpha_vantage": ("api_key",),
    "alpaca_sip": ("key_id", "secret_key"),
    "tiingo": ("api_key",),
    "equibles": ("api_key",),
}
ENV_FALLBACK = {
    ("alpha_vantage", "api_key"): "FININTEL_ALPHA_VANTAGE_API_KEY",
    ("alpaca_sip", "key_id"): "FININTEL_ALPACA_API_KEY_ID",
    ("alpaca_sip", "secret_key"): "FININTEL_ALPACA_API_SECRET_KEY",
    ("tiingo", "api_key"): "FININTEL_TIINGO_API_KEY",
    ("equibles", "api_key"): "FININTEL_EQUIBLES_API_KEY",
}


class CredentialStoreError(RuntimeError):
    """No usable secure OS credential service."""


def _name(provider: str, field: str) -> str:
    if field not in FIELDS.get(provider, ()):
        raise ValueError("Unsupported credential provider or field")
    return f"{provider}.{field}"


def stored(provider: str, field: str) -> bool:
    try:
        return bool(keyring.get_password(SERVICE, _name(provider, field)))
    except KeyringError as exc:
        raise CredentialStoreError("OS secure credential storage unavailable") from exc


def get_secret(provider: str, field: str) -> str:
    """OS vault primary; environment fallback only for developer-run CLI."""
    name = _name(provider, field)
    try:
        value = keyring.get_password(SERVICE, name)
        if value:
            return value.strip()
    except KeyringError:
        value = None
    if os.getenv("FININTEL_PROCESS_TOKEN"):
        return ""
    return os.environ.get(ENV_FALLBACK[(provider, field)], "").strip()


def set_secret(provider: str, field: str, secret: str) -> None:
    name = _name(provider, field)
    value = secret.strip()
    if not 1 <= len(value) <= 4096 or any(ord(ch) < 32 for ch in value):
        raise ValueError("Invalid secret characters or length")
    try:
        keyring.set_password(SERVICE, name, value)
    except KeyringError as exc:
        raise CredentialStoreError("OS secure credential storage unavailable") from exc


def delete_secret(provider: str, field: str) -> None:
    name = _name(provider, field)
    try:
        keyring.delete_password(SERVICE, name)
    except PasswordDeleteError:
        pass
    except Exception as exc:
        raise CredentialStoreError("OS secure credential storage unavailable") from exc
