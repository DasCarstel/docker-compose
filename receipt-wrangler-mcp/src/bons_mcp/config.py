"""Konfiguration des bons-mcp Servers (fail-closed ohne Secrets)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Server kann ohne zwingende Konfiguration nicht starten."""


@dataclass(frozen=True)
class Settings:
    """Start-Einstellungen des Servers."""

    base_url: str
    rw_api_token: str
    mcp_auth_tokens: tuple[str, ...]
    request_timeout: float = 30.0
    image_timeout: float = 60.0
    max_attempts: int = 2


def parse_tokens(raw: str) -> tuple[str, ...]:
    """Kommagetrennte Token-Liste in Tupel zerlegen (Leereinträge ignoriert)."""
    return tuple(t.strip() for t in raw.split(",") if t.strip())


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Settings aus Environment lesen.

    Wirft :class:`ConfigError` (exit != 0 beim Start, siehe server.py), wenn
    `RW_API_TOKEN` oder `MCP_AUTH_TOKENS` fehlen - der Server startet dann
    bewusst nicht (fail closed).
    """
    env = dict(environ if environ is not None else os.environ)

    base_url = (env.get("RW_BASE_URL") or "http://receipt-wrangler:80").strip().rstrip("/")
    if not base_url:
        raise ConfigError("RW_BASE_URL ist leer. Bitte die Receipt-Wrangler-Basis-URL setzen.")

    rw_api_token = (env.get("RW_API_TOKEN") or "").strip()
    if not rw_api_token:
        raise ConfigError(
            "RW_API_TOKEN fehlt oder ist leer. Der Server startet ohne Receipt-Wrangler-API-Key "
            "bewusst NICHT (fail closed). Bitte einen read-only API-Key aus Receipt Wrangler "
            "setzen (Einstellungen > API-Keys, Scope 'r')."
        )

    mcp_auth_tokens = parse_tokens(env.get("MCP_AUTH_TOKENS") or "")
    if not mcp_auth_tokens:
        raise ConfigError(
            "MCP_AUTH_TOKENS fehlt oder ist leer. Der Server startet ohne mindestens einen "
            "Bearer-Token für den MCP-Zugang bewusst NICHT (fail closed). Kommagetrennte Liste "
            "in MCP_AUTH_TOKENS setzen."
        )

    timeouts_OK = _positive(env.get("BONS_REQUEST_TIMEOUT"), "BONS_REQUEST_TIMEOUT", 30.0)
    image_timeout = _positive(env.get("BONS_IMAGE_TIMEOUT"), "BONS_IMAGE_TIMEOUT", 60.0)

    return Settings(
        base_url=base_url,
        rw_api_token=rw_api_token,
        mcp_auth_tokens=mcp_auth_tokens,
        request_timeout=timeouts_OK,
        image_timeout=image_timeout,
    )


def _positive(raw: str | None, name: str, default: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} ist keine Zahl: {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} muss > 0 sein.")
    return value
