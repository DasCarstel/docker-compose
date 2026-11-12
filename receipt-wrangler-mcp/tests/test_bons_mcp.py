"""Tests für bons_mcp: Tool-Mapping, Bild-Download, Fehler, Auth, Fail-Closed.

Alles ohne Netz: httpx.MockTransport für die Receipt-Wrangler-Seite,
httpx.ASGITransport für die ASGI-App (Health/Auth/MCP-Protokoll). Der
Session-Manager der Streamable-HTTP-App wird pro Test-Kontext per
`app.__dict__["bons_mcp"].session_manager.run()` gestartet (was die App-Lifespan
normalerweise macht).
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from base64 import b64encode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Callable

import httpx
import pytest

from bons_mcp.client import RWClient, RWError, build_receipt_request, compact_receipt, compact_search_result
from bons_mcp.config import ConfigError, load_settings
from bons_mcp.server import _sniff_content_kind, build_mcp, create_app

SERVICE_DIR = pathlib.Path(__file__).resolve().parent.parent
SRC_DIR = SERVICE_DIR / "src"


# ---------------------------------------------------------------------------
# Hilfs-Transports
# ---------------------------------------------------------------------------
PAGED_RESPONSE: dict[str, Any] = {
    "totalCount": 3,
    "data": [
        {
            "id": 11,
            "name": "Rewe Einkauf",
            "amount": "42.13",
            "date": "2026-11-01",
            "status": "RESOLVED",
            "paidByUserId": 2,
            "categories": [{"id": 1, "name": "Groceries"}],
            "tags": [{"id": 3, "name": "Haushalt"}],
            "imageFiles": [{"id": 7, "name": "a.jpg", "fileType": "jpg"}],
            "receiptItems": [{"id": 99, "name": "DarfInKompaktNichtAuftauchen"}],
            "firstComment": "Bitte prüfen",
        },
        {
            "id": 12,
            "name": "No Image Bon",
            "amount": "1.50",
            "date": "2026-11-02",
            "status": "OPEN",
            "paidByUserId": 2,
            "categories": [],
            "tags": [],
            "imageFiles": [],
        },
    ],
}


def json_response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("GET", "http://x"))


def make_client(settings: Any, handler: Callable[[httpx.Request], httpx.Response]) -> RWClient:
    return RWClient(settings, transport=httpx.MockTransport(handler))


def asgi_client(app: Any) -> httpx.AsyncClient:
    # Host "localhost": der MCP-Transport validiert den Host-Header (DNS-Rebinding-
    # Schutz) und akzeptiert nur die Server-Allowlist - localhost gehoert dazu.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8840")


@asynccontextmanager
async def running_app(settings: Any) -> AsyncIterator[tuple[Any, httpx.AsyncClient]]:
    """ASGI-App mit aktivem MCP-Session-Manager (wie die echte Lifespan)."""
    app = create_app(settings)
    fast_mcp = app.__dict__["bons_mcp"]
    async with fast_mcp.session_manager.run():
        async with asgi_client(app) as client:
            yield app, client


def patch_rw(handler: Callable[..., httpx.Response]) -> Callable[[], Callable[[], None]]:
    """RWClient in bons_mcp.server auf MockTransport umbiegen; gibt Undo zurück."""
    import bons_mcp.server as server

    original = server.RWClient

    class PatchedRWClient(original):  # type: ignore[misc,valid-type]
        def __init__(self, s: Any, transport: Any = None) -> None:
            super().__init__(s, transport=httpx.MockTransport(handler))

    server.RWClient = PatchedRWClient

    def undo() -> None:
        server.RWClient = original

    return undo


MCP_HEADERS = {
    "Authorization": "Bearer mcp-client-token",
    "Accept": "application/json, text/event-stream",
}


async def mcp_call(
    client: httpx.AsyncClient, method: str, params: dict[str, Any] | None, request_id: int = 1
) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    response = await client.post("/mcp", json=payload, headers=MCP_HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# build_receipt_request / Kompaktierung
# ---------------------------------------------------------------------------
class TestListReceipts:
    async def test_maps_request_body_and_compacts_response(self, settings: Any) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content.decode())
            return json_response(PAGED_RESPONSE)

        body = build_receipt_request(
            page=2,
            page_size=50,
            order_by="date",
            sort_direction="desc",
            status="OPEN",
            start_date="2026-01-01",
            end_date="2026-03-31",
        )
        assert body == {
            "page": 2,
            "pageSize": 50,
            "orderBy": "date",
            "sortDirection": "desc",
            "filter": {
                "status": {"operation": "CONTAINS", "value": ["OPEN"]},
                "date": {"operation": "BETWEEN", "value": ["2026-01-01", "2026-03-31"]},
            },
        }

        async with make_client(settings, handler) as client:
            payload = await client.list_receipts(5, body)

        assert captured["url"].endswith("/api/receipt/group/5")
        assert captured["body"] == body

        data = [compact_receipt(r) for r in payload["data"]]
        first = data[0]
        assert first["id"] == 11
        assert first["categories"] == ["Groceries"]
        assert first["tags"] == ["Haushalt"]
        assert first["imageCount"] == 1
        assert first["status"] == "RESOLVED"
        assert "receiptItems" not in first
        assert "receiptItems" not in json.dumps(data)
        assert payload["totalCount"] == 3
        assert data[1]["imageCount"] == 0

    async def test_single_date_bounds_use_between(self) -> None:
        start_only = build_receipt_request(1, 25, "date", "desc", None, "2026-05-01", None)
        assert start_only["filter"]["date"] == {
            "operation": "BETWEEN",
            "value": ["2026-05-01", "9999-12-31"],
        }
        end_only = build_receipt_request(1, 25, "date", "desc", None, None, "2026-06-30")
        assert end_only["filter"]["date"] == {
            "operation": "BETWEEN",
            "value": ["0001-01-01", "2026-06-30"],
        }

    async def test_no_filter_when_nothing_given(self) -> None:
        body = build_receipt_request(1, 25, "date", "desc", None, None, None)
        assert body == {"page": 1, "pageSize": 25, "orderBy": "date", "sortDirection": "desc"}


# ---------------------------------------------------------------------------
# get_receipt ID-Durchreichung
# ---------------------------------------------------------------------------
class TestGetReceipt:
    async def test_passes_id_through_and_returns_data(self, settings: Any) -> None:
        receipt = {
            "id": 77,
            "name": "Detail Bon",
            "amount": "9.99",
            "date": "2026-10-31",
            "status": "OPEN",
            "paidByUserId": 1,
            "receiptItems": [{"id": 1, "name": "Item", "quantity": 1, "lineTotal": "9.99"}],
            "comments": [{"id": 4, "comment": "hallo"}],
            "customFields": [{"id": 8, "value": "abc"}],
            "imageFiles": [{"id": 42, "name": "bon.jpg", "fileType": "jpg"}],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "GET"
            assert str(request.url).endswith("/api/receipt/77")
            return json_response(receipt)

        async with make_client(settings, handler) as client:
            data = await client.get_receipt(77)

        assert data["id"] == 77
        assert data["receiptItems"][0]["name"] == "Item"


# ---------------------------------------------------------------------------
# Bild-Download: Bytes und Typ-Erkennung
# ---------------------------------------------------------------------------
class TestImageDownloadClient:
    async def test_jpeg_bytes_with_blind_header_get_sniffed(self, settings: Any) -> None:
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 16

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url).endswith("/api/receiptImage/42/download")
            return httpx.Response(
                200, content=jpeg, headers={"content-type": "application/octet-stream"}
            )

        async with make_client(settings, handler) as client:
            data, ctype = await client.download_receipt_image(42, timeout=settings.image_timeout)

        assert data == jpeg
        assert ctype == "application/octet-stream"
        assert _sniff_content_kind(data, ctype) == "jpeg"

    async def test_png_magic_bytes(self, settings: Any) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
        async with make_client(settings, lambda req: httpx.Response(200, content=png)) as client:
            data, _ = await client.download_receipt_image(43, timeout=60.0)
        assert _sniff_content_kind(data, "application/octet-stream") == "png"

    async def test_http_content_type_pdf(self, settings: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"%PDF-1.7 rest", headers={"content-type": "application/pdf"}
            )

        async with make_client(settings, handler) as client:
            data, ctype = await client.download_receipt_image(44, timeout=60.0)
        assert _sniff_content_kind(data, ctype) == "pdf"


# ---------------------------------------------------------------------------
# MCP-Protokoll über die echte ASGI-App (Streamable HTTP, stateless)
# ---------------------------------------------------------------------------
class TestMcpProtocol:
    async def test_tools_list_contains_all_seven(self, settings: Any) -> None:
        async with running_app(settings) as (_, client):
            result = await mcp_call(client, "tools/list", {})
        names = [t["name"] for t in result["result"]["tools"]]
        assert names == [
            "list_groups",
            "list_receipts",
            "get_receipt",
            "search_receipts",
            "get_receipt_image",
            "list_categories_and_tags",
            "set_receipt_status",
        ]

    async def test_installed_tools_on_mcp_instance(self, settings: Any) -> None:
        mcp = build_mcp(settings)
        names = {t.name for t in await mcp.list_tools()}
        assert names == {
            "list_groups",
            "list_receipts",
            "get_receipt",
            "search_receipts",
            "get_receipt_image",
            "list_categories_and_tags",
            "set_receipt_status",
        }

    async def test_get_receipt_via_tools_call(self, settings: Any) -> None:
        receipt = {
            "id": 88, "name": "ViaMCP", "amount": "3.20", "date": "2026-11-01",
            "status": "OPEN", "paidByUserId": 1, "receiptItems": [],
            "comments": [], "customFields": [], "imageFiles": [],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url).endswith("/api/receipt/88")
            assert request.headers["authorization"] == "rw-secret-token"  # Raw-Key, ohne Bearer
            return json_response(receipt)

        undo = patch_rw(handler)
        try:
            async with running_app(settings) as (_, client):
                result = await mcp_call(
                    client, "tools/call",
                    {"name": "get_receipt", "arguments": {"receipt_id": 88}},
                )
        finally:
            undo()

        content = result["result"]["content"][0]
        assert content["type"] == "text"
        parsed = json.loads(content["text"])
        assert parsed["id"] == 88
        assert parsed["name"] == "ViaMCP"

    async def test_search_maps_endpoint_and_compacts(self, settings: Any) -> None:
        search_response = [
            {"id": 5, "name": "Rewe Bon", "type": "receipt", "groupId": 2,
             "date": "2026-10-01", "amount": "12.34", "receiptStatus": "OPEN",
             "paidByUserId": 3, "createdAt": "2026-10-01"}
        ]
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return json_response(search_response)

        undo = patch_rw(handler)
        try:
            async with running_app(settings) as (_, client):
                result = await mcp_call(
                    client, "tools/call",
                    {"name": "search_receipts", "arguments": {"query": "Rewe"}},
                )
        finally:
            undo()

        assert "/api/search/" in captured["url"]
        assert "searchTerm=Rewe" in captured["url"]
        text = result["result"]["content"][0]["text"]
        parsed = json.loads(text)
        assert parsed["count"] == 1
        assert parsed["data"][0] == compact_search_result(search_response[0])
        assert parsed["data"][0]["groupId"] == 2
        # Kompaktform: keine Rohdaten-Felder aus der Suche
        assert "createdAt" not in parsed["data"][0]

    async def test_get_receipt_image_returns_image_content(self, settings: Any) -> None:
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 32

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=jpeg, headers={"content-type": "image/jpeg"})

        undo = patch_rw(handler)
        try:
            async with running_app(settings) as (_, client):
                result = await mcp_call(
                    client, "tools/call",
                    {"name": "get_receipt_image", "arguments": {"receipt_image_id": 7}},
                )
        finally:
            undo()

        contents = result["result"]["content"]
        image_contents = [c for c in contents if c["type"] == "image"]
        assert image_contents, f"expected image content, got: {result}"
        assert image_contents[0]["mimeType"] == "image/jpeg"
        assert b64encode(jpeg).decode() == image_contents[0]["data"]

    async def test_get_receipt_image_pdf_returns_text_only(self, settings: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=b"%PDF-1.4 ...", headers={"content-type": "application/pdf"}
            )

        undo = patch_rw(handler)
        try:
            async with running_app(settings) as (_, client):
                result = await mcp_call(
                    client, "tools/call",
                    {"name": "get_receipt_image", "arguments": {"receipt_image_id": 9}},
                )
        finally:
            undo()

        contents = result["result"]["content"]
        assert all(c["type"] == "text" for c in contents), contents
        text = contents[0]["text"]
        assert "PDF" in text
        assert "receiptImage/9/download" in text
        assert "rw-secret-token" not in text

    async def test_list_categories_and_tags(self, settings: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            path = httpx.URL(str(request.url)).path
            if path.endswith("/api/category/"):
                return json_response([{"id": 1, "name": "Lebensmittel", "description": "d"}])
            assert path.endswith("/api/tag/")
            return json_response([{"id": 2, "name": "Familie", "description": "d"}])

        undo = patch_rw(handler)
        try:
            async with running_app(settings) as (_, client):
                result = await mcp_call(
                    client, "tools/call",
                    {"name": "list_categories_and_tags", "arguments": {}},
                )
        finally:
            undo()

        parsed = json.loads(result["result"]["content"][0]["text"])
        assert parsed["categories"] == [{"id": 1, "name": "Lebensmittel"}]
        assert parsed["tags"] == [{"id": 2, "name": "Familie"}]


# ---------------------------------------------------------------------------
# Fehlerabbildung: 401 vom RW, Retries
# ---------------------------------------------------------------------------
class TestErrors:
    async def test_unauthorized_message_without_token_leak(self, settings: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["authorization"] == "rw-secret-token"  # Raw-Key, ohne Bearer
            return json_response({"error": "unauthorized"}, status=401)

        async with make_client(settings, handler) as client:
            with pytest.raises(RWError) as exc_info:
                await client.get_receipt(1)

        message = str(exc_info.value)
        assert "401" in message
        assert "rw-secret-token" not in message
        assert exc_info.value.status == 401

    async def test_error_snippet_is_redacted(self, settings: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.dumps({"echo": request.headers.get("authorization")}).encode()
            return httpx.Response(400, content=body, headers={"content-type": "application/json"})

        async with make_client(settings, handler) as client:
            with pytest.raises(RWError) as exc_info:
                await client.list_receipts(1, {"page": 1, "pageSize": 25})

        assert "rw-secret-token" not in str(exc_info.value)

    async def test_retry_on_5xx_only(self, settings: Any) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return json_response({"ok": True}, status=503)

        async with make_client(settings, handler) as client:
            with pytest.raises(RWError):
                await client.list_receipts(1, {"page": 1, "pageSize": 25})

        assert calls["n"] == settings.max_attempts

    async def test_no_retry_on_4xx(self, settings: Any) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return json_response({}, status=418)

        async with make_client(settings, handler) as client:
            with pytest.raises(RWError):
                await client.list_receipts(1, {"page": 1, "pageSize": 25})

        assert calls["n"] == 1

    async def test_network_error_exhausts_attempts(self, settings: Any) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ConnectError("boom", request=request)

        async with make_client(settings, handler) as client:
            with pytest.raises(RWError) as exc_info:
                await client.get_receipt(2)

        assert calls["n"] == settings.max_attempts
        assert "nicht erreichbar" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Tool 7: set_receipt_status (Client-Endpunkt + Tool-Logik)
# ---------------------------------------------------------------------------
BULK_UPDATE_RESPONSE: list[dict[str, Any]] = [
    {
        "id": 18,
        "name": "Rewe Einkauf",
        "amount": "42.13",
        "date": "2026-11-01",
        "status": "RESOLVED",
        "resolvedDate": "2026-11-10",
        "paidByUserId": 2,
        "categories": [{"id": 1, "name": "Groceries"}],
        "tags": [],
        "imageFiles": [],
        "receiptItems": [{"id": 99, "name": "DarfInKompaktNichtAuftauchen"}],
    }
]


class TestBulkUpdateStatusClient:
    async def test_posts_bulk_status_update_with_body(self, settings: Any) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content.decode())
            return json_response(BULK_UPDATE_RESPONSE)

        async with make_client(settings, handler) as client:
            data = await client.bulk_update_status([18], "RESOLVED", "erledigt")

        assert captured["url"].endswith("/api/receipt/bulkStatusUpdate")
        assert captured["body"] == {
            "status": "RESOLVED",
            "receiptIds": [18],
            "comment": "erledigt",
        }
        assert data == BULK_UPDATE_RESPONSE

    async def test_omits_comment_key_when_none(self, settings: Any) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return json_response(BULK_UPDATE_RESPONSE)

        async with make_client(settings, handler) as client:
            await client.bulk_update_status([1, 2], "NEEDS_ATTENTION", None)

        assert captured["body"] == {"status": "NEEDS_ATTENTION", "receiptIds": [1, 2]}

    async def test_403_write_scope_rejected_without_token_leak(self, settings: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response({"errorMsg": "Unauthorized"}, status=403)

        async with make_client(settings, handler) as client:
            with pytest.raises(RWError) as exc_info:
                await client.bulk_update_status([18], "RESOLVED")

        message = str(exc_info.value)
        assert "403" in message
        assert "rw-secret-token" not in message
        assert exc_info.value.status == 403


class TestSetReceiptStatusTool:
    async def test_success_returns_compact_summary(self, settings: Any) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return json_response(BULK_UPDATE_RESPONSE)

        undo = patch_rw(handler)
        try:
            async with running_app(settings) as (_, client):
                result = await mcp_call(
                    client, "tools/call",
                    {
                        "name": "set_receipt_status",
                        "arguments": {"receipt_ids": [18], "status": "RESOLVED", "comment": "ok"},
                    },
                )
        finally:
            undo()

        assert captured["body"]["receiptIds"] == [18]
        parsed = json.loads(result["result"]["content"][0]["text"])
        assert parsed["updated"] == 1
        assert parsed["status"] == "RESOLVED"
        assert parsed["receipts"] == [
            {
                "id": 18,
                "name": "Rewe Einkauf",
                "amount": "42.13",
                "status": "RESOLVED",
                "resolvedDate": "2026-11-10",
            }
        ]
        # Kompaktform: keine Rohdaten
        assert "receiptItems" not in result["result"]["content"][0]["text"]

    async def test_single_int_is_normalized_to_list(self, settings: Any) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content.decode())
            return json_response(BULK_UPDATE_RESPONSE)

        undo = patch_rw(handler)
        try:
            async with running_app(settings) as (_, client):
                result = await mcp_call(
                    client, "tools/call",
                    {"name": "set_receipt_status", "arguments": {"receipt_ids": 18, "status": "resolved"}},
                )
        finally:
            undo()

        # int -> [18], case-insensitiver Status -> normalisiert auf RESOLVED
        assert captured["body"]["receiptIds"] == [18]
        assert captured["body"]["status"] == "RESOLVED"
        parsed = json.loads(result["result"]["content"][0]["text"])
        assert parsed["updated"] == 1

    async def test_invalid_status_is_rejected_without_http_request(self, settings: Any) -> None:
        called = {"http": False}

        def handler(request: httpx.Request) -> httpx.Response:
            called["http"] = True
            return json_response(BULK_UPDATE_RESPONSE)

        undo = patch_rw(handler)
        try:
            async with running_app(settings) as (_, client):
                result = await mcp_call(
                    client, "tools/call",
                    {"name": "set_receipt_status", "arguments": {"receipt_ids": [18], "status": "WEG"}},
                )
        finally:
            undo()

        assert called["http"] is False
        parsed = json.loads(result["result"]["content"][0]["text"])
        assert "Ungültiger Status" in parsed["error"]
        for allowed in ("OPEN", "NEEDS_ATTENTION", "RESOLVED", "DRAFT", "DECLINED"):
            assert allowed in parsed["error"]

    async def test_empty_list_is_rejected_without_http_request(self, settings: Any) -> None:
        called = {"http": False}

        def handler(request: httpx.Request) -> httpx.Response:
            called["http"] = True
            return json_response(BULK_UPDATE_RESPONSE)

        undo = patch_rw(handler)
        try:
            async with running_app(settings) as (_, client):
                result = await mcp_call(
                    client, "tools/call",
                    {"name": "set_receipt_status", "arguments": {"receipt_ids": [], "status": "RESOLVED"}},
                )
        finally:
            undo()

        assert called["http"] is False
        parsed = json.loads(result["result"]["content"][0]["text"])
        assert "leer" in parsed["error"]

    async def test_more_than_50_ids_is_rejected(self, settings: Any) -> None:
        called = {"http": False}

        def handler(request: httpx.Request) -> httpx.Response:
            called["http"] = True
            return json_response(BULK_UPDATE_RESPONSE)

        undo = patch_rw(handler)
        try:
            async with running_app(settings) as (_, client):
                result = await mcp_call(
                    client, "tools/call",
                    {
                        "name": "set_receipt_status",
                        "arguments": {"receipt_ids": list(range(1, 52)), "status": "RESOLVED"},
                    },
                )
        finally:
            undo()

        assert called["http"] is False
        parsed = json.loads(result["result"]["content"][0]["text"])
        assert "maximal 50" in parsed["error"]

    async def test_403_from_server_reports_write_scope_problem(self, settings: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return json_response({"errorMsg": "Unauthorized"}, status=403)

        undo = patch_rw(handler)
        try:
            async with running_app(settings) as (_, client):
                result = await mcp_call(
                    client, "tools/call",
                    {"name": "set_receipt_status", "arguments": {"receipt_ids": [18], "status": "RESOLVED"}},
                )
        finally:
            undo()

        text = result["result"]["content"][0]["text"]
        parsed = json.loads(text)
        assert "403" in parsed["error"]
        assert "Write-Scope" in parsed["error"]
        assert "rw-secret-token" not in text


# ---------------------------------------------------------------------------
# Eingehende Auth-Middleware
# ---------------------------------------------------------------------------
def _dummy_asgi() -> Callable[..., Any]:
    async def app(scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

    return app


class TestAuthMiddleware:
    async def test_401_without_bearer(self) -> None:
        from bons_mcp.auth import BearerAuthMiddleware

        app = BearerAuthMiddleware(_dummy_asgi(), ["t1"])
        async with asgi_client(app) as client:
            response = await client.post("/mcp", json={})
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert "Bearer-Token" in response.text

    async def test_401_with_wrong_bearer(self) -> None:
        from bons_mcp.auth import BearerAuthMiddleware

        app = BearerAuthMiddleware(_dummy_asgi(), ["t1"])
        async with asgi_client(app) as client:
            response = await client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401

    async def test_valid_bearer_passes_through(self) -> None:
        from bons_mcp.auth import BearerAuthMiddleware

        app = BearerAuthMiddleware(_dummy_asgi(), ["t1", "t2"])
        async with asgi_client(app) as client:
            response = await client.post("/mcp", json={}, headers={"Authorization": "Bearer t2"})
        assert response.status_code == 200
        assert response.content == b"ok"

    async def test_empty_bearer_list_fails_closed(self) -> None:
        from bons_mcp.auth import BearerAuthMiddleware

        app = BearerAuthMiddleware(_dummy_asgi(), [])
        async with asgi_client(app) as client:
            response = await client.get("/x", headers={"Authorization": "Bearer x"})
        assert response.status_code == 401

    async def test_healthz_open_mcp_protected(self, settings: Any) -> None:
        """Ohne Token: /healthz 200, /mcp 401 + WWW-Authenticate."""
        app = create_app(settings)
        async with asgi_client(app) as client:
            health = await client.get("/healthz")
            mcp = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping"})
        assert health.status_code == 200
        assert health.json() == {"status": "ok", "service": "bons-mcp"}
        assert mcp.status_code == 401
        assert mcp.headers["www-authenticate"] == "Bearer"

    async def test_server_app_wraps_routes(self, settings: Any) -> None:
        """app.__dict__ Handles vorhanden (Betrieb/Tests)."""
        app = create_app(settings)
        assert app.__dict__["bons_mcp"].name == "bons-mcp"
        assert app.__dict__["bons_settings"] == settings


# ---------------------------------------------------------------------------
# Fail closed: Start ohne Secrets nicht möglich
# ---------------------------------------------------------------------------
class TestFailClosedConfig:
    def test_without_rw_api_token_raises(self) -> None:
        with pytest.raises(ConfigError) as exc:
            load_settings({"MCP_AUTH_TOKENS": "tok", "RW_BASE_URL": "http://receipt-wrangler:80"})
        assert "RW_API_TOKEN" in str(exc.value)

    def test_without_mcp_auth_tokens_raises(self) -> None:
        with pytest.raises(ConfigError) as exc:
            load_settings({"RW_API_TOKEN": "x", "MCP_AUTH_TOKENS": "  "})
        assert "MCP_AUTH_TOKENS" in str(exc.value)

    def test_empty_rw_token_string_raises(self) -> None:
        with pytest.raises(ConfigError):
            load_settings({"RW_API_TOKEN": " ", "MCP_AUTH_TOKENS": "t"})

    def test_server_module_builds_app_with_secrets(self, app_module: Any) -> None:
        assert app_module.app is not None

    def test_bare_import_without_secrets_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Import von bons_mcp.server ohne Secrets: exit != 0 mit klarer Meldung."""
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", "/tmp"),
        }
        proc = subprocess.run(
            [sys.executable, "-c", "import bons_mcp.server"],
            env={"PYTHONPATH": str(SRC_DIR), **env},
            capture_output=True,
            text=True,
            cwd=str(SERVICE_DIR),
            timeout=120,
        )
        assert proc.returncode != 0
        combined = (proc.stderr or "") + (proc.stdout or "")
        assert "RW_API_TOKEN" in combined or "MCP_AUTH_TOKENS" in combined
