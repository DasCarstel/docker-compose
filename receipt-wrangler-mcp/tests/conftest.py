"""Gemeinsame Test-Fixtures: Env-Variablen VOR dem Import setzen (fail-closed)."""

from __future__ import annotations

import os
import pathlib
import sys

import pytest
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING


SERVICE_DIR = pathlib.Path(__file__).resolve().parent.parent
SRC_DIR = SERVICE_DIR / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Der Test-Modulkörper importiert bons_mcp.server TOP-LEVEL - das passiert beim
# Sammeln, also noch VOR jedem Fixture. Damit der fail-closed-Import dort nicht
# bereits abbricht, muessen die Secrets bereits hier (Conftest-Importzeit) stehen.
# Es sind nur Test-Dummy-Werte; die Tests ueberschreiben bei Bedarf per monkeypatch.
os.environ.setdefault("RW_BASE_URL", "http://receipt-wrangler:80")
os.environ.setdefault("RW_API_TOKEN", "rw-secret-token")
os.environ.setdefault("MCP_AUTH_TOKENS", "mcp-client-token,second-token")

if TYPE_CHECKING:
    from bons_mcp.config import Settings


# ---------------------------------------------------------------------------
# Async-Support: Tests sind async, aber ohne asyncio/anyio-Marker definiert.
# Der mitgelieferte anyio-Plugin-Pfad wird hier zentral angeschlossen, OHNE
# etwas an den Tests zu aendern (asyncio-Backend, assertive Detect-Deadlock off).
# ---------------------------------------------------------------------------
@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Alle async-Tests automatisch mit dem anyio-Marker versehen."""
    import inspect

    for item in items:
        test_fn = getattr(item, "function", None)
        if test_fn is not None and inspect.iscoroutinefunction(test_fn) and "anyio" not in item.keywords:
            item.add_marker(pytest.mark.anyio)


@pytest.fixture()
def env_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Erforderliche Secrets setzen, damit bons_mcp.server importierbar ist."""
    monkeypatch.setenv("RW_BASE_URL", "http://receipt-wrangler:80")
    monkeypatch.setenv("RW_API_TOKEN", "rw-secret-token")
    monkeypatch.setenv("MCP_AUTH_TOKENS", "mcp-client-token,second-token")


@pytest.fixture()
def settings(env_secrets: None) -> Iterator["Settings"]:
    from bons_mcp.config import load_settings

    yield load_settings()


@pytest.fixture()
def app_module(env_secrets: None) -> Iterator[Callable[[], object]]:
    """bons_mcp.server mit gesetzten Secrets importieren (App ist gebaut)."""
    import bons_mcp.server as server

    yield server
