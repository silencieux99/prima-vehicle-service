"""API Flask — lookup targa italienne via Prima.it uniquement."""

import os
import time

from flask import Flask, jsonify, request
from flask_cors import CORS

from prima_scraper import search_by_plate as prima_search, warmup_session as prima_warmup
from allianz_scraper import search_by_plate as allianz_search, warmup_session as allianz_warmup

app = Flask(__name__)
CORS(app, origins=[
    "http://localhost:3000",
    "https://targastorico.it",
    "https://www.targastorico.it",
])

# Cache résultats
_LOOKUP_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = int(os.environ.get("LOOKUP_CACHE_TTL_SECONDS", "3600"))


def _normalize_plate(plate: str) -> str:
    return plate.upper().replace(" ", "").replace("-", "")


def lookup_prima(plate: str, birth_date: str):
    key = _normalize_plate(plate)
    now = time.time()
    hit = _LOOKUP_CACHE.get(key)
    if hit and hit[0] > now:
        print(f"[Prima Service] cache hit {key}")
        return hit[1]

    result = prima_search(plate, birth_date or "")
    if result:
        _LOOKUP_CACHE[key] = (now + CACHE_TTL_SECONDS, result)
        print(f"[Prima Service] OK {key}")
    return result


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "prima-vehicle-api", "sources": ["prima-playwright", "allianz-playwright"], "version": "allianz-fast-20261028192256"}), 200


@app.route("/api/prima", methods=["GET", "POST"])
def lookup_prima_endpoint():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        plate = data.get("plate") or data.get("targa")
        birth_date = data.get("birth_date") or data.get("data_nascita")
    else:
        plate = request.args.get("plate") or request.args.get("targa")
        birth_date = request.args.get("birth_date") or request.args.get("data_nascita")

    if not plate:
        return jsonify({"error": "plate parameter required"}), 400

    plate = _normalize_plate(plate)
    print(f"[Prima Service] Lookup {plate}")
    ensure_warm()

    result = lookup_prima(plate, birth_date or "")
    if result:
        return jsonify(result), 200
    return jsonify({"error": "no results found", "plate": plate}), 404




# Cache Allianz
_ALLIANZ_CACHE: dict[str, tuple[float, dict]] = {}
ALLIANZ_CACHE_TTL = int(os.environ.get("LOOKUP_CACHE_TTL_SECONDS", "3600"))


@app.route("/api/allianz", methods=["GET", "POST"])
def lookup_allianz_endpoint():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        plate = data.get("plate") or data.get("targa")
        birth_date = data.get("birth_date") or data.get("birthDate") or data.get("data_nascita")
    else:
        plate = request.args.get("plate") or request.args.get("targa")
        birth_date = request.args.get("birth_date") or request.args.get("birthDate") or request.args.get("data_nascita")

    if not plate:
        return jsonify({"error": "plate parameter required"}), 400

    plate = _normalize_plate(plate)
    now = time.time()
    hit = _ALLIANZ_CACHE.get(plate)
    if hit and hit[0] > now:
        print(f"[Allianz] cache hit {plate}")
        return jsonify({"success": True, "found": True, "data": hit[1], "source": "allianz", "elapsed_ms": 0}), 200

    t0 = time.time()
    print(f"[Allianz] Lookup {plate}")
    ensure_warm()
    result = allianz_search(plate, birth_date or "")
    elapsed = int((time.time() - t0) * 1000)
    if result:
        _ALLIANZ_CACHE[plate] = (now + ALLIANZ_CACHE_TTL, result)
        return jsonify({"success": True, "found": True, "data": result, "source": "allianz", "elapsed_ms": elapsed}), 200
    return jsonify({"success": True, "found": False, "data": None, "plate": plate, "source": "allianz", "elapsed_ms": elapsed}), 404


# Lazy sync warmup on first request — Playwright sync API is thread-bound.
_WARMED = False


def ensure_warm() -> None:
    global _WARMED
    if _WARMED:
        return
    try:
        print("[Service] Warming Prima...")
        prima_warmup()
        print("[Service] Prima warm OK")
    except Exception as e:
        print(f"[Service] Prima warm failed: {e}")
    try:
        print("[Service] Warming Allianz...")
        allianz_warmup()
        print("[Service] Allianz warm OK")
    except Exception as e:
        print(f"[Service] Allianz warm failed: {e}")
    _WARMED = True


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
