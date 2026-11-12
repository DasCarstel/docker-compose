"""FastMCP-Server (Streamable HTTP) für Receipt Wrangler - read-only Tools + 1 Schreibtool.

ASGI-Einstiegspunkt für uvicorn: `bons_mcp.server:app`
  - /mcp     : MCP-Protokoll, geschützt durch Bearer-Middleware (MCP_AUTH_TOKENS)
  - /healthz : Health-Check ohne Auth
  - Fail closed: ohne RW_API_TOKEN oder MCP_AUTH_TOKENS bricht der Import ab
    -> uvicorn startet nicht (exit != 0) mit klarer Fehlermeldung.

Alle Tools sind read-only; die einzige Schreiboperation ist
set_receipt_status (POST /receipt/bulkStatusUpdate, Statuswechsel).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP, Image
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from bons_mcp.auth import BearerAuthMiddleware
from bons_mcp.config import Settings, load_settings
from bons_mcp.client import (
    RWClient,
    RWError,
    build_receipt_request,
    compact_receipt,
    compact_search_result,
)

# Host-Allowlist: produktion via Traefik (Host mcp-bons.mueller-nas.de) plus
# localhost fuer lokale Administration/Betrieb. ASGI-Tests nutzen localhost.
ALLOWED_TRANSPORT_HOSTS = [
    "mcp-bons.mueller-nas.de",
    "localhost",
    "127.0.0.1",
    "[::1]",
]

# Status-Enum laut swagger.yml v7.3.0. Der leere String ist in der Spec erlaubt,
# wird bewusst NICHT angeboten (waere ein Silent-Reset).
RECEIPT_STATUSES = ("OPEN", "NEEDS_ATTENTION", "RESOLVED", "DRAFT", "DECLINED")

# Schutz gegen versehentliche Massenoperationen.
MAX_BULK_STATUS_IDS = 50

# Nach dem Bulk-Update maximal so viele Bons einzeln nachladen (GET /receipt/{id}),
# damit name/amount korrekt sind (Receipt Wrangler 7.3 liefert dort Stub-Receipts).
MAX_RELOAD_IDS = 10


def _transport_security() -> Any:
    """DNS-Rebinding-Schutz mit fester Host-Allowlist (bewusst nicht localhost-only)."""
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        # Host mit UND ohne Port zulassen (Traefik sendet Host ohne Port, direkter
        # Aufruf am Container mit Port).
        allowed_hosts=[h + suffix for h in ALLOWED_TRANSPORT_HOSTS for suffix in ("", ":*")],
        allowed_origins=["http://localhost:*", "http://127.0.0.1:*", "http://[::1]:*"],
    )


def _sniff_content_kind(data: bytes, content_type: str) -> str:
    """Inhaltstyp bestimmen: klein, robust, kein Raten bei nichtsagendem Header.

    Returnt einen der Werte: "jpeg", "png", "webp", "pdf", "unknown".
    """
    header_map = {
        "image/jpeg": "jpeg",
        "image/jpg": "jpeg",
        "image/png": "png",
        "image/webp": "webp",
        "application/pdf": "pdf",
    }
    kind = header_map.get(content_type.lower().split(";")[0].strip())
    if kind:
        return kind

    # Header war unzuverlässig (z.B. octet-stream): Magic Bytes prüfen.
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:5] == b"%PDF-":
        return "pdf"
    return "unknown"


def build_mcp(settings: Settings) -> FastMCP:
    """FastMCP-Instanz mit den sieben Tools erzeugen (6x read-only + set_receipt_status).

    json_response + stateless_http: Agenten müssen keine Sessions pflegen.
    """
    mcp = FastMCP(
        "bons-mcp",
        json_response=True,
        stateless_http=True,
        streamable_http_path="/mcp",
        transport_security=_transport_security(),
        instructions=(
            "Zugriff auf Receipt Wrangler (Bons): read-only Tools und genau eine "
            "Schreiboperation (set_receipt_status, Statuswechsel). "
            "Rufe zuerst list_groups auf, um die group_id zu erfahren; "
            "Bild-IDs bekommst du über get_receipt (imageFiles)."
        ),
    )

    def _describe_error(exc: RWError) -> str:
        return f"Fehler beim Aufruf von Receipt Wrangler: {exc}"

    # ------------------------------------------------------------------
    # Tool 1: list_groups
    # ------------------------------------------------------------------
    @mcp.tool(
        name="list_groups",
        description=(
            "Listet die Receipt-Wrangler-Gruppen des Nutzers auf (nur id, name, isAllGroup). "
            "Rufe dieses Tool ZUERST auf: Jede weitere Abfrage (Bons auflisten/lesen) braucht "
            "eine group_id aus dieser Liste. Read-only, ändert nichts."
        ),
    )
    async def list_groups() -> dict[str, Any]:
        try:
            async with RWClient(settings) as client:
                groups = await client.list_groups()
        except RWError as exc:
            return {"error": _describe_error(exc)}
        return {
            "groups": [
                {"id": g.get("id"), "name": g.get("name"), "isAllGroup": g.get("isAllGroup", False)}
                for g in groups
            ],
            "hint": "Verwende die id als group_id für list_receipts.",
        }

    # ------------------------------------------------------------------
    # Tool 2: list_receipts
    # ------------------------------------------------------------------
    @mcp.tool(
        name="list_receipts",
        description=(
            "Kompakte, paginationierte Liste der Bons einer Gruppe. Übergib group_id "
            "(aus list_groups). Rückgabe kürzt Felder bewusst (keine receiptItems): id, name, "
            "amount, date, status (OPEN|NEEDS_ATTENTION|RESOLVED|DRAFT|DECLINED), paidByUserId, "
            "categories/tags als Namen, Anzahl Bilder. Dazu totalCount/page/pageSize, damit du "
            "sichtbar paginieren kannst. Rohdaten einzelner Bons nur über get_receipt. "
            "page_size wird vom Server auf maximal 100 geklemmt. Read-only, ändert nichts."
        ),
    )
    async def list_receipts(
        group_id: int,
        page: int = 1,
        page_size: int = 25,
        order_by: str = "date",
        sort_direction: str = "desc",
        status: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Bons einer Gruppe auflisten (kompakt, der Server filtert/sortiert/paginated)."""
        page_size = max(1, min(page_size, 100))
        page = max(1, page)
        if sort_direction not in ("asc", "desc"):
            sort_direction = "desc"
        if not status:
            status = None
        #Datumsfilter muessen als 'YYYY-MM-DD' kommen (BuildDayFilterQuery erwartet Kalendertage).
        request_body = build_receipt_request(
            page, page_size, order_by, sort_direction, status, start_date, end_date
        )
        try:
            async with RWClient(settings) as client:
                payload = await client.list_receipts(group_id, request_body)
        except RWError as exc:
            return {"error": _describe_error(exc), "page": page, "pageSize": page_size}

        receipts = [compact_receipt(r) for r in (payload.get("data") or [])]
        total = int(payload.get("totalCount", 0))
        has_more = page * page_size < total
        return {
            "data": receipts,
            "totalCount": total,
            "page": page,
            "pageSize": page_size,
            "hasMore": has_more,
            "hint": (
                "Weitere Seite mit erhöhtem page anfragen (hasMore=true). "
                "Einzelne Bons im Detail über get_receipt(receipt_id)."
                if has_more
                else "Alle Seiten wurden gelesen."
            ),
        }

    # ------------------------------------------------------------------
    # Tool 3: get_receipt
    # ------------------------------------------------------------------
    @mcp.tool(
        name="get_receipt",
        description=(
            "Volle Details zu genau einem Bon: receiptItems (Positionen mit id, name, quantity, "
            "lineTotal), Kommentare, customFields und imageFiles (Bild-IDs mit name/fileType). "
            "Nutze die Bild-IDs NICHT als receipt_id verwechseln: imageFiles[].id geht in "
            "get_receipt_image(receipt_image_id). Read-only, ändert nichts."
        ),
    )
    async def get_receipt(receipt_id: int) -> dict[str, Any]:
        try:
            async with RWClient(settings) as client:
                receipt = await client.get_receipt(receipt_id)
        except RWError as exc:
            return {"error": _describe_error(exc)}

        items = [
            {
                "id": i.get("id"),
                "name": i.get("name"),
                "quantity": i.get("quantity"),
                "lineTotal": i.get("lineTotal"),
                "categories": [c.get("name") for c in (i.get("categories") or [])],
                "tags": [t.get("name") for t in (i.get("tags") or [])],
            }
            for i in (receipt.get("receiptItems") or [])
        ]
        comments = [
            {"id": c.get("id"), "comment": c.get("comment"), "createdAt": c.get("createdAt")}
            for c in (receipt.get("comments") or [])
        ]
        return {
            "id": receipt.get("id"),
            "groupId": receipt.get("groupId"),
            "name": receipt.get("name"),
            "amount": receipt.get("amount"),
            "date": receipt.get("date"),
            "resolvedDate": receipt.get("resolvedDate"),
            "status": receipt.get("status"),
            "paidByUserId": receipt.get("paidByUserId"),
            "categories": [c.get("name") for c in (receipt.get("categories") or [])],
            "tags": [t.get("name") for t in (receipt.get("tags") or [])],
            "receiptItems": items,
            "comments": comments,
            "customFields": [
                {"id": f.get("id"), "value": f.get("value"), "typedValue": f.get("typedValue")}
                for f in (receipt.get("customFields") or [])
            ],
            "imageFiles": [
                {"id": f.get("id"), "name": f.get("name"), "fileType": f.get("fileType")}
                for f in (receipt.get("imageFiles") or [])
            ],
            "hint": "Bonbild über get_receipt_image(receipt_image_id) holen, falls nötig.",
        }

    # ------------------------------------------------------------------
    # Tool 4: search_receipts
    # ------------------------------------------------------------------
    @mcp.tool(
        name="search_receipts",
        description=(
            "Durchsucht die Bons über die Receipt-Wrangler-Suche (Suchbegriff greift z.B. auf "
            "den Bonnamen). Rückgabe kompakt wie list_receipts, zusätzlich groupId je Treffer; "
            "keine Paginierung (die Suche liefert direkt eine Liste ohne totalCount). Für Details "
            "danach get_receipt verwendet. Read-only, ändert nichts."
        ),
    )
    async def search_receipts(query: str) -> dict[str, Any]:
        query = (query or "").strip()
        if len(query) < 2:
            return {"error": "Suchbegriff zu kurz: mindestens 2 Zeichen erforderlich."}
        try:
            async with RWClient(settings) as client:
                results = await client.search_receipts(query)
        except RWError as exc:
            return {"error": _describe_error(exc)}

        return {
            "query": query,
            "count": len(results),
            "data": [compact_search_result(r) for r in results],
            "hint": "get_receipt(id) für Details zu einem Treffer.",
        }

    # ------------------------------------------------------------------
    # Tool 5: get_receipt_image
    # ------------------------------------------------------------------
    @mcp.tool(
        name="get_receipt_image",
        structured_output=False,  # Return: MCP-ImageContent | Text -> unvernarrt weitergeben
        description=(
            "Lädt ein Bonbild (jpeg/png/webp) und liefert es als MCP-Image-Content, damit deine "
            "Vision-Fähigkeit den Bon direkt lesen kann. Übergib receipt_image_id aus "
            "get_receipt (imageFiles[].id). Bei PDF kommt bewusst KEIN Fake-Bild, sondern ein "
            "Hinweis inkl. Download-Hinweis zurück. Read-only, ändert nichts."
        ),
    )
    async def get_receipt_image(receipt_image_id: int) -> Image | str:
        try:
            async with RWClient(settings) as client:
                data, content_type = await client.download_receipt_image(
                    receipt_image_id, timeout=settings.image_timeout
                )
        except RWError as exc:
            return _describe_error(exc)
        except httpx.HTTPError as exc:
            return f"Fehler beim Aufruf von Receipt Wrangler: {type(exc).__name__}"

        if not data:
            return f"Das Bild mit id {receipt_image_id} enthält keine Daten (leere Antwort)."

        kind = _sniff_content_kind(data, content_type)
        if kind in ("jpeg", "png", "webp"):
            return Image(data=data, format=kind)
        if kind == "pdf":
            return (
                f"Der Inhalt mit id {receipt_image_id} ist eine PDF-Datei "
                f"(Content-Type: {content_type}, {len(data)} bytes). Ein Vision-Bild kann ich "
                "hier nicht liefern und fälsche auch keines. Bitte die Datei regulär "
                "herunterladen und außerhalb dieses Tools auswerten, oder den Inhalt über "
                "get_receipt lesen. Download-Endpunkt (benötigt den Receipt-Wrangler-API-Key "
                f"als Bearer-Token): {settings.base_url}/api/receiptImage/{receipt_image_id}/download"
            )
        return (
            f"Nicht unterstützter Bildtyp für id {receipt_image_id} "
            f"(Content-Type: {content_type}); bytes sind weder jpeg/png/webp noch pdf, "
            "kein Bild zurückgegeben."
        )

    # ------------------------------------------------------------------
    # Tool 6: list_categories_and_tags
    # ------------------------------------------------------------------
    @mcp.tool(
        name="list_categories_and_tags",
        description=(
            "Alle Kategorien und Tags des Systems mit id und name. Verwende die IDs in "
            "Filtern/Abfrage-Kontexten, nicht die Namen raten lassen. Rückgabe: "
            "{'categories': [{id, name}], 'tags': [{id, name}]}. Read-only, ändert nichts."
        ),
    )
    async def list_categories_and_tags() -> dict[str, Any]:
        try:
            async with RWClient(settings) as client:
                categories = await client.list_categories()
                tags = await client.list_tags()
        except RWError as exc:
            return {"error": _describe_error(exc)}

        return {
            "categories": [{"id": c.get("id"), "name": c.get("name")} for c in (categories or [])],
            "tags": [{"id": t.get("id"), "name": t.get("name")} for t in (tags or [])],
        }

    # ------------------------------------------------------------------
    # Tool 7: set_receipt_status (die einzige Schreiboperation)
    # ------------------------------------------------------------------
    @mcp.tool(
        name="set_receipt_status",
        description=(
            "ACHTUNG - SCHREIBT: ändert den Status von einem oder mehreren Bons "
            "(Bulk-Statusupdate auf dem Server). Alle anderen Tools sind read-only, "
            "nur dieses Tool verändert Daten! Übergib receipt_ids (eine Zahl oder "
            f"Liste von Zahlen, maximal {MAX_BULK_STATUS_IDS} pro Aufruf) und status "
            "(erlaubte Werte: OPEN, NEEDS_ATTENTION, RESOLVED, DRAFT, DECLINED; "
            "case-insensitive). Die üblichen Betriebswerte sind NEEDS_ATTENTION und "
            "RESOLVED. resolvedDate setzt Receipt Wrangler serverseitig. Optional "
            "landet comment als Kommentar (System-User) auf jedem geänderten Bon. "
            "Rückgabe kompakt: "
            "{updated, status, receipts: [{id, name, amount, status, resolvedDate}]}. "
            "Bei bis zu 10 Bons werden Name/Betrag nach dem Update korrekt "
            "nachgeladen; darüber hinaus nur {id, status} (Details über get_receipt)."
        ),
    )
    async def set_receipt_status(
        receipt_ids: int | list[int],
        status: str,
        comment: str | None = None,
    ) -> dict[str, Any]:
        """Status von Bons ÄNDERN (schreibt auf dem Server)."""
        # Normalisierung: einzelne int -> Liste (modellfreundliche Signatur).
        if isinstance(receipt_ids, bool) or not isinstance(receipt_ids, (int, list, tuple)):
            return {
                "error": f"receipt_ids muss eine Zahl oder eine Liste von Zahlen sein (erhielt {type(receipt_ids).__name__})."
            }
        raw_ids = [receipt_ids] if isinstance(receipt_ids, int) else list(receipt_ids)
        if not all(isinstance(i, int) and not isinstance(i, bool) for i in raw_ids):
            return {"error": "receipt_ids darf nur ganze Zahlen (Bon-IDs) enthalten."}
        ids: list[int] = list(dict.fromkeys(raw_ids))  # Duplikate entfernen, Reihenfolge bleibt
        if not ids:
            return {"error": "receipt_ids ist leer: mindestens eine Bon-ID erforderlich."}
        if len(ids) > MAX_BULK_STATUS_IDS:
            return {
                "error": (
                    f"Zu viele Bon-IDs ({len(ids)}): maximal {MAX_BULK_STATUS_IDS} "
                    "pro Aufruf, bitte in mehreren Aufrufen abarbeiten."
                )
            }
        if not all(i > 0 for i in ids):
            return {"error": "Bon-IDs müssen positive ganze Zahlen sein."}

        normalized_status = (status or "").strip().upper()
        if normalized_status not in RECEIPT_STATUSES:
            return {
                "error": (
                    f"Ungültiger Status {status!r}. Erlaubte Werte: "
                    + ", ".join(RECEIPT_STATUSES)
                )
            }

        clean_comment = (comment or "").strip() or None
        try:
            async with RWClient(settings) as client:
                receipts = await client.bulk_update_status(ids, normalized_status, clean_comment)
                if not isinstance(receipts, list):  # Defensiv: Server liefert Receipt[]
                    return {"error": "Unerwartete Antwort vom Server (Receipt[] erwartet)."}

                stub_receipts = [
                    {
                        "id": r.get("id"),
                        "name": r.get("name"),
                        "amount": r.get("amount"),
                        "status": r.get("status"),
                        "resolvedDate": r.get("resolvedDate"),
                    }
                    for r in receipts
                ]

                if len(stub_receipts) > MAX_RELOAD_IDS:
                    # Skalierung: bei vielen Bons keine Einzel-Nachlade-Requests,
                    # nur id + status zurückgeben (Details über get_receipt).
                    return {
                        "updated": len(receipts),
                        "status": normalized_status,
                        "receipts": [
                            {"id": r.get("id"), "status": r.get("status")}
                            for r in receipts
                        ],
                        "hint": (
                            f"Bei mehr als {MAX_RELOAD_IDS} Bons werden nur id und status "
                            "zurückgegeben; Name, Betrag und resolvedDate eines Bons "
                            "ließt get_receipt(receipt_id) aus."
                        ),
                    }

                # Receipt Wrangler 7.3 liefert im Bulk-Response Stub-Receipts
                # (name: "", amount: "0") - betroffene Bons einzeln nachladen.
                reloaded: list[dict[str, Any]] = []
                reload_failed = False
                for entry in stub_receipts:
                    receipt_id = entry["id"]
                    try:
                        full = await client.get_receipt(int(receipt_id))
                    except RWError:
                        reload_failed = True  # Nachladen darf nicht scheitern
                        reloaded.append(entry)
                        continue
                    if not isinstance(full, dict):
                        reload_failed = True
                        reloaded.append(entry)
                        continue
                    reloaded.append(
                        {
                            "id": full.get("id"),
                            "name": full.get("name"),
                            "amount": full.get("amount"),
                            "status": full.get("status"),
                            "resolvedDate": full.get("resolvedDate"),
                        }
                    )
        except RWError as exc:
            return {"error": _describe_error(exc), "status": normalized_status}

        hint = "resolvedDate setzt Receipt Wrangler serverseitig."
        if len(ids) <= MAX_RELOAD_IDS:
            if reload_failed:
                hint = (
                    "Nicht alle Bons konnten nachgeladen werden; fehlende Werte "
                    "stammen aus dem Bulk-Response. Details über get_receipt(receipt_id)."
                )
            else:
                hint = f"{hint} Details zu einem Bon über get_receipt(receipt_id)."

        return {
            "updated": len(reloaded),
            "status": normalized_status,
            "receipts": reloaded,
            "hint": hint,
        }

    return mcp


async def _healthz(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "bons-mcp"})


def create_app(settings: Settings | None = None) -> BearerAuthMiddleware:
    """ASGI-App bauen: /healthz offen, /mcp hinter Bearer-Schutz.

    Handles für den Betrieb liegen als Attribute auf der Middleware:
      - app.__dict__["bons_mcp"]: FastMCP-Instanz (u.a. session_manager.run() für Tests)
      - app.__dict__["bons_settings"]: die verwendete Settings-Instanz
    """
    settings = settings or load_settings()
    mcp = build_mcp(settings)

    asgi = mcp.streamable_http_app()  # Starlette inkl. Lifespan (Session-Manager)
    asgi.router.routes.append(Route("/healthz", endpoint=_healthz, methods=["GET"]))

    wrapped = BearerAuthMiddleware(asgi, settings.mcp_auth_tokens)
    # Test-/Betriebshandles: als echte __dict__-Keys zugreifbar
    # (app.__dict__["bons_mcp"], app.__dict__["bons_settings"]).
    wrapped.__dict__["bons_mcp"] = mcp
    wrapped.__dict__["bons_settings"] = settings
    return wrapped


# Fail-closed: Importzeit-Validierung steigt aus, uvicorn startet dann nicht.
_settings = load_settings()
try:
    app = create_app(_settings)
except Exception:  # noqa: BLE001 - bewusst fail-closed für den Betriebsdienst
    logging.exception("bons-mcp: Start verweigert (fehlende Konfiguration).")
    raise


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
