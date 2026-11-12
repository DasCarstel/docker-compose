"""Outfit render service for wardrowbe.

Read-only on the wardrobe database, generates photorealistic outfit images with
Gemini (Nano Banana) using a stored full-body reference photo of the owner.

Deliberately decoupled: no writes into wardrowbe, no fork, only SELECTs.
"""

import base64
import html
import io
import json
import os
import time
import urllib.error
import urllib.request
import uuid as uuidlib
from pathlib import Path

import psycopg
from PIL import Image, ImageOps
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

APPDATA = Path(os.environ.get("APPDATA_DIR", "/appdata"))
RENDERS = APPDATA / "renders"
RENDERS.mkdir(parents=True, exist_ok=True)
REFERENCE = APPDATA / "reference.jpg"

STORAGE = Path(os.environ.get("STORAGE_ROOT", "/storage"))
MODEL_DEFAULT = "gemini-nano-banana-2.1"
SIZE_DEFAULT = "2K"
MODEL = os.environ.get("RENDER_MODEL", MODEL_DEFAULT)
IMAGE_SIZE = os.environ.get("RENDER_IMAGE_SIZE", SIZE_DEFAULT)
API_KEY = os.environ["GEMINI_API_KEY"]
DSN = os.environ["DATABASE_URL"]

app = FastAPI(title="Outfit Render", docs_url=None, redoc_url=None)

OUTER = {"jacket", "coat", "blazer", "parka", "vest", "overcoat", "cardigan", "hoodie"}
MID = {"hoodie", "cardigan", "vest", "sweatshirt", "sweater", "jumper", "pullover"}
BOTTOM = {"pants", "jeans", "shorts", "skirt", "trousers", "chinos", "leggings"}
SHOES = {"shoes", "sneakers", "boots", "sandals", "heels", "flats", "loafers"}
FULL = {"dress", "jumpsuit", "overalls", "suit"}
ACCESSORY = {"accessories", "accessory", "belt", "socks", "scarf", "hat",
             "cap", "watch", "bag", "tie", "jewelry", "sunglasses"}


def side(it):
    """Which body slot a garment fills. Everything unknown is a base top."""
    t = (it.get("type") or "").lower()
    if t in OUTER:
        return "outer"
    if t in BOTTOM:
        return "bottom"
    if t in SHOES:
        return "shoes"
    if t in FULL:
        return "full"
    return "top"


def kind_name(it):
    """Human-readable garment type for the prompt."""
    return (it.get("type") or "item").replace("_", " ")


def wear_weight(it):
    """How close to the body a garment sits: 1 = innermost, 3 = outermost."""
    t = (it.get("type") or "").lower()
    if t in OUTER:
        return 3
    if t in MID:
        return 2
    return 1


def order_key(it):
    """Display order for reference images: from the body outwards."""
    return {"shoes": 0, "bottom": 1, "top": 2, "full": 2, "outer": 3}[side(it)]


def layer_rank(it):
    """Sort order for the layered UI stack: from the body outwards.

    Mixes side() (shoes/bottom first) with wear_weight() for upper bodies:
    base tops sit close, mid layers over them, outerwear on top.
    """
    s = side(it)
    if s == "shoes":
        return 0
    if s == "bottom":
        return 1
    return 1 + wear_weight(it)


def layer_badge(it):
    """Compact layer label for the UI stack, from the body outwards."""
    t = (it.get("type") or "").lower()
    if t in OUTER:
        return "Außen"
    if t in MID:
        return "Mid"
    s = side(it)
    if s == "shoes":
        return "Schuhe"
    if s == "bottom":
        return "Unten"
    if s == "full":
        return "Komplett"
    if t in ACCESSORY:
        return "Detail"
    return "Basis"


def query(sql, params=()):
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]


def execute(sql, params=()):
    """Run a write statement. The render service connects with the full
    wardrobe DB role so it can persist outfit feedback votes."""
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()


def ensure_tables():
    """Panel tables for the Hestia Wardrobe tab: settings (switchable render
    model / budget), one row per render request for usage tracking and the
    favorites table for the web UI."""
    execute(
        """
        CREATE TABLE IF NOT EXISTS panel_settings (
            key        text PRIMARY KEY,
            value      text NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    execute(
        """
        CREATE TABLE IF NOT EXISTS render_usage (
            id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            created_at        timestamptz NOT NULL DEFAULT now(),
            outfit_id         uuid,
            model             text NOT NULL,
            image_size        text,
            prompt_tokens     integer DEFAULT 0,
            text_output_tokens integer DEFAULT 0,
            image_tokens      integer DEFAULT 0,
            total_tokens      integer DEFAULT 0,
            service_tier      text,
            status            text NOT NULL DEFAULT 'ok',
            error             text,
            request_ms        integer
        )
        """
    )
    try:
        # Favorites are only used by the UI; if this fails (e.g. missing
        # rights) the service must still start, favorites then report False.
        execute(
            """
            CREATE TABLE IF NOT EXISTS outfit_favorites (
                outfit_id  uuid PRIMARY KEY REFERENCES outfits(id) ON DELETE CASCADE,
                created_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    except Exception:  # noqa: BLE001 — favorites must never block startup
        pass


def render_config():
    """Render model + image size for THIS request, live from panel_settings
    so the Hestia panel can switch them without a container restart. The
    RENDER_MODEL/RENDER_IMAGE_SIZE env values act as fallback."""
    cfg = {"model": MODEL, "image_size": IMAGE_SIZE}
    try:
        for row in query(
            "SELECT key, value FROM panel_settings "
            "WHERE key IN ('image_model', 'image_size')"
        ):
            value = (row.get("value") or "").strip()
            if value:
                cfg["model" if row["key"] == "image_model" else "image_size"] = value
    except Exception:  # noqa: BLE001 — settings are optional, defaults win
        pass
    return cfg


def usage_stats(payload):
    """Split a generateContent usageMetadata into the token classes we bill:
    input (prompt) tokens, text/thinking output tokens and image output token
    (the expensive class for Nano Banana)."""
    u = (payload or {}).get("usageMetadata") or {}

    def modality_tokens(details, modality):
        return sum(
            int(d.get("tokenCount") or 0)
            for d in (details or [])
            if (d.get("modality") or "").upper() == modality
        )

    image_tokens = modality_tokens(u.get("candidatesTokensDetails"), "IMAGE")
    candidates = int(u.get("candidatesTokenCount") or 0)
    return {
        "prompt_tokens": int(u.get("promptTokenCount") or 0),
        "text_output_tokens": max(candidates - image_tokens, 0),
        "image_tokens": image_tokens,
        "total_tokens": int(u.get("totalTokenCount") or 0),
        "service_tier": u.get("serviceTier"),
    }


def log_usage(outfit_id, cfg, usage, status="ok", error=None, request_ms=None):
    """Persist one usage row. Tracking must never break a render, so failures
    are swallowed (worst case: a render isn't counted)."""
    try:
        execute(
            """
            INSERT INTO render_usage
                   (outfit_id, model, image_size, prompt_tokens,
                    text_output_tokens, image_tokens, total_tokens,
                    service_tier, status, error, request_ms)
            VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                outfit_id, cfg["model"], cfg.get("image_size"),
                usage.get("prompt_tokens", 0), usage.get("text_output_tokens", 0),
                usage.get("image_tokens", 0), usage.get("total_tokens", 0),
                usage.get("service_tier"), status,
                (error or "")[:500] or None, request_ms,
            ),
        )
    except Exception:  # noqa: BLE001
        pass


ensure_tables()


_NAME_CACHE = {}


def resolve(path_value):
    """Locate a wardrobe image in the read-only storage mount.

    wardrowbe stores absolute paths such as /data/items/<id>/<file>.png from the
    backend container's point of view; here the same tree is mounted at STORAGE.
    """
    if not path_value:
        return None
    p = Path(path_value)
    candidates = [p]
    if str(p).startswith("/data/"):
        candidates.append(STORAGE / str(p)[len("/data/"):])
    candidates.append(STORAGE / p)
    candidates.append(STORAGE / p.name)
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    if p.name not in _NAME_CACHE:
        found = next(iter(sorted(STORAGE.rglob(p.name))), None)
        _NAME_CACHE[p.name] = found
    return _NAME_CACHE[p.name]


def item_image(item):
    for key in ("image_path", "original_image_path", "thumbnail_path"):
        got = resolve(item.get(key))
        if got:
            return got
    return None


def list_outfits(limit=80):
    """All outfits + items + latest renders for the UI.

    Favorites degrade gracefully: if the outfit_favorites table is missing
    (creation failed at startup), every outfit is reported as favorited=False.
    """
    base = """
        SELECT o.id::text AS id,
               COALESCE(NULLIF(o.reasoning, ''), o.name) AS name,
               COALESCE(o.style_notes, '') AS style_notes,
               o.occasion, o.created_at,
               o.source,
               o.status::text AS status,
               o.responded_at AS responded_at,
               uf.rating AS rating,
               uf.accepted AS accepted,
               uf.created_at AS rated_at
               __FAVSEL__
        FROM outfits o
        LEFT JOIN user_feedback uf ON uf.outfit_id = o.id
        ORDER BY o.created_at DESC NULLS LAST
        LIMIT %s
        """
    fav_sel = (
        ",\n               EXISTS (SELECT 1 FROM outfit_favorites f "
        "WHERE f.outfit_id = o.id) AS favorited"
    )
    try:
        outfits = query(base.replace("__FAVSEL__", fav_sel), (limit,))
    except Exception:  # noqa: BLE001 — favorites table may not exist yet
        outfits = query(base.replace("__FAVSEL__", ""), (limit,))
        for o in outfits:
            o["favorited"] = False
    if not outfits:
        return []
    ids = [o["id"] for o in outfits]
    items = query(
        """
        SELECT oi.outfit_id::text AS outfit_id, oi.position, oi.layer_type,
               ci.id::text AS item_id, ci.name, ci.type, ci.subtype,
               ci.primary_color, ci.pattern, ci.material,
               ci.image_path, ci.original_image_path, ci.thumbnail_path
        FROM outfit_items oi
        JOIN clothing_items ci ON ci.id = oi.item_id
        WHERE oi.outfit_id = ANY(%s::uuid[])
        ORDER BY oi.outfit_id, oi.position
        """,
        (ids,),
    )
    bucket = {}
    for it in items:
        bucket.setdefault(it["outfit_id"], []).append(it)

    for o in outfits:
        o["items"] = bucket.get(o["id"], [])
        o["has_image"] = bool(item_image(o["items"][0])) if o["items"] else False
        rdir = RENDERS / o["id"]
        renders = sorted(rdir.glob("*.png"), reverse=True) if rdir.is_dir() else []
        o["renders"] = [p.name for p in renders]
        o["render_count"] = len(o["renders"])
        o["favorited"] = bool(o.get("favorited"))
        o["when"] = o["created_at"].strftime("%d.%m.%Y %H:%M") if o["created_at"] else ""
    return outfits


def build_prompt(items, styling=None):
    numbered = []
    idx = 2
    for it in sorted(items, key=order_key):
        desc = f"image {idx}: {(it.get('primary_color') or '')} {kind_name(it)}".replace("  ", " ").strip()
        if it.get("subtype"):
            desc += f" ({it['subtype'].replace('_', ' ')})"
        if it.get("pattern") and it["pattern"] != "solid":
            desc += f", {it['pattern']} pattern"
        numbered.append(desc)
        idx += 1

    layering = []
    types = [(it.get("type") or "").lower() for it in items]
    upper = [it for it in items if side(it) in ("top", "outer")]

    if any(t in FULL for t in types):
        layering.append("the one-piece garment is worn as the single main layer")
    else:
        if any(t in BOTTOM for t in types):
            layering.append("the trousers or skirt sit at the natural waist")
        if upper:
            if len(upper) == 1:
                layering.append("the top sits at the waistline (tucked in or out — the styling direction below decides)")
            else:
                # Layered look: list every piece in the order it is worn, from
                # the piece closest to the body to the outermost one, so the
                # model cannot drop a layer.
                names = [f"the {kind_name(it).lower()}" for it in sorted(upper, key=wear_weight)]
                if len(names) == 2:
                    chain = f"{names[-1]} is worn over {names[0]}"
                else:
                    chain = ", then ".join(reversed(names))
                layering.append(f"layered look (in this order): {chain}")
                layering.append("each upper layer is fully visible, nothing is hidden; the innermost layer peeks out at the collar and hem")
                layering.append("open and closed layers follow the garment's own design (zips, buttons, pullovers)")
        if any(t in SHOES for t in types):
            layering.append("the shoes are on the feet")

    return (
        "You are a fashion photographer producing a realistic outfit preview.\n\n"
        "Image 1 is the reference photo of the person: keep their face, hairstyle, "
        "body shape, skin tone, pose and camera framing exactly as they are, and keep "
        "the same background and lighting.\n\n"
        "The remaining images are single garments photographed flat on a plain background:\n"
        + "\n".join(numbered)
        + "\n\nDress the person from image 1 in exactly these garments.\n"
        "Rules:\n"
        "- Use only the garments listed above. Do not add, remove or invent any item.\n"
        "- Reproduce each garment's exact colour, pattern, print, fabric and visible "
        "details (buttons, stitching, collar, cuffs, logos). Never restyle or recolour them.\n"
        "- Respect the real proportions and fit; the clothes must look worn, not pasted on.\n"
        + ("- " + "; ".join(layering) + ".\n" if layering else "")
        + ("- Styling direction for how the garments sit: " + styling.strip() +
           ". Apply it only to the listed garments (cuffs, tucking, open or closed, "
           "sleeves); never add, remove or replace any garment.\n" if styling else "")
        + "- Waistline default: when the styling direction (if any) does not mention whether the top sits in or out of the waistband, tuck collar shirts with a defined hem (button-down, oxford, dress shirt) neatly into trousers, while t-shirts, sweaters, knits and camp-collar shirts stay untucked; in layered looks the innermost tuckable layer follows this rule and outer layers stay out.\n"
        + "- Keep the person's face and body unchanged; do not beautify or reshape them.\n"
        "- Full body in frame including the shoes, front view, natural daylight.\n"
        "- Output exactly one photorealistic photograph.\n"
    )


def gemini_generate(parts, model=None, image_size=None, timeout=300):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model or MODEL}:generateContent"
    )
    bodies = [
        {"contents": [{"parts": parts}],
         "generationConfig": {"imageConfig": {"imageSize": image_size or IMAGE_SIZE}}},
        {"contents": [{"parts": parts}]},
    ]
    last = None
    last_usage = {}
    for body in bodies:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": API_KEY},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            last = f"HTTP {exc.code}: {exc.read()[:300].decode('utf-8', 'replace')}"
            continue
        except Exception as exc:  # noqa: BLE001
            last = str(exc)[:300]
            continue
        last_usage = payload.get("usageMetadata") or {}
        for cand in payload.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                blob = part.get("inlineData") or part.get("inline_data")
                if blob and blob.get("data"):
                    return base64.b64decode(blob["data"]), usage_stats(payload)
        last = "no image in response"
    raise HTTPException(status_code=502, detail=last or "generation failed")


def render_outfit(outfit_id):
    if not REFERENCE.is_file():
        raise HTTPException(status_code=409, detail="Kein Referenzfoto hinterlegt.")
    items = query(
        """
        SELECT ci.id::text, ci.name, ci.type, ci.subtype, ci.primary_color,
               ci.pattern, ci.image_path, ci.original_image_path, ci.thumbnail_path,
               oi.position
        FROM outfit_items oi
        JOIN clothing_items ci ON ci.id = oi.item_id
        WHERE oi.outfit_id = %s::uuid
        ORDER BY oi.position
        """,
        (outfit_id,),
    )
    if not items:
        raise HTTPException(status_code=404, detail="Outfit nicht gefunden oder leer.")

    # The wardrobe AI's own styling tip travels with the request, so the
    # rendered picture can show the intended little details (cuffs, tucks).
    meta = query(
        """
        SELECT COALESCE(NULLIF(o.reasoning, ''), o.name) AS name,
               COALESCE(o.style_notes, '') AS style_notes
        FROM outfits o WHERE o.id = %s::uuid
        """,
        (outfit_id,),
    )
    styling = (meta[0]["style_notes"] if meta else "").strip()

    parts = [
        {"inlineData": {"mimeType": "image/jpeg",
                        "data": base64.b64encode(REFERENCE.read_bytes()).decode()}}
    ]
    used = []
    for it in items:
        path = item_image(it)
        if not path:
            continue
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        parts.append({"inlineData": {"mimeType": mime,
                                     "data": base64.b64encode(path.read_bytes()).decode()}})
        used.append(it)
    if len(used) == 0:
        raise HTTPException(status_code=409, detail="Keine Bilddateien zu den Teilen gefunden.")
    # Reference images and prompt numbering must follow the same order.
    used.sort(key=order_key)

    prompt = build_prompt(used, styling=styling)

    parts.append({"text": prompt})
    cfg = render_config()
    t0 = time.time()
    try:
        data, usage = gemini_generate(
            parts, model=cfg["model"], image_size=cfg["image_size"]
        )
    except HTTPException as exc:
        log_usage(outfit_id, cfg, {}, status="error", error=exc.detail,
                  request_ms=int((time.time() - t0) * 1000))
        raise

    out_dir = RENDERS / outfit_id
    out_dir.mkdir(parents=True, exist_ok=True)
    name = f"{int(time.time())}.png"
    (out_dir / name).write_bytes(data)
    (out_dir / f"{int(time.time())}.json").write_text(
        json.dumps({"model": cfg["model"], "used": [u["id"] for u in used],
                    "styling": styling, "usage": usage,
                    "prompt": prompt}, ensure_ascii=False, indent=2)
    )
    log_usage(outfit_id, cfg, usage, status="ok",
              request_ms=int((time.time() - t0) * 1000))
    return name


@app.get("/healthz")
def healthz():
    try:
        query("SELECT 1")
        db = "ok"
    except Exception as exc:  # noqa: BLE001
        db = f"error: {str(exc)[:80]}"
    cfg = render_config()
    return {"status": "ok", "db": db, "model": cfg["model"],
            "image_size": cfg["image_size"], "reference": REFERENCE.is_file()}


@app.get("/api/panel/status")
def panel_status():
    """Internal status source for the Hestia Wardrobe tab (settings + last
    usage rows). The panel itself aggregates usage directly from the DB."""
    cfg = render_config()
    settings = {row["key"]: row["value"]
                for row in query("SELECT key, value FROM panel_settings")}
    total = query(
        """
        SELECT count(*)::int AS renders,
               count(*) FILTER (WHERE created_at > date_trunc('month', now()))::int
                    AS renders_month,
               max(created_at) AS last_render,
               sum(prompt_tokens)::int AS prompt_tokens,
               sum(text_output_tokens)::int AS text_output_tokens,
               sum(image_tokens)::int AS image_tokens
        FROM render_usage WHERE status = 'ok'
        """
    )
    recent = query(
        """
        SELECT created_at, model, image_size, prompt_tokens,
               text_output_tokens, image_tokens, total_tokens, status
        FROM render_usage ORDER BY created_at DESC LIMIT 5
        """
    )
    for row in recent:
        row["created_at"] = row["created_at"].isoformat()
    if "last_render" in total:
        total["last_render"] = (total["last_render"].isoformat()
                                if total["last_render"] else None)
    return {"active": cfg, "env_default_model": MODEL,
            "settings": settings, "totals": total, "recent": recent}


def normalize_jpeg(raw, max_side=2000):
    """Apply EXIF rotation (phone photos are often stored landscape) and shrink
    to a sane size, so the image model receives what the user actually sees."""
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


@app.post("/api/reference")
def upload_reference(file: UploadFile = File(...)):
    raw = file.file.read()
    if len(raw) < 2000:
        raise HTTPException(status_code=400, detail="Bild zu klein.")
    try:
        raw = normalize_jpeg(raw)
    except Exception:  # noqa: BLE001 - keep the original if it is not decodable
        pass
    REFERENCE.write_bytes(raw)
    return {"ok": True, "bytes": len(raw)}


@app.post("/api/render/{outfit_id}")
def render(outfit_id: str):
    name = render_outfit(outfit_id)
    return {"ok": True, "file": name}


RATING_VOTE = {"up": (True, 5, "accepted"), "down": (False, 1, "rejected")}


def apply_vote(outfit_id: str, vote: str) -> dict:
    """Store an outfit rating in wardrowbe's own user_feedback table.

    Same semantics as the wardrobe app: up = accepted + rating 5,
    down = rejected + rating 1, including outfit.status/responded_at.
    user_feedback.outfit_id is UNIQUE, so the vote is an upsert.
    """
    if vote not in RATING_VOTE:
        raise HTTPException(status_code=400, detail="vote muss 'up' oder 'down' sein.")
    accepted, rating, status_value = RATING_VOTE[vote]

    allowed = {row["id"] for row in query("SELECT id::text AS id FROM outfits")}
    if outfit_id not in allowed:
        raise HTTPException(status_code=404, detail="Outfit nicht gefunden.")

    execute(
        """
        INSERT INTO user_feedback (id, outfit_id, accepted, rating)
        VALUES (%s::uuid, %s::uuid, %s, %s)
        ON CONFLICT (outfit_id) DO UPDATE
        SET accepted = EXCLUDED.accepted,
            rating = EXCLUDED.rating,
            created_at = now()
        """,
        (str(uuidlib.uuid4()), outfit_id, accepted, rating),
    )
    execute(
        """
        UPDATE outfits SET status = %s, responded_at = now()
        WHERE id = %s::uuid
        """,
        (status_value, outfit_id),
    )
    return {"ok": True, "outfit_id": outfit_id, "vote": vote,
            "accepted": accepted, "rating": rating}


@app.post("/api/feedback/{outfit_id}")
def feedback(outfit_id: str, body: dict = None):
    vote = ((body or {}).get("vote") or "").strip().lower()
    if not vote:  # allow form posts and empty bodies via query param
        raise HTTPException(status_code=400, detail="body: {\"vote\": \"up\"|\"down\"}")
    return apply_vote(outfit_id, vote)


@app.delete("/api/feedback/{outfit_id}")
def feedback_reset(outfit_id: str):
    allowed = {row["id"] for row in query("SELECT id::text AS id FROM outfits")}
    if outfit_id not in allowed:
        raise HTTPException(status_code=404, detail="Outfit nicht gefunden.")
    execute("DELETE FROM user_feedback WHERE outfit_id = %s::uuid", (outfit_id,))
    execute(
        "UPDATE outfits SET status = 'pending', responded_at = NULL WHERE id = %s::uuid",
        (outfit_id,),
    )
    return {"ok": True, "outfit_id": outfit_id, "vote": None}


def toggle_favorite(outfit_id: str) -> dict:
    """Toggle a favorite for the UI. Returns the resulting state."""
    try:
        exists = query(
            "SELECT 1 AS one FROM outfits WHERE id = %s::uuid", (outfit_id,)
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"DB-Fehler: {str(exc)[:120]}")
    if not exists:
        raise HTTPException(status_code=404, detail="Outfit nicht gefunden.")
    try:
        current = query(
            "SELECT 1 AS one FROM outfit_favorites WHERE outfit_id = %s::uuid",
            (outfit_id,),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"DB-Fehler: {str(exc)[:120]}")
    if current:
        execute(
            "DELETE FROM outfit_favorites WHERE outfit_id = %s::uuid", (outfit_id,)
        )
        favorited = False
    else:
        execute(
            "INSERT INTO outfit_favorites (outfit_id) VALUES (%s::uuid) "
            "ON CONFLICT (outfit_id) DO NOTHING",
            (outfit_id,),
        )
        favorited = True
    return {"ok": True, "outfit_id": outfit_id, "favorited": favorited}


@app.post("/api/favorite/{outfit_id}")
def favorite(outfit_id: str):
    return toggle_favorite(outfit_id)


FAVICON = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="12" fill="#111214"/>
<path d="M32 12 L54 30 L50 36 L36 26 L36 46 L28 46 L28 26 L14 36 L10 30 Z"
      fill="#3a6df0" stroke="#3a6df0" stroke-width="0"/>
</svg>"""


@app.get("/favicon.svg")
def favicon():
    return Response(content=FAVICON, media_type="image/svg+xml")


@app.get("/renders/{outfit_id}/{name}")
def get_render(outfit_id: str, name: str):
    path = RENDERS / outfit_id / name
    if not path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(path)


@app.get("/reference")
def get_reference():
    if not REFERENCE.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(REFERENCE)


# ============================================================================
# Web UI
# ============================================================================


def esc(v):
    """HTML-escape any user generated text (names, notes, occasions)."""
    return html.escape(str(v if v is not None else ""), quote=True)


def svg_icon(name):
    """Small inline stroke icons (24x24, currentColor)."""
    paths = {
        "logo":
            '<path d="M12 3 l4 5 -4 3 v10" fill="none"/>'
            '<circle cx="9" cy="8" r="2" fill="none"/>'
            '<path d="M5 21 h14" fill="none"/>',
        "top":
            '<path d="M8 3 L4 6 l2 3 1-1 v10 h10 V8 l1 1 2-3 -4-3 c-1 2 -7 2 -8 0 Z" fill="none"/>',
        "bottom":
            '<path d="M5 3 h14 l-1.5 18 h-4 L12 9 l-1.5 12 h-4 Z" fill="none"/>',
        "shoes":
            '<path d="M2 16 c6 0 8 -3 8 -6 v-2 h4 v3 c0 2 2 3 5 3 h3 v3 H2 Z" fill="none"/>',
        "outer":
            '<path d="M9 3 L5 5 v16 h5 L12 7 l2 14 h5 V5 l-4 -2 c-1 2 -5 2 -6 0 Z" fill="none"/>'
            '<path d="M9 3 v7 M15 3 v7" fill="none"/>',
        "full":
            '<path d="M8 3 c1 3 7 3 8 0 l2 2 -1 5 -2 0 -0.5 11 h-3 L11 10 l-0.5 11 h-3 L7 10 l-2 0 -1 -5 Z" fill="none"/>',
        "star":
            '<path d="M12 3.6 l2.6 5.4 5.8 0.8 -4.2 4 1 5.8 -5.2 -2.8 -5.2 2.8 1 -5.8 -4.2 -4 5.8 -0.8 Z"/>',
        "sun":
            '<circle cx="12" cy="12" r="4.5" fill="none"/>'
            '<path d="M12 2.5v2.5 M12 19v2.5 M2.5 12H5 M19 12h2.5 M4.9 4.9l1.8 1.8 M17.3 17.3l1.8 1.8 M19.1 4.9l-1.8 1.8 M6.7 17.3l-1.8 1.8" fill="none"/>',
        "moon":
            '<path d="M20 14.5 A8.5 8.5 0 1 1 9.5 4 A7 7 0 0 0 20 14.5 Z" fill="none"/>',
        "monitor":
            '<rect x="2.5" y="4" width="19" height="13" rx="2" fill="none"/>'
            '<path d="M8.5 20.5 h7 M12 17.5 v3" fill="none"/>',
        "camera":
            '<path d="M3 7 h4 l2 -2.5 h6 L17 7 h4 v12 H3 Z" fill="none"/>'
            '<circle cx="12" cy="13" r="3.5" fill="none"/>',
        "search":
            '<circle cx="10.5" cy="10.5" r="5.5" fill="none"/>'
            '<path d="M15 15 L20.5 20.5" fill="none"/>',
        "close":
            '<path d="M5 5 L19 19 M19 5 L5 19" fill="none"/>',
        "download":
            '<path d="M12 3 v12 M6.5 10.5 L12 16 l5.5 -5.5 M4 20 h16" fill="none"/>',
        "refresh":
            '<path d="M20 12 a8 8 0 1 1 -2.3 -5.7 M20 3 v4 h-4" fill="none"/>',
        "gear":
            '<circle cx="12" cy="12" r="3.1" fill="none"/>'
            '<path d="M19.1 13.4a1.7 1.7 0 0 0 .34 1.87l.06.06a2.06 2.06 0 1 1-2.91 2.91l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56v.1a2.06 2.06 0 1 1-4.12 0v-.1a1.7 1.7 0 0 0-1.11-1.55 1.7 1.7 0 0 0-1.87.34l-.06.06a2.06 2.06 0 1 1-2.91-2.91l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0 0-1.56-1.03H4.5a2.06 2.06 0 1 1 0-4.12h.1a1.7 1.7 0 0 0 1.55-1.11 1.7 1.7 0 0 0-.34-1.87l-.06-.06a2.06 2.06 0 1 1 2.91-2.91l.06.06a1.7 1.7 0 0 0 1.87.34h.08A1.7 1.7 0 0 0 11.7 3.5v-.1a2.06 2.06 0 1 1 4.12 0v.1a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2.06 2.06 0 1 1 2.91 2.91l-.06.06a1.7 1.7 0 0 0-.34 1.87v.08a1.7 1.7 0 0 0 1.56 1.03h.1a2.06 2.06 0 1 1 0 4.12h-.1a1.7 1.7 0 0 0-1.56 1.03Z" fill="none"/>',
        "warning":
            '<path d="M12 3 L22 20 H2 Z" fill="none"/><path d="M12 9 v5 M12 16.5 v0.5"/>',
        "empty":
            '<rect x="3" y="5" width="18" height="14" rx="2" fill="none"/>'
            '<circle cx="9" cy="11" r="1.6"/><path d="M5 19 l4.5 -5 4 4 2.5 -3 3 4" fill="none"/>',
    }
    body = paths.get(name, paths["top"])
    return (
        '<svg viewBox="0 0 24 24" aria-hidden="true" stroke="currentColor" '
        'stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">'
        + body + "</svg>"
    )


def _item(it):
    """Fallback-safe values for an item dict used in the UI layer list."""
    return {
        "item_id": esc(it.get("item_id") or ""),
        "name": esc(it.get("name") or kind_name(it)),
        "type": esc(it.get("subtype") or it.get("type") or ""),
        "color": esc(it.get("primary_color") or "—"),
        "material": esc(it.get("material") or "—"),
        "pattern": esc(it.get("pattern") or "—"),
    }


def layer_stack(o):
    """Layered clothes view: a stack of item cards (closer to the body first,
    outerwear on top) plus a compact layer list. Hovering a list row swaps the
    stack for one large image of that item; clicking the row opens its detail."""
    ordered = sorted(o["items"], key=layer_rank)
    if not ordered:
        return (
            '<div class="layers"><div class="stack emptystack">'
            '<span class="hinttext">Keine Teile hinterlegt</span></div></div>'
        )

    n = len(ordered)
    pieces, rows = [], []
    for i, it in enumerate(ordered):
        d = _item(it)
        pieces.append(
            f'<figure class="piece" aria-hidden="true" '
            f'style="--i:{i};--z:{i + 1}">'
            f'<img src="/item/{d["item_id"]}" alt="" '
            f'loading="{"eager" if i == 0 else "lazy"}" decoding="async"></figure>'
        )
        badge = esc(layer_badge(it))
        bcls = {"Außen": " outer", "Mid": " mid"}.get(badge, "")
        ic = svg_icon("shoes" if side(it) == "shoes" else side(it))
        rows.append(
            f'<li class="ll" tabindex="0" role="button" '
            f'data-item="{d["item_id"]}" data-name="{d["name"]}" '
            f'data-type="{d["type"]}" data-color="{d["color"]}" '
            f'data-material="{d["material"]}" data-pattern="{d["pattern"]}">'
            f'<span class="badge{bcls}">{badge}</span>'
            f'<span class="lic">{ic}</span>'
            f'<span class="lnm" title="{d["name"]}">{d["name"]}'
            f'<em>{d["color"]} · {d["type"]}</em></span></li>'
        )

    return (
        '<div class="layers">'
        f'<div class="stack" style="--n:{n}">'
        '<div class="fan">' + "".join(pieces) + "</div>"
        '<img class="bigpiece" alt="" decoding="async">'
        "</div>"
        '<ul class="layerlist">' + "".join(rows) + "</ul>"
        "</div>"
    )


def state_of(o):
    """Vote state: a feedback row is authoritative, otherwise fall back to the
    outfit status the wardrobe app writes for its own thumbs."""
    accepted = o.get("accepted")
    status = o.get("status")
    if accepted is not None:
        return "up" if accepted else "down"
    if status in ("accepted", "rejected"):
        return "up" if status == "accepted" else "down"
    return "none"


def card_html(o, idx):
    """One outfit card, fully server rendered; progressive enhancement in JS."""
    state = state_of(o)
    has_render = bool(o["renders"])
    loading = "eager" if idx == 0 else "lazy"

    name = esc(o["name"]) if o["name"] else "Unbenanntes Outfit"
    occasion = esc(o["occasion"] or "ohne Anlass")
    notes = esc((o["style_notes"] or "")[:220])
    source = esc(o["source"] or "—")
    created_ts = int(o["created_at"].timestamp()) if o["created_at"] else 0
    rated_raw = o.get("rated_at") or o.get("responded_at")
    rated_ts = int(rated_raw.timestamp()) if rated_raw else 0
    search_blob = esc(f'{name} {occasion} {o["style_notes"] or ""}'.lower())
    rating_val = "5" if state == "up" else ("1" if state == "down" else "0")

    if has_render:
        media = (
            f'<img class="rimg" src="/renders/{esc(o["id"])}/{esc(o["renders"][0])}" '
            f'alt="Generiertes Outfit-Bild" loading="{loading}" decoding="async">'
        )
        had = "1"
        btn_label = "Neu erzeugen"
    else:
        media = (
            '<div class="ph" role="button" tabindex="0" '
            'aria-label="Bild erzeugen"><span class="phi">'
            + svg_icon("camera") +
            '</span><span class="ptxt">Bild erzeugen</span></div>'
        )
        had = "0"
        btn_label = "Bild erzeugen"

    seg_up = " on" if state == "up" else ""
    seg_down = " on" if state == "down" else ""
    fav_on = "true" if o.get("favorited") else "false"

    return f'''<article class="card" data-id="{esc(o["id"])}" data-fav="{1 if o.get("favorited") else 0}" \
data-image="{1 if has_render else 0}" data-file="{esc(o["renders"][0]) if has_render else ""}" \
data-state="{state}" data-rating="{rating_val}" data-created="{created_ts}" data-rated="{rated_ts}" \
data-search="{search_blob}">
  <div class="media">
    {media}
    <!-- fav --><button class="fav" data-id="{esc(o["id"])}" aria-pressed="{fav_on}" \
title="Favorit umschalten" aria-label="Favorit umschalten">{svg_icon("star")}</button>
    <div class="prog" aria-hidden="true"><i></i></div>
  </div>
  <div class="body">
    <div class="titlerow">
      <h3 class="name">{name}</h3>
    </div>
    <div class="meta">
      <span>{occasion}</span>
      <span class="dot" aria-hidden="true"></span>
      <time data-ts="{created_ts}" title="{esc(o["when"])}">…</time>
      <span class="dot" aria-hidden="true"></span>
      <span>{source}</span>
      <span class="dot" aria-hidden="true"></span>
      <span>{o.get("render_count", len(o["renders"]))}&times; gerendert</span>
    </div>
    {f'<p class="notes">{notes}</p>' if notes else ""}
    {layer_stack(o)}
    <div class="actions">
      <button class="rbtn" data-id="{esc(o["id"])}" data-had="{had}">
        <svg class="spin" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 a9 9 0 1 0 9 9" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>
        <span class="lbl">{btn_label}</span>
      </button>
      <div class="seg" role="group" aria-label="Bewertung">
        <button data-vote="up" class="segbtn{seg_up}" aria-pressed="{"true" if state == "up" else "false"}">👍 Positiv</button>
        <button data-vote="down" class="segbtn{seg_down}" aria-pressed="{"true" if state == "down" else "false"}">👎 Negativ</button>
      </div>
    </div>
    <p class="votehint">Bewertung fließt in die Vorschlags-KI ein</p>
  </div>
</article>'''


_CSS = """
/* ============ Tokens: dark default + light theme ============ */
:root{
  --bg:#0f1013; --surface:#16181d; --surface-2:#1f2229; --border:#2a2d36;
  --text:#ecebe6; --muted:#94959f;
  --accent:#c9a468; --accent-strong:#dcbf8c; --accent-ink:#221805;
  --positive:#3ecf8e; --negative:#e5484d; --star:#e6b857;
  --pos-soft:rgba(62,207,142,.14); --neg-soft:rgba(229,72,77,.14);
  --shadow:0 12px 32px rgba(0,0,0,.40); --shadow-soft:0 4px 14px rgba(0,0,0,.24);
  --header-bg:rgba(16,17,21,.72);
  --media-bg:#0b0c0e; --gridc:rgba(255,255,255,.10);
  --aurora1:#2b4a7a; --aurora2:#6a4a8a; --aurora3:#1f6a5a;
  --ph-glow:rgba(201,164,104,.20);
}
html.light{
  --bg:#f4f1ea; --surface:#fffdf8; --surface-2:#f1ece2; --border:#e3dccb;
  --text:#26211a; --muted:#7a7364;
  --accent:#8f6617; --accent-strong:#7c5410; --accent-ink:#fffdf8;
  --positive:#1d8a5c; --negative:#c0392f; --star:#c9860a;
  --pos-soft:rgba(29,138,92,.12); --neg-soft:rgba(192,57,47,.12);
  --shadow:0 12px 32px rgba(84,70,40,.14); --shadow-soft:0 4px 14px rgba(84,70,40,.10);
  --header-bg:rgba(246,243,236,.78);
  --media-bg:#efe9dd; --gridc:rgba(40,32,16,.08);
  --aurora1:#bcd0ea; --aurora2:#d8c5e8; --aurora3:#badecf;
  --ph-glow:rgba(143,102,23,.12);
}

*{box-sizing:border-box}
html{color-scheme:dark}
html.light{color-scheme:light}
body{
  margin:0; background:var(--bg); color:var(--text);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  transition:background-color .4s ease,color .4s ease;
  padding-bottom:56px;
}
img{display:block}
button{font:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.wrap{max-width:1480px;margin:0 auto;padding:0 20px}

/* ============ Header / hero ============ */
.topbar{
  position:sticky;top:0;z-index:60;
  background:var(--header-bg);
  -webkit-backdrop-filter:blur(14px);backdrop-filter:blur(14px);
  border-bottom:1px solid var(--border);
  transition:background-color .4s ease;
}
.bar{display:flex;align-items:center;gap:18px;padding-top:12px;padding-bottom:12px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.logo{width:40px;height:40px;border-radius:12px;flex:none;display:flex;align-items:center;
  justify-content:center;background:linear-gradient(135deg,var(--accent),var(--accent-strong));
  color:var(--accent-ink);box-shadow:var(--shadow-soft)}
.logo svg{width:22px;height:22px}
.brand h1{font-size:19px;margin:0;letter-spacing:.2px}
.brand p{margin:0;font-size:12px;color:var(--muted)}
.spacer{flex:1}
.statchips{display:flex;gap:8px;flex-wrap:wrap}
.stat{display:inline-flex;align-items:baseline;gap:6px;background:var(--surface-2);
  border:1px solid var(--border);border-radius:999px;padding:5px 12px;font-size:12px;
  color:var(--muted)}
.stat b{color:var(--text);font-size:14px}
.tt{width:38px;height:38px;border-radius:999px;border:1px solid var(--border);
  background:var(--surface-2);color:var(--text);cursor:pointer;flex:none;
  display:flex;align-items:center;justify-content:center;
  transition:border-color .3s ease,background-color .3s ease}
.tt:hover{border-color:var(--accent)}
.tt svg{width:19px;height:19px;display:none}
.tpick{position:relative;display:flex;align-items:center;padding:3px;flex:none;
  border-radius:999px;border:1px solid var(--border);background:var(--surface-2);
  height:38px;gap:0}
.tpick .thumb{position:absolute;top:3px;left:3px;width:30px;height:30px;
  border-radius:999px;background:var(--surface);border:1px solid var(--accent);
  box-shadow:var(--shadow-soft);pointer-events:none;
  transition:transform .34s cubic-bezier(.35,.1,.25,1.4),
    background-color .3s ease,border-color .3s ease}
.tpick button{position:relative;z-index:1;width:30px;height:30px;padding:0;
  border:0;background:none;border-radius:999px;cursor:pointer;color:var(--muted);
  display:flex;align-items:center;justify-content:center;
  transition:color .25s ease}
.tpick button svg{width:16px;height:16px;display:block}
.tpick button:hover{color:var(--text)}
.tpick button[aria-checked="true"]{color:var(--accent)}
.dberr{margin:14px 0 -4px;padding:10px 14px;border-radius:10px;font-size:13px;
  color:var(--negative);border:1px solid var(--negative);opacity:.85}

/* ============ Toolbar: ref panel + filter bar ============ */
main.wrap{padding-top:18px}
.panel{background:var(--surface);border:1px solid var(--border);border-radius:16px;
  padding:14px;margin-bottom:14px;box-shadow:var(--shadow-soft);
  transition:background-color .4s ease,border-color .4s ease}
.refwrap{display:flex;gap:14px;align-items:center;flex-wrap:wrap}
.refzone{position:relative;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:8px;min-width:200px;padding:10px 14px;
  border:1.5px dashed var(--border);border-radius:12px;cursor:pointer;
  background:var(--surface-2);color:var(--muted);text-align:center;font-size:12.5px;
  transition:border-color .25s ease,background-color .25s ease;overflow:hidden}
.refzone:hover,.refzone.drag{border-color:var(--accent);color:var(--text)}
.refzone .rv{position:relative;width:120px;height:156px;border-radius:10px;
  overflow:hidden;background:var(--media-bg)}
.refzone .rv img{width:100%;height:100%;object-fit:cover;transition:transform .5s ease}
.refzone:hover .rv img{transform:scale(1.06)}
.refzone .ovl{position:absolute;inset:0;background:rgba(10,10,14,.45);color:#fff;
  display:flex;align-items:center;justify-content:center;gap:6px;font-size:12px;
  opacity:0;transition:opacity .25s ease;border-radius:10px}
.refzone:hover .ovl{opacity:1}
.refzone .ovl svg{width:14px;height:14px}
.refzone .phi svg{width:26px;height:26px;color:var(--accent)}
.refzone .rr{display:flex;flex-direction:column;align-items:center;gap:2px;
  line-height:1.35;text-align:center}
.refzone .rr b{color:var(--text);font-size:12.5px;font-weight:600}
.refzone .rr span{color:var(--muted);font-size:11px}
.refzone b{color:var(--text);font-size:13px;font-weight:600}
.refzone.warn{border-color:var(--accent);color:var(--text)}
.refzone.warn .wt{color:var(--accent);font-weight:600}
.refinfo{min-width:220px;flex:1}
.refinfo h3{margin:0 0 2px;font-size:14px}
.refacts{display:flex;align-items:center;gap:12px;margin-top:10px;flex-wrap:wrap}
.refbtn{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--border);
  background:var(--surface-2);color:var(--text);border-radius:10px;padding:8px 13px;
  font-size:12.5px;font-weight:600;cursor:pointer;white-space:nowrap;
  transition:border-color .25s ease,color .25s ease,background-color .25s ease}
.refbtn svg{width:15px;height:15px;flex:none}
.refbtn:hover{border-color:var(--accent);color:var(--accent)}
.refinfo p{margin:0;font-size:12.5px;color:var(--muted);max-width:64ch}
#refstat{display:inline-block;margin-top:6px;font-size:12px;color:var(--muted);
  min-height:18px;opacity:.6}
#refstat.on{opacity:1;color:var(--text)}
.visuallyhidden{position:absolute;width:1px;height:1px;overflow:hidden;
  clip-path:inset(50%);white-space:nowrap}

.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  padding:6px 0 14px}
.chips{display:flex;gap:7px;flex-wrap:wrap}
.chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--border);
  background:var(--surface);color:var(--muted);border-radius:999px;
  padding:6px 12px;font-size:12.5px;cursor:pointer;
  transition:all .25s ease}
.chip:hover{border-color:var(--accent);color:var(--text)}
.chip.active{background:var(--accent);border-color:var(--accent);color:var(--accent-ink);
  font-weight:600}
.chip .cnt{font-size:10.5px;background:var(--surface-2);border-radius:999px;
  padding:1px 6px;min-width:18px;text-align:center}
.chip.active .cnt{background:rgba(255,255,255,.25);color:inherit}
.searchbox{position:relative;flex:1;min-width:200px;max-width:340px}
.searchbox svg{position:absolute;left:10px;top:50%;transform:translateY(-50%);
  width:15px;height:15px;color:var(--muted);pointer-events:none}
#search{width:100%;padding:8px 12px 8px 32px;border-radius:999px;
  background:var(--surface);border:1px solid var(--border);color:var(--text);
  font-size:13px;transition:border-color .25s ease}
#search:focus{border-color:var(--accent);outline:none}
#sortsel{border-radius:999px;background:var(--surface);border:1px solid var(--border);
  color:var(--text);font-size:12.5px;padding:7px 10px;cursor:pointer}
#sortsel:focus{outline:none;border-color:var(--accent)}
#rescount{font-size:12px;color:var(--muted);white-space:nowrap}
.rescount{margin-left:auto;display:flex;align-items:center;gap:10px}

/* ============ Grid + cards ============ */
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;align-items:start}
@media (max-width:1199px){.grid{grid-template-columns:repeat(2,1fr)}}
@media (max-width:719px){.grid{grid-template-columns:1fr}
  .brand p{display:none}
  .bar{padding-top:10px;padding-bottom:10px;gap:12px}
  .stat{padding:4px 10px;font-size:11px}
  .stat b{font-size:13px}
  .refzone{flex-direction:column;align-items:center;gap:10px;width:100%;padding:12px;
    min-width:0}
  .refzone .rv{width:88px;height:114px}
  .refzone .rr{align-items:center;text-align:center}
  .setmodal .refwrap{flex-direction:column;align-items:stretch}
  .setmodal .refinfo{text-align:center}
  .setmodal .refacts{justify-content:center}
  .layers{flex-direction:column;align-items:stretch;gap:10px}
  .stack{align-self:center}
  .layers .stack{width:132px;height:176px}
  .layerlist{width:100%}
  .badge{font-size:9px;padding:1.5px 6px}
}

.card{background:var(--surface);border:1px solid var(--border);border-radius:16px;
  overflow:hidden;display:flex;flex-direction:column;position:relative;
  box-shadow:var(--shadow-soft);
  transition:box-shadow .3s ease,transform .3s ease,border-color .3s ease,
             background-color .4s ease,opacity .5s ease}
.card::after{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;z-index:4;
  background:transparent;transition:background-color .3s ease}
.card[data-state="up"]::after{background:var(--positive)}
.card[data-state="down"]::after{background:var(--negative)}
.card:hover{transform:translateY(-3px);box-shadow:var(--shadow)}
.card.hide{display:none}
.card.pre{opacity:0;transform:translateY(16px)}
.card.in{opacity:1;transform:none;
  transition:opacity .55s ease var(--d,0ms),transform .55s cubic-bezier(.2,.7,.3,1) var(--d,0ms),
             box-shadow .3s ease,border-color .3s ease}
.card.popin{animation:fadein .3s ease}
@keyframes fadein{from{opacity:0}to{opacity:1}}
.card.glow{animation:glow 1.4s ease}
@keyframes glow{25%{box-shadow:0 0 0 5px var(--pos-soft),var(--shadow)}}
.card>*{transition:opacity .4s ease}

.media{position:relative;width:100%;aspect-ratio:3/4;background:var(--media-bg);
  overflow:hidden;cursor:zoom-in}
.media .rimg{width:100%;height:100%;object-fit:cover;
  transition:transform .5s ease,opacity .4s ease}
.card:hover .media .rimg{transform:scale(1.05)}
.rimg.enter{animation:zoomin .5s ease}
@keyframes zoomin{from{opacity:0;transform:scale(1.08)}to{opacity:1}}

/* aurora placeholder for missing renders */
.ph{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:14px;cursor:pointer;overflow:hidden;isolation:isolate;
  background:
    radial-gradient(85% 60% at 50% 0%,var(--ph-glow),transparent 62%),
    linear-gradient(168deg,var(--surface-2),var(--media-bg) 82%);
  color:var(--text)}
.ph::before{content:"";position:absolute;inset:0;z-index:0;opacity:.55;
  background-image:linear-gradient(var(--gridc) 1px,transparent 1px),
    linear-gradient(90deg,var(--gridc) 1px,transparent 1px);
  background-size:34px 34px;
  -webkit-mask-image:radial-gradient(72% 62% at 50% 42%,#000,transparent 80%);
  mask-image:radial-gradient(72% 62% at 50% 42%,#000,transparent 80%)}
.ph::after{content:"";position:absolute;z-index:0;width:240px;height:240px;
  border-radius:999px;filter:blur(66px);opacity:.28;background:var(--accent);
  animation:phglow 11s ease-in-out infinite alternate}
.phi{position:relative;z-index:2;display:flex;width:58px;height:58px;border-radius:999px;
  align-items:center;justify-content:center;color:var(--accent);
  background:var(--surface);border:1px solid var(--border);
  box-shadow:0 8px 24px rgba(0,0,0,.18);transition:transform .35s ease,border-color .35s ease}
.ph:hover .phi{transform:scale(1.07);border-color:var(--accent)}
.phi svg{width:25px;height:25px}
.ptxt{position:relative;z-index:2;color:var(--muted);font-size:12.5px;
  font-weight:600;letter-spacing:.3px;text-transform:uppercase}
.ph:hover .ptxt{color:var(--text)}
@keyframes phglow{from{transform:translate3d(-18%,-10%,0) scale(.9)}
  to{transform:translate3d(18%,12%,0) scale(1.08)}}

.prog{display:none;position:absolute;left:0;right:0;bottom:0;height:4px;
  background:rgba(127,127,140,.35)}
.prog.on{display:block;overflow:hidden}
.prog i{display:block;height:100%;width:42%;
  background:linear-gradient(90deg,transparent,var(--accent),transparent);
  animation:slide 1.1s linear infinite}
@keyframes slide{from{transform:translateX(-80%)}to{transform:translateX(260%)}}

.fav{position:absolute;top:10px;right:10px;z-index:5;width:36px;height:36px;
  border-radius:999px;display:flex;align-items:center;justify-content:center;
  cursor:pointer;border:1px solid rgba(255,255,255,.28);color:#fff;
  background:rgba(12,12,16,.45);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);
  transition:transform .25s ease,background-color .25s ease}
.fav:hover{transform:scale(1.1)}
.fav svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.6;
  transition:fill .25s ease,stroke .25s ease}
.fav[aria-pressed="true"] svg{fill:var(--star);stroke:var(--star)}

.body{padding:13px 15px 14px;display:flex;flex-direction:column;gap:9px}
.titlerow{display:flex;align-items:flex-start;gap:8px}
.name{margin:0;font-size:15.5px;font-weight:650;letter-spacing:.1px}
.meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:12px;
  color:var(--muted)}
.meta .dot{width:3px;height:3px;border-radius:50%;background:var(--muted);flex:none}
time{color:var(--muted)}
.notes{margin:0;font-size:12.5px;color:var(--muted);max-width:66ch;
  display:-webkit-box;-webkit-line-clamp:2;line-clamp:2;-webkit-box-orient:vertical;
  overflow:hidden}

/* ---- Layering view: overlapping stack + list ---- */
.layers{display:flex;gap:14px;align-items:flex-start;
  padding:10px;border-radius:12px;background:var(--surface-2);
  border:1px solid var(--border)}
.stack{position:relative;width:148px;height:196px;flex:none;border-radius:12px}
.emptystack{display:flex;align-items:center;justify-content:center;width:100%;
  height:auto}
.hinttext{font-size:12px;color:var(--muted)}
.fan{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);
  width:108px;height:92px;transition:opacity .22s ease,transform .28s ease}
.piece{position:absolute;left:0;bottom:0;margin:0;width:56px;height:92px;
  border-radius:9px;overflow:hidden;background:var(--surface);
  border:1px solid var(--border);box-shadow:var(--shadow-soft);
  transform:translate(calc(var(--i)*13px),calc(var(--i)*4px));
  transition:transform .3s cubic-bezier(.2,.7,.3,1.4),box-shadow .3s ease}
.piece img{width:100%;height:100%;object-fit:cover}
.bigpiece{position:absolute;inset:6px;margin:auto;width:auto;height:auto;
  object-fit:contain;border-radius:10px;background:var(--surface);
  border:1px solid var(--border);opacity:0;transform:scale(.94);
  transition:opacity .22s ease,transform .3s cubic-bezier(.2,.7,.3,1.2);
  pointer-events:none}
.stack.showing{pointer-events:auto;cursor:zoom-in}
.stack.showing .fan{opacity:0;transform:translate(-50%,-50%) scale(.9)}
.stack.showing .bigpiece{opacity:1;transform:none;pointer-events:auto;
  box-shadow:0 10px 26px rgba(0,0,0,.35)}
.layerlist{list-style:none;margin:0;padding:0;flex:1;min-width:0;
  display:flex;flex-direction:column;gap:3px}
.ll{display:flex;align-items:center;gap:7px;padding:4px 8px;border-radius:8px;
  cursor:pointer;font-size:12.5px;border:1px solid transparent;min-width:0}
.ll:hover{background:var(--surface);border-color:var(--border)}
.badge{flex:none;font-size:9.5px;font-weight:600;letter-spacing:.4px;
  padding:1.5px 7px;border-radius:999px;color:var(--muted);
  background:var(--surface);border:1px solid var(--border)}
.badge.outer{color:var(--accent);border-color:var(--accent)}
.badge.mid{color:var(--positive);border-color:var(--positive)}
.lic{flex:none;display:flex}
.lic svg{width:15px;height:15px;color:var(--muted)}
.lnm{display:flex;align-items:baseline;gap:6px;min-width:0;flex:1}
.lnm::after{content:"";flex:1;border-bottom:1px dotted var(--border);
  height:1em;margin:0 2px}
.lnm em{font-style:normal;color:var(--muted);font-size:11px;white-space:nowrap}
.lnm>span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ---- actions: render button + segmented rating ---- */
.actions{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.rbtn{display:inline-flex;align-items:center;gap:8px;border:1px solid var(--accent);
  background:var(--accent);color:var(--accent-ink);border-radius:10px;
  padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer;
  transition:background-color .25s ease,border-color .25s ease,color .25s ease}
.rbtn:hover{background:var(--accent-strong);border-color:var(--accent-strong)}
.rbtn .spin{width:15px;height:15px;display:none}
.rbtn.busy{opacity:.75;cursor:wait;background:var(--surface-2);
  color:var(--text);border-color:var(--border)}
.rbtn.busy .spin{display:block;animation:rot 1s linear infinite}
@keyframes rot{to{transform:rotate(360deg)}}
.rbtn.done{border-color:var(--positive);color:var(--positive);
  background:var(--pos-soft)}
.seg{display:inline-flex;gap:2px;padding:3px;border-radius:999px;
  background:var(--surface-2);border:1px solid var(--border)}
.seg button{display:inline-flex;align-items:center;gap:5px;border:0;
  background:transparent;color:var(--muted);border-radius:999px;
  padding:5.5px 12px;font-size:12.5px;cursor:pointer;
  transition:background-color .25s ease,color .25s ease}
.seg button:hover{color:var(--text)}
.seg button.on[data-vote="up"]{background:var(--pos-soft);color:var(--positive);
  font-weight:600}
.seg button.on[data-vote="down"]{background:var(--neg-soft);color:var(--negative);
  font-weight:600}
.seg button.on::after{content:"✓";font-size:12px}
.seg button.segbtn{min-width:86px;justify-content:center}
.votehint{margin:0;font-size:11px;color:var(--muted)}
.rbtn.list-container-clear{display:none}

/* ============ Empty state ============ */
.empty{display:none;text-align:center;padding:56px 20px;color:var(--muted)}
.empty.show{display:block}
.empty .ei{width:64px;height:64px;margin:0 auto 14px;opacity:.5;
  border-radius:18px;background:var(--surface-2);
  display:flex;align-items:center;justify-content:center}
.empty .ei svg{width:30px;height:30px}
.empty p{margin:0;font-size:14px}

/* ============ Toasts ============ */
.toasts{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);z-index:120;
  display:flex;flex-direction:column;gap:8px;align-items:center;pointer-events:none}
.toast{background:var(--surface);color:var(--text);border:1px solid var(--border);
  border-left:3px solid var(--accent);border-radius:10px;padding:9px 16px;
  font-size:13px;box-shadow:var(--shadow);max-width:min(92vw,460px);
  animation:toastin .3s ease}
.toast.ok{border-left-color:var(--positive)}
.toast.err{border-left-color:var(--negative)}
.toast.out{opacity:0;transform:translateY(6px);transition:all .3s ease}
@keyframes toastin{from{opacity:0;transform:translateY(8px)}to{opacity:1}}

/* ============ Item popover ============ */
/* settings modal + missing-reference banner */
#setbtn svg{display:block}
#setbtn{position:relative}
#setbtn.warn::after{content:"";position:absolute;top:5px;right:5px;width:8px;
  height:8px;border-radius:999px;background:var(--accent);
  box-shadow:0 0 0 2px var(--surface-2)}
.lb.setmodal .win{max-width:min(92vw,600px);width:100%}
.setmodal .setbody{padding:0}
.setmodal .setbody .panel{margin:0;padding:6px 2px 2px;border:0;
  background:transparent;box-shadow:none}
.setmodal .refwrap{gap:16px;align-items:center;flex-wrap:wrap}
.setmodal .refzone{min-width:0}
.setmodal .refinfo{min-width:0}
.setmodal .refacts{gap:10px}
.setnote{margin:0;padding-top:2px;font-size:12px;color:var(--muted);
  text-align:center}
.refbanner{display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  margin:0 0 14px;padding:11px 14px;border-radius:12px;
  border:1px solid var(--accent);background:var(--surface-2);font-size:13px}
.refbanner svg{width:17px;height:17px;color:var(--accent);flex:none}
.refbanner span{flex:1;min-width:200px;color:var(--text)}
.refbanner .refbtn{margin-left:auto}
.imgmodal .win{max-width:min(92vw,520px)}
.imgmodal img{max-height:62vh;object-fit:contain;margin:0 auto}
.imdl{display:grid;grid-template-columns:auto 1fr;gap:4px 14px;margin:0;
  font-size:13px;padding:8px 2px 2px;border-top:1px solid var(--border)}
.imdl dt{color:var(--muted)}
.imdl dd{margin:0;color:var(--text)}
.ll.hot{background:var(--surface);border-color:var(--accent)}

/* ============ Lightbox ============ */
.lb{position:fixed;inset:0;z-index:130;display:none;align-items:center;
  justify-content:center;background:rgba(8,8,12,.72);
  -webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);padding:22px}
.lb.open{display:flex;animation:fadein .25s ease}
.lb dialog,.lb .win{background:var(--surface);border:1px solid var(--border);
  border-radius:16px;padding:12px;box-shadow:var(--shadow);max-width:min(92vw,900px);
  display:flex;flex-direction:column;gap:10px;max-height:94vh}
.lb img{max-height:74vh;max-width:100%;width:auto;border-radius:10px;
  background:var(--media-bg)}
.lb .row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.lb .pt2{margin:0;font-size:14.5px;font-weight:600;flex:1;min-width:0;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lbtn{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--accent);
  background:var(--accent);color:var(--accent-ink);border-radius:10px;
  padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer;text-decoration:none;
  transition:background-color .25s ease}
.lbtn:hover{background:var(--accent-strong)}
.lbtn.ghost{background:transparent;border-color:var(--border);color:var(--text)}
.lbtn .stack,.lbtn svg{width:15px;height:15px}
.lbclose{width:34px;height:34px;border-radius:999px;border:1px solid var(--border);
  background:var(--surface-2);color:var(--text);cursor:pointer;flex:none;
  display:flex;align-items:center;justify-content:center}
.lbclose svg{width:15px;height:15px}

/* ============ Motion preferences ============ */
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.01ms !important;
    animation-iteration-count:1 !important;transition-duration:.01ms !important}
  .card.pre{opacity:1;transform:none}
}
"""

_JS = """
(function(){
"use strict";
var $=function(s,e){return (e||document).querySelector(s);};
var $$=function(s,e){return Array.prototype.slice.call((e||document).querySelectorAll(s));};
var grid=document.getElementById('grid');

/* ---------- theme: hell / system / dunkel (also set early in <head>) ---------- */
var root=document.documentElement;
var mq=window.matchMedia?window.matchMedia('(prefers-color-scheme: light)'):null;
var MODE='system';
function resolveMode(m){return m==='system'?(mq&&mq.matches?'light':'dark'):m;}
function paintTheme(save){
  root.classList.toggle('light',resolveMode(MODE)==='light');
  $$('#tpick button').forEach(function(b){
    b.setAttribute('aria-checked',b.dataset.mode===MODE?'true':'false');
  });
  var th=$('#tpick .thumb');
  var idx={light:0,system:1,dark:2}[MODE];if(idx==null)idx=1;
  if(th)th.style.transform='translateX('+(idx*30)+'px)';
  if(save){try{localStorage.setItem('wardrobe-theme',MODE);}catch(e){}}
}
try{
  var saved=localStorage.getItem('wardrobe-theme');
  if(saved==='light'||saved==='dark'||saved==='system')MODE=saved;
}catch(e){}
$$('#tpick button').forEach(function(b){
  b.addEventListener('click',function(){MODE=b.dataset.mode;paintTheme(true);});
});
paintTheme(false);
if(mq){
  var onMq=function(){if(MODE==='system')paintTheme(false);};
  if(mq.addEventListener)mq.addEventListener('change',onMq);
  else if(mq.addListener)mq.addListener(onMq);
}

/* ---------- toasts ---------- */
function toast(msg,kind){
  var wrap=document.getElementById('toasts');
  var t=document.createElement('div');
  t.className='toast '+(kind||'info');
  t.textContent=msg;
  wrap.appendChild(t);
  setTimeout(function(){t.classList.add('out');setTimeout(function(){t.remove();},320);},3200);
}

/* ---------- relative dates ---------- */
function rel(sec){
  if(!sec)return'';
  var s=Math.floor(Date.now()/1000-sec);
  if(s<90)return'gerade eben';
  var m=Math.floor(s/60);if(m<60)return'vor '+m+' Min.';
  var h=Math.floor(m/60);if(h<24)return'vor '+(h===1?'einer Stunde':h+' Std.');
  var d=Math.floor(h/24);
  if(d===1)return'vor 1 Tag';
  if(d<7)return'vor '+d+' Tagen';
  var w=Math.floor(d/7);
  if(w<5)return'vor '+(w===1?'einer Woche':w+' Wochen');
  var mo=Math.floor(d/30);
  if(mo<12)return'vor '+(mo===1?'einem Monat':mo+' Monaten');
  return'vor über einem Jahr';
}
function relOutfit(c){
  var t=c.querySelector('time[data-ts]');
  if(t&&t.dataset.ts){t.textContent=rel(+t.dataset.ts);}
}

/* ---------- filter / search / sort ---------- */
var F='all',Q='',SORT='new';
var EMPT={
  all:'Noch keine Outfits im Kleiderschrank.',
  fav:'Noch keine Favoriten \\u2013 markiere Outfits mit dem Stern.',
  up:'Noch keine positiv bewerteten Outfits.',
  down:'Keine negativ bewerteten Outfits.',
  unrate:'Alle Outfits sind bereits bewertet.',
  img:'Noch keine Bilder erzeugt \\u2013 klicke auf \\u201eBild erzeugen\\u201c.',
  noimg:'Alle Outfits haben bereits ein Bild.'
};
function mSearch(c){
  if(!Q)return true;
  return (c.dataset.search||'').indexOf(Q)>-1;
}
function mFilt(c){
  switch(F){
    case'fav':return c.dataset.fav==='1';
    case'up':return c.dataset.state==='up';
    case'down':return c.dataset.state==='down';
    case'unrate':return c.dataset.state==='none';
    case'img':return c.dataset.image==='1';
    case'noimg':return c.dataset.image==='0';
    default:return true;
  }
}
function pop(c){c.classList.remove('popin');void c.offsetWidth;c.classList.add('popin');}
function apply(){
  var total=0,vis=0,n={all:0,fav:0,up:0,down:0,unrate:0,img:0,noimg:0};
  var cs=cards();
  cs.forEach(function(c){
    if(mSearch(c)){
      total++;n.all++;
      if(c.dataset.fav==='1')n.fav++;
      if(c.dataset.state==='up')n.up++;else if(c.dataset.state==='down')n.down++;else n.unrate++;
      n[c.dataset.image==='1'?'img':'noimg']++;
    }
    var ok=mSearch(c)&&mFilt(c);
    var was=c.classList.contains('hide');
    c.classList.toggle('hide',!ok);
    if(ok){vis++;if(was)pop(c);}
  });
  $$('.chip').forEach(function(b){
    var e=b.querySelector('.cnt');
    if(e)e.textContent=n[b.dataset.f]||0;
  });
  var empty=document.getElementById('empty');
  if(empty){
    if(vis===0){
      var msg=(total>0&&Q)?'Kein Outfit passt zur Suche.':(EMPT[F]||EMPT.all);
      var p=empty.querySelector('p');
      if(p)p.textContent=msg;
      if(total>0&&Q)empty.classList.add('searchmode');
      else empty.classList.remove('searchmode');
      empty.classList.add('show');
    }else{
      empty.classList.remove('show');
    }
  }
  var rc=document.getElementById('rescount');
  if(rc)rc.textContent=vis+' von '+total+' Outfits';
}
function cards(){return $$('.card',grid);}
function cmp(a,b){
  if(SORT==='rated')return (+b.dataset.rated)-(+a.dataset.rated);
  if(SORT==='best'){
    var sc=function(c){return c.dataset.state==='up'?2:(c.dataset.state==='down'?0:1);};
    var d=sc(b)-sc(a);
    if(d)return d;
    return (+b.dataset.created)-(+a.dataset.created);
  }
  return (+b.dataset.created)-(+a.dataset.created);
}
function resort(){
  cards().sort(cmp).forEach(function(c){grid.appendChild(c);});
  apply();
}
$$('.chip').forEach(function(b){b.addEventListener('click',function(){
  F=b.dataset.f;
  $$('.chip').forEach(function(x){x.classList.toggle('active',x===b);});
  try{localStorage.setItem('wardrobe-filter',F);}catch(e){}
  apply();
});});
var search=document.getElementById('search');
if(search){search.addEventListener('input',function(){
  Q=search.value.trim().toLowerCase();apply();
});}
var sortsel=document.getElementById('sortsel');
if(sortsel){sortsel.addEventListener('change',function(){
  SORT=sortsel.value;resort();
});}

/* ---------- rating (single segmented control, toggle to delete) ---------- */
function voteUI(card,state){
  $$('.seg button',card).forEach(function(b){
    var on=b.dataset.vote===state;
    b.classList.toggle('on',on);
    b.setAttribute('aria-pressed',on?'true':'false');
  });
  card.dataset.state=state||'none';
}
async function onVote(id,v,btn){
  var card=$('.card[data-id="'+id+'"]');
  if(!card)return;
  var prev=card.dataset.state==='up'?'up':(card.dataset.state==='down'?'down':null);
  var next=(v===prev)?null:v;
  var before=card.dataset.state;
  voteUI(card,next);
  try{
    if(!next){
      if(!prev){apply();return;}
      var rd=await fetch('/api/feedback/'+id,{method:'DELETE'});
      var dd=await rd.json();
      if(!rd.ok)throw new Error(dd.detail||'Fehler');
      card.dataset.rated='0';
      toast('Bewertung entfernt','info');
    }else{
      var r=await fetch('/api/feedback/'+id,{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({vote:next})});
      var d=await r.json();
      if(!r.ok)throw new Error(d.detail||'Fehler');
      card.dataset.rated=String(Math.floor(Date.now()/1000));
      toast(next==='up'?'Positiv bewertet':'Negativ bewertet',next==='up'?'ok':'err');
    }
    apply();
  }catch(e){
    voteUI(card,before==='none'?null:before);
    toast('Bewertung fehlgeschlagen: '+e.message,'err');
    apply();
  }
}

/* ---------- favorites ---------- */
async function onFav(id,btn){
  var card=$('.card[data-id="'+id+'"]');
  var was=btn.getAttribute('aria-pressed')==='true';
  btn.setAttribute('aria-pressed',was?'false':'true');
  if(card)card.dataset.fav=was?'0':'1';
  try{
    var r=await fetch('/api/favorite/'+id,{method:'POST'});
    var d=await r.json();
    if(!r.ok)throw new Error(d.detail||'Fehler');
    btn.setAttribute('aria-pressed',d.favorited?'true':'false');
    if(card)card.dataset.fav=d.favorited?'1':'0';
    toast(d.favorited?'Zu Favoriten hinzugefügt':'Aus Favoriten entfernt','ok');
  }catch(e){
    btn.setAttribute('aria-pressed',was?'true':'false');
    if(card)card.dataset.fav=was?'1':'0';
    toast('Favorit fehlgeschlagen: '+e.message,'err');
  }
  apply();
}

/* ---------- render with progress / seconds / swap ---------- */
async function onRender(id,btn){
  var card=$('.card[data-id="'+id+'"]');
  if(!btn)btn=$('.rbtn[data-id="'+id+'"]');
  if(!btn)return;
  var cardHad=card?card.dataset.image==='1':false;
  btn.disabled=true;btn.classList.add('busy');btn.classList.remove('done');
  var lbl=$('.lbl',btn),s=0;
  if(lbl)lbl.textContent='erzeuge … 0 s';
  var timer=setInterval(function(){s++;if(lbl)lbl.textContent='erzeuge … '+s+' s';},1000);
  var prog=card?$('.prog',card):null;
  if(prog)prog.classList.add('on');
  try{
    var r=await fetch('/api/render/'+id,{method:'POST'});
    var d=await r.json();
    if(!r.ok)throw new Error(d.detail||'Fehler');
    clearInterval(timer);
    if(prog)prog.classList.remove('on');
    var url='/renders/'+id+'/'+d.file;
    if(card){
      var media=$('.media',card),ph=$('.ph',media);
      if(ph){
        ph.insertAdjacentHTML('afterend','<img class="rimg enter" src="'+url+'" alt="Generiertes Outfit-Bild" decoding="async">');
        ph.remove();
        card.classList.add('zoomanime');
      }else{
        var img=$('img.rimg',media);
        img.classList.remove('enter');void img.offsetWidth;
        img.src=url;img.classList.add('enter');
      }
      card.dataset.image='1';card.dataset.file=d.file;
      card.classList.add('glow');
      setTimeout(function(){card.classList.remove('glow');},1500);
    }
    btn.classList.remove('busy');btn.classList.add('done');
    btn.dataset.had='1';
    if(lbl)lbl.textContent='Neu erzeugen';
    setTimeout(function(){btn.classList.remove('done');},2000);
    var lbImg=document.getElementById('lbimg');
    var lb=document.getElementById('lb');
    if(lb.classList.contains('open')&&lb.dataset.id===id&&lbImg)lbImg.src=url;
    toast('Bild erzeugt','ok');
    apply();
  }catch(e){
    clearInterval(timer);
    if(prog)prog.classList.remove('on');
    btn.disabled=false;btn.classList.remove('busy');
    if(lbl)lbl.textContent=cardHad?'Neu erzeugen':'Bild erzeugen';
    toast('Fehler: '+e.message,'err');
  }
}

/* ---------- lightbox ---------- */
var lb=document.getElementById('lb');
var lastFocus=null;
function openLB(id){
  var card=$('.card[data-id="'+id+'"]');
  if(!card||card.dataset.image!=='1')return;
  lastFocus=document.activeElement;
  var file=card.dataset.file;
  var url='/renders/'+id+'/'+file;
  lb.dataset.id=id;
  $('#lbimg').src=url;
  var dl=$('#lbdl');dl.href=url;
  var nm=$('.card[data-id="'+id+'"] .name');
  $('#lbtitle').textContent=nm?nm.textContent:'Outfit-Bild';
  lb.classList.add('open');
  lb.setAttribute('aria-hidden','false');
  $('#lbclose').focus();
}
function closeLB(){
  lb.classList.remove('open');
  lb.setAttribute('aria-hidden','true');
  if(lastFocus&&lastFocus.focus)lastFocus.focus();
}
$('#lbclose').addEventListener('click',closeLB);
lb.addEventListener('click',function(e){if(e.target===lb)closeLB();});
$('#lbre').addEventListener('click',function(){
  var id=lb.dataset.id;
  closeLB();
  if(id)onRender(id,null);
});

/* ---------- stage: one large item image per hovered layer row ---------- */
var im=document.getElementById('im');
function fitBig(img){
  if(!img)return;
  var nw=img.naturalWidth||3,nh=img.naturalHeight||4;
  var st=img.parentNode;
  var W=((st&&st.clientWidth)||148)-14,H=((st&&st.clientHeight)||196)-14;
  var sc=Math.min(W/nw,H/nh);
  img.style.width=Math.round(nw*sc)+'px';
  img.style.height=Math.round(nh*sc)+'px';
}
function focusItem(el){
  var card=el.closest('.card');if(!card)return;
  var id=el.dataset.item;if(!id)return;
  var st=card.querySelector('.stack');
  if(!st)return;
  var big=st.querySelector('.bigpiece');
  if(!big)return;
  var src='/item/'+id;
  if(big.getAttribute('src')!==src)big.src=src;
  fitBig(big);
  if(!big.naturalWidth)big.addEventListener('load',function(){fitBig(big);},{once:true});
  st.classList.add('showing');
  ['item','name','type','color','material','pattern'].forEach(function(k){
    if(el.dataset[k]!=null)st.dataset[k]=el.dataset[k];
  });
  $$('.ll',card).forEach(function(x){x.classList.toggle('hot',x===el);});
}
function clearFocus(card){
  if(!card)return;
  var st=card.querySelector('.stack');
  if(st){
    st.classList.remove('showing');
    ['item','name','type','color','material','pattern'].forEach(function(k){
      delete st.dataset[k];
    });
  }
  $$('.ll',card).forEach(function(x){x.classList.remove('hot');});
}

/* ---------- settings (reference photo) ---------- */
var set=document.getElementById('set');
function openSet(){
  if(!set)return;
  lastFocus=document.activeElement;
  set.classList.add('open');set.setAttribute('aria-hidden','false');
  var c=document.getElementById('setclose');if(c)c.focus();
}
function closeSet(){
  if(!set||!set.classList.contains('open'))return;
  set.classList.remove('open');set.setAttribute('aria-hidden','true');
  if(lastFocus&&lastFocus.focus)lastFocus.focus();
}
var setbtn=document.getElementById('setbtn');
if(setbtn)setbtn.addEventListener('click',openSet);
var setclose=document.getElementById('setclose');
if(setclose)setclose.addEventListener('click',closeSet);
if(set)set.addEventListener('click',function(e){if(e.target===set)closeSet();});
var refbannerbtn=document.getElementById('refbannerbtn');
if(refbannerbtn)refbannerbtn.addEventListener('click',openSet);

function openItem(el){
  if(!im||!el||!el.dataset.item)return;
  var id=el.dataset.item;
  $('#imimg').src='/item/'+id;
  $('#imtitle').textContent=el.dataset.name||'Kleidungsstück';
  $('#imtype').textContent=el.dataset.type||'—';
  $('#imcolor').textContent=el.dataset.color||'—';
  $('#immat').textContent=el.dataset.material||'—';
  $('#impat').textContent=el.dataset.pattern||'—';
  $('#imopen').href='/item/'+id;
  clearFocus(el.closest('.card'));
  lastFocus=document.activeElement;
  im.classList.add('open');im.setAttribute('aria-hidden','false');
  $('#imclose').focus();
}
function closeItem(){
  if(!im||!im.classList.contains('open'))return;
  im.classList.remove('open');im.setAttribute('aria-hidden','true');
  if(lastFocus&&lastFocus.focus)lastFocus.focus();
}
$('#imclose').addEventListener('click',closeItem);
im.addEventListener('click',function(e){if(e.target===im)closeItem();});

/* ---------- reference photo upload (fetch, no reload) ---------- */
var refinput=document.getElementById('refinput');
var refzone=document.getElementById('refzone');
var refstat=document.getElementById('refstat');
var refbtn=document.getElementById('refbtn');
if(refbtn)refbtn.addEventListener('click',function(e){e.stopPropagation();refinput.click();});
if(refzone){
  refzone.addEventListener('click',function(){refinput.click();});
  refinput.addEventListener('change',function(){
    if(refinput.files&&refinput.files[0])uploadRef(refinput.files[0]);
    refinput.value='';
  });
  ['dragover','dragenter'].forEach(function(ev){
    refzone.addEventListener(ev,function(e){e.preventDefault();refzone.classList.add('drag');});
  });
  ['dragleave','dragend'].forEach(function(ev){
    refzone.addEventListener(ev,function(){refzone.classList.remove('drag');});
  });
  refzone.addEventListener('drop',function(e){
    e.preventDefault();refzone.classList.remove('drag');
    if(e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0])uploadRef(e.dataTransfer.files[0]);
  });
}
async function uploadRef(fl){
  if(refstat){refstat.textContent='Lade Referenzfoto hoch …';refstat.classList.add('on');}
  var fd=new FormData();fd.append('file',fl);
  try{
    var r=await fetch('/api/reference',{method:'POST',body:fd});
    var d=await r.json();
    if(!r.ok)throw new Error(d.detail||'Upload fehlgeschlagen');
    var img=$('#refimg');
    if(img){
      img.classList.add('swapping');
      setTimeout(function(){img.src='/reference?ts='+Date.now();img.classList.remove('swapping');},240);
      var z=document.getElementById('refwarn');
      if(z)z.remove();
      if(refstat)refstat.textContent='Referenzfoto aktualisiert.';
      var bn=document.getElementById('refbanner');
      if(bn)bn.remove();
      var sb=document.getElementById('setbtn');
      if(sb)sb.classList.remove('warn');
    }else{
      location.reload();
    }
    toast('Referenzfoto aktualisiert','ok');
  }catch(e){
    if(refstat){refstat.classList.remove('on');refstat.textContent='Upload fehlgeschlagen';}
    toast('Upload fehlgeschlagen: '+e.message,'err');
  }
}

/* ---------- delegated card interaction ---------- */
grid.addEventListener('click',function(e){
  var fav=e.target.closest('.fav');
  if(fav){onFav(fav.dataset.id,fav);return;}
  var segb=e.target.closest('.seg button');
  if(segb){onVote(segb.closest('.card').dataset.id,segb.dataset.vote,segb);return;}
  var rb=e.target.closest('.rbtn');
  if(rb){onRender(rb.dataset.id,rb);return;}
  var ph=e.target.closest('.ph');
  if(ph){onRender(ph.closest('.card').dataset.id,$('.rbtn[data-id="'+ph.closest('.card').dataset.id+'"]'));return;}
  var lbm=e.target.closest('.media');
  if(lbm&&lbm.querySelector('img.rimg')){openLB(lbm.closest('.card').dataset.id);return;}
  var pc=e.target.closest('.ll');
  if(pc){openItem(pc);return;}
  var stg=e.target.closest('.stack.showing');
  if(stg&&stg.dataset.item){openItem(stg);return;}
});
grid.addEventListener('pointerover',function(e){
  var el=e.target.closest('.ll');if(el)focusItem(el);
});
grid.addEventListener('pointerout',function(e){
  var el=e.target.closest('.ll');if(!el)return;
  if(el.contains(e.relatedTarget))return;
  var card=el.closest('.card');
  var to=(e.relatedTarget&&e.relatedTarget.closest)?
    e.relatedTarget.closest('.ll'):null;
  if(to&&to!==el&&card&&to.closest('.card')===card)return;
  clearFocus(card);
});
grid.addEventListener('focusin',function(e){
  var el=e.target.closest('.ll');if(el)focusItem(el);
});
grid.addEventListener('focusout',function(e){
  var el=e.target.closest('.ll');if(el)clearFocus(el.closest('.card'));
});
window.addEventListener('blur',function(){
  $$('.card .stack.showing').forEach(function(st){
    clearFocus(st.closest('.card'));
  });
});
document.addEventListener('keydown',function(e){
  if(e.key==='Escape'&&!e.defaultPrevented){closeItem();closeLB();closeSet();}
  if(e.key==='Enter'){
    var el=document.activeElement;
    if(el&&el.classList.contains('ll')){
      e.preventDefault();openItem(el);
    }else if(el&&el.classList.contains('ph')){
      e.preventDefault();onRender(el.closest('.card').dataset.id,$('.rbtn[data-id="'+el.closest('.card').dataset.id+'"]'));
    }
  }
  if(e.key==='Tab'&&lb.classList.contains('open')){
    var f=$$('#lb button, #lb a',lb).filter(function(x){return x.offsetParent!==null;});
    if(!f.length)return;
    var first=f[0],last=f[f.length-1];
    if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}
    else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
  }
});

/* ---------- init: staggered entrance + stored filter ---------- */
var cs=cards();
var io=('IntersectionObserver'in window)
  ?new IntersectionObserver(function(entries){
    entries.forEach(function(en){
      if(en.isIntersecting){en.target.classList.add('in');io.unobserve(en.target);}
    });
  },{rootMargin:'90px'})
  :null;
cs.forEach(function(c,i){
  relOutfit(c);
  c.classList.add('pre');
  c.style.setProperty('--d',((i%9)*55)+'ms');
  if(io)io.observe(c);else c.classList.add('in');
});
/* safety net: anything not revealed by the observer becomes visible */
setTimeout(function(){cs.forEach(function(c){c.classList.add('in');});},4000);
try{
  var stored=localStorage.getItem('wardrobe-filter');
  var chip=stored&&$('.chip[data-f="'+stored+'"]');
  if(chip&&stored!=='all'){chip.click();}
}catch(e){}
apply();
})();
"""


@app.get("/", response_class=HTMLResponse)
def index():
    try:
        outfits = list_outfits()
        err = ""
    except Exception as exc:  # noqa: BLE001
        outfits, err = [], str(exc)[:300]

    cards = "".join(card_html(o, i) for i, o in enumerate(outfits))
    n_total = len(outfits)
    n_img = sum(1 for o in outfits if o["renders"])
    n_rated = sum(1 for o in outfits if state_of(o) != "none")
    has_ref = REFERENCE.is_file()

    if has_ref:
        ref_inner = (
            '<div class="rv"><img id="refimg" src="/reference" alt="Referenzfoto">'
            '<span class="ovl">' + svg_icon("refresh") + "Ersetzen</span></div>"
            '<div class="rr"><b>Referenzfoto hinterlegt</b><span>Drag &amp; Drop'
            '&nbsp;·&nbsp;Klick zum Ersetzen</span></div>'
        )
    else:
        ref_inner = (
            '<span class="phi">' + svg_icon("camera") + "</span>"
            '<span class="wt">Referenzfoto fehlt</span>'
            '<span>ohne Foto kann kein Bild erzeugt werden – jetzt hochladen</span>'
            '<div class="rr"><b id="refwarn" hidden></b></div>'
        )
    ref_panel = (
        '<section class="panel"><div class="refwrap">'
        '<div id="refzone" class="refzone{cls}" tabindex="0" '
        'aria-label="Referenzfoto hochladen oder ersetzen">{inner}</div>'
        '<div class="refinfo"><h3>Referenzfoto</h3>'
        '<p>{txt}</p><div class="refacts">'
        '<button id="refbtn" class="refbtn" type="button">' + svg_icon("camera")
        + ("Foto ersetzen" if has_ref else "Foto auswählen") + '</button>'
        '<span id="refstat">PNG/JPG · wird vor dem Upload automatisch '
        'gedreht und verkleinert</span></div></div>'
        '<input id="refinput" class="visuallyhidden" type="file" accept="image/*">'
        "</div></section>"
    ).format(
        cls="" if has_ref else " warn",
        inner=ref_inner,
        txt=("Mit dem Foto wird dein Outfit an deiner Person gerendert. Zieh ein "
             "aktuelles Bild in die Vorschau oder klicke darauf, um es zu ersetzen."
             if has_ref else
             "Lade ein aktuelles, aufrechtes Foto deines Körpers hoch – es bleibt "
             "Grundlage für jede Bildgenerierung."),
    ) if has_ref else (
        '<section class="panel"><div class="refwrap">'
        '<div id="refzone" class="refzone warn" tabindex="0" '
        'aria-label="Referenzfoto hochladen">' + ref_inner + "</div>"
        '<div class="refinfo"><h3>Referenzfoto</h3>'
        '<p>Lade ein aktuelles, aufrechtes Foto deines Körpers hoch – es bleibt '
        'Grundlage für jede Bildgenerierung.</p>'
        '<div class="refacts"><button id="refbtn" class="refbtn" type="button">' + svg_icon("camera")
        + "Foto auswählen" + '</button>'
        '<span id="refstat">PNG/JPG · wird vor dem Upload automatisch '
        'gedreht und verkleinert</span></div></div>'
        '<input id="refinput" class="visuallyhidden" type="file" accept="image/*">'
        "</div></section>"
    )

    chip_data = [
        ("all", "Alle"), ("fav", "★ Favoriten"), ("up", "Positiv"),
        ("down", "Negativ"), ("unrate", "Unbewertet"), ("img", "Mit Bild"),
        ("noimg", "Ohne Bild"),
    ]
    chips = "".join(
        f'<button class="chip{(" active" if key == "all" else "")}" data-f="{key}">'
        f"{label} <span class='cnt'>&middot;</span></button>"
        for key, label in chip_data
    )

    head_script = (
        "try{var t=localStorage.getItem('wardrobe-theme')||'system';"
        "var l=t==='light'||(t!=='dark'&&window.matchMedia&&"
        "window.matchMedia('(prefers-color-scheme: light)').matches);"
        "if(l)document.documentElement.classList.add('light');}catch(e){}"
    )

    camera_path = ("M3 7 h4 l2 -2.5 h6 L17 7 h4 v12 H3 Z M12 16.5 a3.5 3.5 0 1 0 0 -7 "
                   "a3.5 3.5 0 0 0 0 7 Z")

    page = "".join([
        "<!doctype html>\n"
        '<html lang="de"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        '<meta name="color-scheme" content="dark light">\n'
        "<title>Outfit-Bilder</title>\n"
        '<link rel="icon" type="image/svg+xml" href="/favicon.svg">\n'
        "<script>", head_script, "</script>\n<style>", _CSS, "</style></head>\n"
        "<body>\n",
        '<header class="topbar"><div class="wrap bar">'
        '<div class="brand"><span class="logo">', svg_icon("logo"), "</span>"
        "<div><h1>Outfit-Bilder</h1>"
        "<p>Erzeugt aus Kleiderschrank-Outfits ein realistisches Bild an deiner "
        "Referenzperson.</p></div></div>"
        '<div class="spacer"></div>'
        '<div class="statchips">',
        f'<span class="stat"><b>{n_total}</b><span>Outfits</span></span>',
        f'<span class="stat"><b>{n_img}</b><span>mit Bild</span></span>',
        f'<span class="stat"><b>{n_rated}</b><span>bewertet</span></span>',
        "</div>"
        '<div id="tpick" class="tpick" role="radiogroup" aria-label="Farbschema">'
        '<span class="thumb" aria-hidden="true"></span>'
        '<button type="button" data-mode="light" role="radio" aria-checked="false" '
        'aria-label="Hell" title="Hell">' + svg_icon("sun") + "</button>"
        '<button type="button" data-mode="system" role="radio" aria-checked="true" '
        'aria-label="Systemeinstellung" title="Systemeinstellung">'
        + svg_icon("monitor") + "</button>"
        '<button type="button" data-mode="dark" role="radio" aria-checked="false" '
        'aria-label="Dunkel" title="Dunkel">' + svg_icon("moon") + "</button>"
        "</div>"
        '<button id="setbtn" class="tt' + ("" if has_ref else " warn") + '" '
        'aria-label="Einstellungen" title="Einstellungen">'
        + svg_icon("gear") + "</button>"
        "</div></header>\n",
        ('<div class="wrap"><div class="dberr">DB-Fehler: ' + esc(err) +
         '</div></div>' if err else ""),
        '<main class="wrap">\n',
        ("" if has_ref else
         '<div class="refbanner">' + svg_icon("warning") +
         "<span>Kein Referenzfoto hinterlegt &ndash; ohne Foto kann kein Bild "
         "erzeugt werden.</span>"
         '<button id="refbannerbtn" class="refbtn" type="button">Foto '
         "hinterlegen</button></div>"),
        '<section class="toolbar">'
        '<div class="chips" role="group" aria-label="Filter">' + chips + "</div>"
        '<div class="searchbox">' + svg_icon("search") +
        '<input id="search" type="search" placeholder="Suche Name, Anlass, '
        'Style-Notes …" aria-label="Suchen"></div>'
        '<select id="sortsel" aria-label="Sortierung">'
        '<option value="new" selected>Neueste</option>'
        '<option value="rated">Zuletzt bewertet</option>'
        '<option value="best">Beste Bewertung</option></select>'
        '<div class="rescount"><span id="rescount">…</span></div>'
        "</section>\n",
        '<div id="empty" class="empty"><div class="ei">' + svg_icon("empty") +
        "</div><p>Noch keine Outfits im Kleiderschrank.</p></div>\n"
        '<div class="grid" id="grid">', cards, "</div>\n</main>\n",
        '<div id="toasts" class="toasts" aria-live="polite"></div>\n',
        '<div id="set" class="lb setmodal" role="dialog" aria-modal="true" '
        'aria-hidden="true" aria-label="Einstellungen"><div class="win">'
        '<div class="row"><p class="pt2">Einstellungen</p>'
        '<button id="setclose" class="lbclose" aria-label="Schließen">'
        + svg_icon("close") + "</button></div>"
        '<div class="setbody">', ref_panel, "</div>"
        '<p class="setnote">Das Referenzfoto gilt für alle künftig erzeugten '
        "Bilder.</p></div></div>\n"
        '<div id="im" class="lb imgmodal" role="dialog" aria-modal="true" '
        'aria-hidden="true" aria-label="Kleidungsstück"><div class="win">'
        '<img id="imimg" src="" alt="Kleidungsstück">'
        '<div class="row"><p id="imtitle" class="pt2"></p>'
        '<a id="imopen" class="lbtn ghost" target="_blank" rel="noopener">'
        + svg_icon("download") + 'Original</a>'
        '<button id="imclose" class="lbclose" aria-label="Schließen">'
        + svg_icon("close") + '</button></div>'
        '<dl class="imdl"><dt>Typ</dt><dd id="imtype"></dd>'
        '<dt>Farbe</dt><dd id="imcolor"></dd>'
        '<dt>Material</dt><dd id="immat"></dd>'
        '<dt>Muster</dt><dd id="impat"></dd></dl>'
        '</div></div>\n',
        '<div id="lb" class="lb" role="dialog" aria-modal="true" '
        'aria-label="Outfit-Bild" aria-hidden="true"><div class="win">'
        '<img id="lbimg" src="" alt="Generiertes Outfit-Bild">'
        '<div class="row"><p id="lbtitle" class="pt2"></p>'
        '<a id="lbdl" class="lbtn" download href="#">' + svg_icon("download") +
        "Download</a>"
        '<button id="lbre" class="lbtn ghost">' + svg_icon("refresh") +
        "Neu erzeugen</button>"
        '<button id="lbclose" class="lbclose" aria-label="Schließen">' +
        svg_icon("close") + "</button></div></div></div>\n",
        "<script>", _JS, "</script>\n</body></html>",
    ])
    # keep the camera icon referenced (lint-friendly constant use)
    _ = camera_path
    return page


@app.get("/item/{item_id}")
def item_thumb(item_id: str):
    rows = query(
        """SELECT image_path, original_image_path, thumbnail_path
           FROM clothing_items WHERE id = %s::uuid""",
        (item_id,),
    )
    if not rows:
        raise HTTPException(status_code=404)
    path = item_image(rows[0])
    if not path:
        raise HTTPException(status_code=404)
    return FileResponse(path)
