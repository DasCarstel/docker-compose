"""Minimale STT-Weboberflaeche fuer qwen3-asr (vLLM).

Nur Python-Stdlib, keine Abhaengigkeiten, kein Modell im Container.

Drei Wege hinein:
  GET  /              Web-UI (hinter Authentik)
  POST /api/transcribe  UI-Backend, roher WAV-Body, antwortet JSON
  POST /text            Kompatibilitaets-Endpunkt fuer Apps, die den nackten
                        Text erwarten (Bearer-Key). vLLM liefert naemlich auch
                        bei response_format=text immer JSON - dieser Endpunkt
                        packt das Feld "text" als text/plain aus.
"""

import json
import os
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("STT_UPSTREAM", "http://qwen3-asr:8000/v1/audio/transcriptions")
MODEL = os.environ.get("STT_MODEL", "qwen3-asr")
API_KEY = os.environ.get("STT_API_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))
MAX_BYTES = 100 * 1024 * 1024
BOUNDARY = "----sttui7d1f4a"

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "index.html"), "rb") as fh:
    INDEX = fh.read()


def build_multipart(audio, language=""):
    """Baut den Request-Body fuer /v1/audio/transcriptions."""
    chunks = [
        ("--%s\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n%s\r\n"
         % (BOUNDARY, MODEL)).encode(),
    ]
    if language:
        chunks.append(
            ("--%s\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n%s\r\n"
             % (BOUNDARY, language)).encode())
    chunks.append(
        ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n"
         "Content-Type: audio/wav\r\n\r\n" % BOUNDARY).encode())
    chunks.append(audio)
    chunks.append(("\r\n--%s--\r\n" % BOUNDARY).encode())
    return b"".join(chunks), "multipart/form-data; boundary=" + BOUNDARY


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "stt-ui"

    # ---------- Hilfen ----------
    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._send(411, b'{"error":"Content-Length fehlt"}')
            return None
        if length > MAX_BYTES:
            self._send(413, b'{"error":"Datei zu gross (max 100 MB)"}')
            return None
        return self.rfile.read(length)

    def _upstream(self, payload, ctype):
        req = urllib.request.Request(
            UPSTREAM, data=payload, method="POST",
            headers={
                "Authorization": "Bearer " + API_KEY,
                "Content-Type": ctype,
                "Content-Length": str(len(payload)),
            })
        return urllib.request.urlopen(req, timeout=600)

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX, "text/html; charset=utf-8")
        elif path == "/healthz":
            self._send(200, b'{"ok":true}')
        else:
            self._send(404, b'{"error":"not found"}')

    # ---------- POST ----------
    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/text":
            return self.handle_text()
        if path.startswith("/api/transcribe"):
            return self.handle_ui()
        return self._send(404, b'{"error":"not found"}')

    def handle_text(self):
        """Nackter Text fuer Apps ohne JSON-Parser."""
        if not API_KEY or self.headers.get("Authorization", "") != "Bearer " + API_KEY:
            return self._send(401, b"unauthorized", "text/plain; charset=utf-8")
        audio = self._read_body()
        if audio is None:
            return

        ctype = self.headers.get("Content-Type") or ""
        if ctype.startswith("multipart/form-data"):
            payload, out_type = audio, ctype       # App spricht schon OpenAI-Format
        else:
            payload, out_type = build_multipart(audio)

        try:
            with self._upstream(payload, out_type) as resp:
                data = json.loads(resp.read() or b"{}")
                text = (data.get("text") or "").strip()
                return self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b""
            try:
                raw = (json.loads(raw).get("error") or {}).get("message", raw).encode()
            except Exception:
                pass
            return self._send(exc.code, raw, "text/plain; charset=utf-8")
        except Exception as exc:  # noqa: BLE001
            return self._send(502, str(exc).encode(), "text/plain; charset=utf-8")

    def handle_ui(self):
        """UI-Backend: roher WAV-Body rein, JSON von vLLM raus."""
        audio = self._read_body()
        if audio is None:
            return

        language = ""
        if "lang=" in self.path:
            language = self.path.split("lang=", 1)[1].split("&", 1)[0][:5]
            if language not in ("de", "en"):
                language = ""

        payload, ctype = build_multipart(audio, language)
        try:
            with self._upstream(payload, ctype) as resp:
                return self._send(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            return self._send(exc.code, exc.read() or b'{"error":"upstream"}')
        except Exception as exc:  # noqa: BLE001
            return self._send(502, json.dumps({"error": str(exc)}).encode())

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


if __name__ == "__main__":
    print("stt-ui auf :%d -> %s (Modell %s)" % (PORT, UPSTREAM, MODEL), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
