"""Minimale STT-Weboberflaeche fuer qwen3-asr (vLLM).

Nur Python-Stdlib, keine Abhaengigkeiten, kein Modell im Container.
Der Browser schickt immer 16-kHz-Mono-WAV; dieser Server verpackt das in
ein multipart/form-data und spricht damit /v1/audio/transcriptions von
qwen3-asr an. Der API-Key bleibt serverseitig - er taucht nie im Browser auf.

Laeuft hinter Traefik mit Authentik (UI-Route), waehrend /v1 direkt und
ohne SSO an vLLM geht (Bearer-Key).
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

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "index.html"), "rb") as fh:
    INDEX = fh.read()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "stt-ui"

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

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, INDEX, "text/html; charset=utf-8")
        elif path == "/healthz":
            self._send(200, b'{"ok":true}')
        else:
            self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        if not self.path.startswith("/api/transcribe"):
            return self._send(404, b'{"error":"not found"}')

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._send(411, b'{"error":"Content-Length fehlt"}')
        if length > MAX_BYTES:
            return self._send(413, b'{"error":"Datei zu gross (max 100 MB)"}')

        audio = self.rfile.read(length)

        language = ""
        if "lang=" in self.path:
            language = self.path.split("lang=", 1)[1].split("&", 1)[0][:5]
            if language not in ("de", "en"):
                language = ""

        boundary = "----sttui7d1f4a"
        chunks = [
            ("--%s\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n%s\r\n"
             % (boundary, MODEL)).encode(),
        ]
        if language:
            chunks.append(
                ("--%s\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n%s\r\n"
                 % (boundary, language)).encode())
        chunks.append(
            ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"audio.wav\"\r\n"
             "Content-Type: audio/wav\r\n\r\n" % boundary).encode())
        chunks.append(audio)
        chunks.append(("\r\n--%s--\r\n" % boundary).encode())
        body = b"".join(chunks)

        req = urllib.request.Request(
            UPSTREAM, data=body, method="POST",
            headers={
                "Authorization": "Bearer " + API_KEY,
                "Content-Type": "multipart/form-data; boundary=" + boundary,
                "Content-Length": str(len(body)),
            })
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return self._send(resp.status, resp.read())
        except urllib.error.HTTPError as exc:
            payload = exc.read() or b'{"error":"upstream"}'
            return self._send(exc.code, payload)
        except Exception as exc:  # noqa: BLE001 - an den Browser durchreichen
            return self._send(502, json.dumps({"error": str(exc)}).encode())

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


if __name__ == "__main__":
    print("stt-ui auf :%d -> %s (Modell %s)" % (PORT, UPSTREAM, MODEL), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
