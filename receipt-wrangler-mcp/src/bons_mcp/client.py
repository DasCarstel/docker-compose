"""HTTP-Client für die Receipt-Wrangler-API (verifiziert an swagger.yml).

read-only Endpunkte plus genau eine Schreiboperation
(POST /receipt/bulkStatusUpdate, Statuswechsel).
"""

from __future__ import annotations

import re
from typing import Any

import httpx

# Pfade relativ zur API-Basis (Settings.base_url + "/api/").
# Verifiziert an api/swagger.yml:
#   GET  /group                      -> Group[]
#   POST /receipt/group/{groupId}    -> PagedData (Body ReceiptPagedRequestCommand)
#   GET  /receipt/{receiptId}        -> Receipt
#   GET  /search/                    -> SearchResult[] (Query param: searchTerm)
#   GET  /receiptImage/{id}/download -> application/octet-stream
#   GET  /category/                  -> Category[]
#   GET  /tag/                       -> Tag[]
#   POST /receipt/bulkStatusUpdate   -> Receipt[] (Body BulkStatusUpdateCommand:
#        {"status": "RESOLVED", "receiptIds": [..], "comment": "optional"});
#        Status-Enum: OPEN|NEEDS_ATTENTION|RESOLVED|DRAFT|DECLINED

RETRYABLE_STATUSES = (502, 503, 504)


class RWError(Exception):
    """Verständlicher Fehler gegenüber dem Tool-Aufrufer (ohne Token-Leak)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def compact_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Vollständiges Receipt auf kompakte Listenform kürzen (keine receiptItems)."""
    return {
        "id": receipt.get("id"),
        "name": receipt.get("name"),
        "amount": receipt.get("amount"),
        "date": receipt.get("date"),
        "resolvedDate": receipt.get("resolvedDate"),
        "status": receipt.get("status"),
        "paidByUserId": receipt.get("paidByUserId"),
        "categories": [c.get("name") for c in (receipt.get("categories") or [])],
        "tags": [t.get("name") for t in (receipt.get("tags") or [])],
        "imageCount": len(receipt.get("imageFiles") or []),
        "firstComment": receipt.get("firstComment"),
    }


def compact_search_result(result: dict[str, Any]) -> dict[str, Any]:
    """SearchResult auf dieselbe Kompaktform wie list_receipts abbilden."""
    return {
        "id": result.get("id"),
        "name": result.get("name"),
        "type": result.get("type"),
        "groupId": result.get("groupId"),
        "date": result.get("date"),
        "amount": result.get("amount"),
        "status": result.get("receiptStatus"),
        "paidByUserId": result.get("paidByUserId"),
    }


def build_receipt_request(
    page: int,
    page_size: int,
    order_by: str,
    sort_direction: str,
    status: str | None,
    start_date: str | None,
    end_date: str | None,
) -> dict[str, Any]:
    """ReceiptPagedRequestCommand aus Tool-Parametern bauen.

    Value-Semantik laut Backend (api/internal/repositories/base_repository.go):
      - status: Operation CONTAINS mit Wert-Array -> SQL `IN` auf der Status-Spalte.
      - dates:  Operation BETWEEN mit Wert-Array [beginn, ende] als "yyyy-MM-dd";
                BETWEEN vergleicht als Kalendertage inklusive beider Randtage.
    """
    body: dict[str, Any] = {
        "page": page,
        "pageSize": page_size,
        "orderBy": order_by,
        "sortDirection": sort_direction,
    }
    filter_def: dict[str, Any] = {}

    if status:
        filter_def["status"] = {"operation": "CONTAINS", "value": [status]}

    if start_date or end_date:
        if start_date and end_date:
            value: list[str] = [start_date, end_date]
        elif start_date:
            value = [start_date, "9999-12-31"]
        else:
            value = ["0001-01-01", str(end_date)]
        filter_def["date"] = {"operation": "BETWEEN", "value": value}

    if filter_def:
        body["filter"] = filter_def
    return body


class RWClient:
    """Dünner Async-Wrapper um die Receipt-Wrangler-API."""

    def __init__(
        self,
        settings: Any,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._max_attempts = settings.max_attempts
        self._client = httpx.AsyncClient(
            base_url=f"{settings.base_url}/api/",
            # WICHTIG (live verifiziert): Receipt Wrangler erwartet den API-Key
            # ROH im Authorization-Header, OHNE "Bearer "-Präfix.
            #   Authorization: <api_key>   -> 200
            #   Authorization: Bearer <api_key> -> 403 {"errorMsg":"Unauthorized"}
            headers={"Authorization": settings.rw_api_token, "Accept": "application/json"},
            timeout=settings.request_timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> RWClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Request mit einem Retry (max. 2 Versuche) und sauberer Fehler-Abbildung."""
        response: httpx.Response | None = None
        last_exc: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.request(
                    method, path, json=json, params=params, timeout=timeout
                )
                if response.status_code in RETRYABLE_STATUSES and attempt < self._max_attempts:
                    continue
                break
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt >= self._max_attempts:
                    raise RWError(
                        "Receipt Wrangler ist nicht erreichbar (Netzwerk-/Timeout-Fehler nach "
                        f"{self._max_attempts} Versuchen): {type(exc).__name__}",
                        status=None,
                    ) from exc

        assert response is not None
        if response.status_code >= 400:
            raise RWError(_humanize_status(response, self._settings.rw_api_token), status=response.status_code)
        return response

    # --- Endpunkte -------------------------------------------------------

    async def list_groups(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "group")
        return response.json()  # type: ignore[no-any-return]

    async def list_receipts(
        self,
        group_id: int,
        request_body: dict[str, Any],
    ) -> dict[str, Any]:
        response = await self._request(
            "POST", f"receipt/group/{group_id}", json=request_body
        )
        return response.json()  # type: ignore[no-any-return]

    async def get_receipt(self, receipt_id: int) -> dict[str, Any]:
        response = await self._request("GET", f"receipt/{receipt_id}")
        return response.json()  # type: ignore[no-any-return]

    async def search_receipts(self, query: str) -> list[dict[str, Any]]:
        response = await self._request("GET", "search/", params={"searchTerm": query})
        return response.json()  # type: ignore[no-any-return]

    async def download_receipt_image(self, receipt_image_id: int, timeout: float) -> tuple[bytes, str]:
        """Bildbytes plus Server-Content-Type holen (60 s Timeout laut Dispatch)."""
        response = await self._request(
            "GET", f"receiptImage/{receipt_image_id}/download", timeout=timeout
        )
        content_type = response.headers.get("content-type", "application/octet-stream")
        return response.content, content_type

    async def list_categories(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "category/")
        return response.json()  # type: ignore[no-any-return]

    async def list_tags(self) -> list[dict[str, Any]]:
        response = await self._request("GET", "tag/")
        return response.json()  # type: ignore[no-any-return]

    async def bulk_update_status(
        self,
        receipt_ids: list[int],
        status: str,
        comment: str | None = None,
    ) -> list[dict[str, Any]]:
        """Status von mehreren Bons auf einmal ändern (POST /receipt/bulkStatusUpdate).

        Body: {"status": <status>, "receiptIds": [...], "comment": optional}.
        Die Validierung (leer/>50/Enum) macht das Tool in server.py, hier geht nur
        ein sauberer Request raus. Rückgabe: die aktualisierten Receipt[] (roh),
        Kompaktierung übernimmt das Tool.
        """
        body: dict[str, Any] = {"status": status, "receiptIds": list(receipt_ids)}
        if comment:
            body["comment"] = comment
        response = await self._request("POST", "receipt/bulkStatusUpdate", json=body)
        return response.json()  # type: ignore[no-any-return]


def _redact_bearer(text: str, token: str | None = None) -> str:
    """Bearer-Tokens UND den konfigurierten API-Key aus fremden Texten entfernen.

    Receipt Wrangler erwartet den Key ROH im Authorization-Header (ohne Bearer),
    daher kann ein Fehler-Snippet den Key auch unpraefixt enthalten - beides muss
    maskiert werden (kein Secret-Leak in Fehlern).
    """
    text = re.sub(r"Bearer\s+\S+", "Bearer ***", text)
    if token:
        text = text.replace(token, "***")
    return text


def _humanize_status(
    response: httpx.Response, token: str | None = None
) -> str:
    """Status + kurze Meldung verständlich formulieren; Body nur als Snippet."""
    status = response.status_code
    if status == 401:
        return "Receipt Wrangler hat den Aufruf abgelehnt (401 Unauthorized): der API-Key ist ungültig, abgelaufen oder hat keinen read-only-Scope."
    if status == 403:
        return (
            "Kein Zugriff (403 Forbidden): der API-Key bzw. der Nutzer hat für den "
            "angefragten Bereich keine Rechte. Bei einer Schreiboperation (Status ändern) "
            "deutet dies auf einen fehlenden Write-Scope des API-Keys hin."
        )
    if status == 404:
        return "Nicht gefunden (404): die angefrage ID existiert nicht oder ist nicht sichtbar."
    if status == 429:
        return "Zu viele Anfragen (429): Receipt Wrangler drosselt den Zugang, später erneut versuchen."

    snippet = _redact_bearer(response.text.strip()[:200], token) if response.text else ""
    detail = f" -> {snippet}" if snippet else ""
    return f"Receipt Wrangler antwortete mit HTTP {status}{detail}"
