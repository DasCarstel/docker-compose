"""Kleiner Shim zwischen wardrowbe und den KI-Endpunkten (Gemini + OpenCode Go).

Historischer Kern: wardrowbe fordert fuer den ersten Tagging-Pass `logprobs` +
`top_logprobs` an (um daraus einen Konfidenzwert zu berechnen). Die Gemini-
OpenAI-Endpoint unterstuetzt beides nicht und antwortet mit 400 Bad Request --
und weil wardrowbe `raise_for_status()` vor der Fehlerbehandlung aufruft,
greift sein eigener Rueckfall auf "ohne logprobs" nicht.

PRO-347 Erweiterung: Routing nach Modellnamen. Ist GO_API_KEY gesetzt und das
angefragte Modell in GO_MODELS, geht der Request an den OpenCode-Go-Endpunkt
(dort werden die Felder von der Console_aehnlichen Schicht selbst vertragen;
wir droppen sie der Einheitlichkeit halber trotzdem). Scheitert der Go-Endpunkt,
faehrt der Shim denselben Request mit GO_FALLBACK_MODEL gegen das Gemini-
Upstream -- mit der Originale-Autorisierung des Aufrufers (wardrobe-Gemini-Key).
Der Shim kennt keine Fachlogik und bleibt unabhaengig von Wardrowbe-Versionen.
"""

import json
import logging
import os

import httpx
from fastapi import FastAPI, Request, Response

UPSTREAM = os.getenv("UPSTREAM", "https://generativelanguage.googleapis.com")
GO_UPSTREAM = os.getenv("GO_UPSTREAM", "https://opencode.ai/inference/go")
GO_API_KEY = os.getenv("GO_API_KEY", "")
GO_MODELS = frozenset(
    m.strip() for m in os.getenv("GO_MODELS", "").split(",") if m.strip()
)
GO_FALLBACK_MODEL = os.getenv("GO_FALLBACK_MODEL", "gemini-flash-latest")
# Go policy: clients identify themselves and present a stable session header.
GO_USER_AGENT = os.getenv("GO_USER_AGENT", "my-coding-agent/1.0")
GO_SESSION_ID = os.getenv("GO_SESSION_ID", "wardrobe-shim-1")
# Gemini's OpenAI-compatible endpoint rejects these:
#   logprobs/top_logprobs -> 400 (wardrowbe asks for them to compute a confidence)
#   reasoning_effort      -> 400 whenever the value is "none" (wardrowbe's default)
DROP = {"logprobs", "top_logprobs", "reasoning_effort"}
# Path prefix wardrobe uses via AI_BASE_URL=http://wardrobe-shim:8080/v1beta/openai
GEMINI_PREFIX = "/v1beta/openai"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("shim")

app = FastAPI(title="wardrobe ai shim")


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "upstream": UPSTREAM,
        "go_upstream": GO_UPSTREAM,
        "go_models": sorted(GO_MODELS),
        "go_configured": bool(GO_API_KEY),
        "go_fallback_model": GO_FALLBACK_MODEL,
        "dropped": sorted(DROP),
    }


async def _strip(body: bytes) -> bytes:
    if not body:
        return body
    try:
        data = json.loads(body)
    except Exception:
        return body
    if not isinstance(data, dict):
        return body
    if not DROP & data.keys():
        return body
    for field in DROP:
        data.pop(field, None)
    return json.dumps(data).encode()


async def proxy(
    request: Request,
    upstream_base: str,
    path: str,
    headers: dict,
    body: bytes,
    fallback: dict | None = None,
) -> Response:
    """Forward to one upstream; optionally retry once against a second config."""
    async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
        try:
            upstream = await client.request(
                request.method,
                f"{upstream_base}{path}",
                content=body,
                headers=headers,
                params=dict(request.query_params),
            )
        except httpx.HTTPError as exc:
            upstream = None
            err = f"{type(exc).__name__}: {exc}"
            if fallback is not None:
                log.warning("go upstream unreachable (%s), falling back to gemini", err)
                return await proxy(
                    request,
                    fallback["base"],
                    fallback["path"],
                    fallback["headers"],
                    fallback["body"],
                    None,
                )
            return Response(content=f'{{"error":"upstream unreachable: {err}"}}', status_code=502, media_type="application/json")

    if upstream is not None and upstream.status_code >= 400 and fallback is not None:
        log.warning(
            "go upstream %s: %s -- falling back to gemini model=%s",
            upstream.status_code,
            upstream.text[:300],
            fallback["body_model"],
        )
        return await proxy(
            request,
            fallback["base"],
            fallback["path"],
            fallback["headers"],
            fallback["body"],
            None,
        )

    if upstream is not None and upstream.status_code >= 400:
        log.warning("upstream %s: %s", upstream.status_code, upstream.text[:600])

    return Response(
        content=upstream.content if upstream is not None else b'{"error":"no upstream response"}',
        status_code=upstream.status_code if upstream is not None else 502,
        media_type=upstream.headers.get("content-type") if upstream is not None else "application/json",
    )


@app.api_route("/{path:path}", methods=["GET", "POST"])
async def route(path: str, request: Request) -> Response:
    body = await _strip(await request.body())

    caller_auth = request.headers.get("authorization") or ""
    headers = {}
    if caller_auth:
        headers["authorization"] = caller_auth
    if ctype := request.headers.get("content-type"):
        headers["content-type"] = ctype

    gemini_path = "/" + path

    # ---- Go routing --------------------------------------------------------
    if GO_API_KEY and GO_MODELS and body:
        model = None
        try:
            model = (json.loads(body) or {}).get("model")
        except Exception:
            model = None
        if model in GO_MODELS:
            if gemini_path.startswith(GEMINI_PREFIX + "/"):
                go_path = "/openai/v1" + gemini_path[len(GEMINI_PREFIX):]
            elif gemini_path.startswith("/v1beta/openai"):
                go_path = "/openai/v1" + gemini_path[len("/v1beta/openai"):]
            else:
                go_path = gemini_path
            go_headers = dict(headers)
            go_headers["authorization"] = f"Bearer {GO_API_KEY}"
            go_headers["user-agent"] = GO_USER_AGENT
            go_headers["x-opencode-session"] = GO_SESSION_ID
            log.info("routing model=%s -> go %s%s", model, GO_UPSTREAM, go_path)

            # Fallback body: same payload, model swapped to the Gemini model.
            fb_body = body
            try:
                data = json.loads(body)
                data["model"] = GO_FALLBACK_MODEL
                fb_body = json.dumps(data).encode()
            except Exception:
                pass
            fb_headers = {}
            if caller_auth:
                fb_headers["authorization"] = caller_auth
            fb_headers["content-type"] = request.headers.get("content-type", "application/json")

            return await proxy(
                request,
                GO_UPSTREAM,
                go_path,
                go_headers,
                body,
                fallback={
                    "base": UPSTREAM,
                    "path": gemini_path,
                    "headers": fb_headers,
                    "body": fb_body,
                    "body_model": GO_FALLBACK_MODEL,
                },
            )

    return await proxy(
        request,
        UPSTREAM,
        gemini_path,
        headers,
        body,
        None,
    )
