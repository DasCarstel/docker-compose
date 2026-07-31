import os
import threading

import requests
from flask import Flask, Response, jsonify

app = Flask(__name__)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:9b-128k")
RELOAD_DELAY = int(os.environ.get("RELOAD_DELAY", "1800"))

_lock = threading.Lock()
_timer = None
_unloaded = False

print(f"[init] OLLAMA_URL={OLLAMA_URL} MODEL={OLLAMA_MODEL} RELOAD_DELAY={RELOAD_DELAY}s")


def _unload_model():
    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate", json={
            "model": OLLAMA_MODEL,
            "prompt": ".",
            "keep_alive": 0,
        }, timeout=10)
        print(f"[unload] status={r.status_code}")
        return r.status_code == 200
    except Exception as e:
        print(f"[unload] error: {e}")
        return False


def _reload_model():
    global _unloaded
    print("[reload] loading model back into VRAM...")
    try:
        r = requests.post(f"{OLLAMA_URL}/api/generate", json={
            "model": OLLAMA_MODEL,
            "prompt": "ping",
            "max_tokens": 1,
            "keep_alive": -1,
        }, timeout=120)
        print(f"[reload] status={r.status_code}")
    except Exception as e:
        print(f"[reload] error: {e}")
    finally:
        with _lock:
            _unloaded = False
        print("[reload] model reloaded, keep_alive=-1")


def _reset_timer():
    global _timer
    if _timer:
        _timer.cancel()
    _timer = threading.Timer(RELOAD_DELAY, _reload_model)
    _timer.daemon = True
    _timer.start()
    remaining_min = RELOAD_DELAY // 60
    print(f"[timer] reset, reload in {remaining_min} min")


@app.route("/check")
def check():
    global _unloaded
    with _lock:
        try:
            ps = requests.get(f"{OLLAMA_URL}/api/ps", timeout=5).json()
        except Exception as e:
            print(f"[check] /api/ps error: {e}")
            return Response("OK", status=200)

        if ps.get("models"):
            print("[check] model loaded, unloading...")
            if _unload_model():
                _unloaded = True
                _reset_timer()
            else:
                print("[check] unload failed")
        else:
            print("[check] no model loaded")
            _unloaded = True
            _reset_timer()

    return Response("OK", status=200)


@app.route("/status")
def status():
    with _lock:
        return jsonify({
            "unloaded": _unloaded,
            "reload_delay_s": RELOAD_DELAY,
        })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
