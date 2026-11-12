# bons-mcp — MCP-Server für Receipt Wrangler (read-only Tools + 1 Schreibtool)

Schlanker MCP-Server (Streamable HTTP) vor Receipt Wrangler
(v7.3.0, `https://bons.mueller-nas.de`). Zweck: Hermes (MCP-Client) liest
darüber Bons und deren Inhalte aus — und kann genau eine Schreiboperation
ausführen: den Status von Bons ändern (`set_receipt_status`).

## Endpunkte

| Pfad | Auth | Zweck |
|------|------|-------|
| `GET /healthz` | **ohne** | Health-Check (`{"status":"ok"}`) |
| `/mcp` | Bearer-Token (`MCP_AUTH_TOKENS`) | MCP Streamable HTTP (stateless, plain JSON) |

Eingehender Schutz: eigene ASGI-Middleware prüft `Authorization: Bearer <token>`
gegen die kommaseparierte Liste `MCP_AUTH_TOKENS`; Fehler → `401` mit
`WWW-Authenticate: Bearer`. Bewusst **keine** `authentik@docker`-Middleware
(Agenten können sich nicht per SSO anmelden).

## Tools (read-only + genau eine Schreiboperation)

| Tool | Zweck |
|------|-------|
| `list_groups()` | Gruppen (id, name) — ZUERST aufrufen, liefert `group_id` |
| `list_receipts(group_id, ...)` | Kompakte, paginierte Bonliste (id, name, amount, date, status, paidByUserId, categories/tags als Namen, Anzahl Bilder) + `totalCount`/`page`/`pageSize` |
| `get_receipt(receipt_id)` | Volle Details: receiptItems, Kommentare, customFields, imageFiles |
| `search_receipts(query)` | RW-Websuche, kompakte Form wie `list_receipts` (kein totalCount) |
| `get_receipt_image(receipt_image_id)` | Bonbild als MCP-Image-Content (jpeg/png/webp); PDF → Hinweis + Download-Hinweis, kein Fake-Bild |
| `list_categories_and_tags()` | Kategorien/Tags mit id+name (für Filter) |
| `set_receipt_status(receipt_ids, status, comment?)` | **SCHREIBT:** Status von 1–50 Bons ändern (Bulk-Statusupdate); einziger schreibender Aufruf |

## Status setzen (`set_receipt_status`)

Der API-Key braucht für dieses Tool einen Key mit **Read+Write-Scope**
(`r+w`); die anderen Tools funktionieren weiterhin mit reinem Read-Scope.

```json
{"receipt_ids": [18], "status": "RESOLVED", "comment": "Kassenbon geprüft"}
```

- `receipt_ids`: eine Zahl oder Liste von Zahlen (maximal 50 pro Aufruf,
  Duplikate werden entfernt, leere Liste/Typfehler → Tool-Fehler).
- `status`: `OPEN`, `NEEDS_ATTENTION`, `RESOLVED`, `DRAFT`, `DECLINED`
  (case-insensitive; übliche Werte im Betrieb: `NEEDS_ATTENTION`/`RESOLVED`).
- `comment`: optional, landet als Kommentar (System-User) auf jedem geänderten Bon.

Rückgabe kompakt: `{updated, status, receipts: [{id, name, amount, status,
resolvedDate}]}` — `resolvedDate` setzt der Server selbst beim RESOLVED-Wechsel.
Fehlt der Write-Scope im API-Key, antwortet Receipt Wrangler mit `403` und das
Tool meldet das verständlich (ohne Token). Endpunkt:
`POST /api/receipt/bulkStatusUpdate` (verifiziert an `api/swagger.yml` v7.3.0).

Konstanten: Statuswerte sind `OPEN`, `NEEDS_ATTENTION`, `RESOLVED`, `DRAFT`,
`DECLINED`; Datumsfilter als `YYYY-MM-DD`; das `page_size` wird vom Server auf
maximal 100 geklemmt.

## Environment

| Variable | Pflicht | Beschreibung |
|----------|---------|--------------|
| `RW_API_TOKEN` | ja | Receipt-Wrangler-API-Key. **Stolperfalle:** der Key wird **roh** im Header `Authorization: <api_key>` gesendet, **ohne** `Bearer`-Präfix (`Bearer`-Präfix führt zu `403 {"errorMsg":"Unauthorized"}`). Der Key braucht **Read+Write-Berechtigung**, damit `set_receipt_status` funktioniert (nur Read → die Schreiboperation antwortet mit 403, der Rest läuft weiter). Fail-closed: ohne den Wert startet der Server nicht. |
| `MCP_AUTH_TOKENS` | ja | Kommaseparierte eingehende Bearer-Tokens (Hermes). Fail-closed ohne Wert. |
| `RW_BASE_URL` | nein | Default `http://receipt-wrangler:80` (Containername + interner Port 80 im Proxy-Netz). |

API-Basis ist `<RW_BASE_URL>/api` (kein `/v1`). Beim Key-Erstellen in Receipt
Wrangler eine Key mit **Read+Write-Berechtigung** wählen — reines Read reicht
nur, solange `set_receipt_status` nicht genutzt werden soll.

Secrets liegen in **OpenBao** unter `secret/data/mcp/bons` (Feld `token` für
`BONS_MCP_TOKEN`/`MCP_AUTH_TOKENS`).

## Build & Deploy (Docker-Host)

Dateien per `sftp-upload` nach `/opt/docker-compose/receipt-wrangler-mcp/`
(Docker-Host-Profil `docker-host`, Host-Netz: CT 100), dann:

```bash
cd /etc/docker/compose/receipt-wrangler-mcp   # tatsächlich abgelegtes Verzeichnis auf dem Host
docker build --build-arg UID=10001 -t receipt-wrangler-mcp:0.1.0 .
# .env anlegen (Werte aus OpenBao), dann:
docker compose up -d
docker logs -f receipt-wrangler-mcp
```

`.env` (aus `.env.example`):

```dotenv
RW_BASE_URL=http://receipt-wrangler:80
RW_API_TOKEN=<OpenBao secret/data/mcp/bons: token>
BONS_MCP_TOKEN=<OpenBao secret/data/mcp/bons: token>
```

Der Container läuft im externen Docker-Netz `proxy` und hängt sich via
Traefik-Labels an die Route `mcp-bons.mueller-nas.de` (Port 8000, HTTPS).
Keine Authentik-Middleware. Receipt Wrangler ist über den Containernamen im
`proxy`-Netz erreichbar (`RW_BASE_URL`).

## Hermes-Registrierung

In Hermes als MCP-Server vom **type: http** registrieren:

- URL: `https://mcp-bons.mueller-nas.de/mcp`
- Auth: `Bearer <BONS_MCP_TOKEN>` (Wert aus OpenBao Feld `token`)
- Transportstechnik: Streamable HTTP stateless (jede Anfrage eigenständig),
  `GET /healthz` für Registrierungs-Health-Check ohne Auth.

## Labels / Interne Details (für den Betreiber)

- FastMCP: `json_response=True`, `stateless_http=True` — keine Session-Pflege.
- Timeout/Retry Richtung RW: HTTP 30 s, Bilder 60 s; 2 Versuche bei Netzfehler
  bzw. 502/503/504.
- Scheitert der Receipt-Wrangler-Aufruf, kommt ein verständlicher Tool-Fehler
  mit HTTP-Status + kurzer Meldung zurück; Tokens werden in Fehlern
  rausgefiltert (Bearer-Redaction).
- Der Server startet ohne `RW_API_TOKEN` oder `MCP_AUTH_TOKENS` bewusst nicht
  (fail-closed): der Import von `bons_mcp.server` bricht dann ab und uvicorn
  beendet sich mit Exit-Code `!= 0`.

## Tests

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=src pytest tests/
```

Tests laufen ohne Netzwerk (`httpx.MockTransport` für die RW-Seite,
`httpx.ASGITransport` für die ASGI-App).
