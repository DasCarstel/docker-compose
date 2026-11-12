# Outfit-Render-Service

Ergänzt wardrowbe um generierte Outfit-Bilder: die Person vom Referenzfoto wird
mit den Kleidungsstücken eines Outfits aus dem Kleiderschrank gerendert.

## Aufbau

- eigener Container `outfits-render` (FastAPI), Stack unter
  `/opt/docker-compose/outfits-render/`
- liest die wardrowbe-Datenbank (SELECT) und schreibt ausschließlich Bewertungen
  (`user_feedback`, `outfits.status`) sowie die eigenen Tabellen
  (`panel_settings`, `render_usage`, `outfit_favorites`)
- Bildspeicher des Wardrobe-Stacks ist read-only unter `/storage` gemountet
- Referenzfoto + erzeugte Bilder liegen in `/etc/docker/outfits-render/`
- erreichbar unter https://outfits.mueller-nas.de (Authentik Forward-Auth)

## Oberfläche

Eine Seite, server-gerendert, ohne externe Assets:

- **Referenzfoto-Panel** mit Vorschau, Drag & Drop und „Foto ersetzen“
- **Filterleiste**: Alle, Favoriten, Positiv, Negativ, Unbewertet, Mit Bild,
  Ohne Bild – je mit Live-Zähler; dazu Suche und Sortierung
- **Outfit-Karten** mit generiertem Bild, Favoriten-Stern, **Ebenen-Stapel**
  (Schuhe/Hose → Oberteil → Mid-Layer → Jacke, fächert beim Hovern auf) und
  Ebenen-Liste mit Badges; Klick auf ein Teil öffnet ein Detail-Popover
- **Bewertung** als ein Segmented-Control (👍/👎): der Zustand steckt nur im
  aktiven Button, erneuter Klick löscht die Bewertung
- **Bild erzeugen** mit Fortschrittsbalken, Sekundenzähler, Glow und
  Fade/Zoom-Bildwechsel (ohne Reload)
- **Lightbox** mit Download und „Neu erzeugen“
- **Light/Dark-Theme** über Button, Wahl in `localStorage`, Erstbesuch folgt
  `prefers-color-scheme`

## Endpunkte

| Pfad | Zweck |
|------|-------|
| `/` | Übersicht aller Outfits (Filter, Bewertung, Bilderzeugen) |
| `/healthz` | DB/Modell/Referenz-Status |
| `/api/panel/status` | Status für das Hestia-Panel |
| `POST /api/reference` | Referenzfoto (multipart, Feld `file`) |
| `POST /api/render/{outfit_id}` | Bild erzeugen |
| `POST /api/feedback/{outfit_id}` | Bewertung `{"vote":"up"\|"down"}` |
| `DELETE /api/feedback/{outfit_id}` | Bewertung entfernen |
| `POST /api/favorite/{outfit_id}` | Favorit umschalten |
| `/renders/{outfit_id}/{datei}.png` | erzeugtes Bild |
| `/reference` | Referenzfoto |
| `/item/{item_id}` | Kleidungsstück-Bild |

## Konfiguration (`.env`)

| Variable | Bedeutung |
|----------|-----------|
| `DATABASE_URL` | Postgres-DSN |
| `GEMINI_API_KEY` | Key aus OpenBao `secret/data/mcp/gemini` |
| `RENDER_MODEL` | `gemini-nano-banana-2.1` (2K) / `gemini-3-pro-image` |
| `RENDER_IMAGE_SIZE` | `1K` / `2K` |

## Referenzfoto

Ganzkörper inkl. Schuhe, frontal, Arme leicht vom Körper, glatte einfarbige Wand,
Tageslicht von vorne, enges einfarbiges Shirt + lange Hose, **in Farbe**
(bei Schwarzweiß-Vorlagen wird auch das Ergebnis schwarzweiß).

## Betrieb

```sh
cd /opt/docker-compose/outfits-render
docker compose up -d --build
docker logs outfits-render --tail 50
```

Ein Render dauert ca. 30–40 s, kostet ca. 0,05 $ (Nano Banana 2.1 @2K).
Der Prompt wird neben jedem Bild als JSON mitgespeichert (Debugging).
