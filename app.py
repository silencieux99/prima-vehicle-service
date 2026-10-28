"""API Flask — lookup targa italienne via Prima.it uniquement."""

import os
import time

from flask import Flask, jsonify, request
from flask_cors import CORS

from prima_scraper import search_by_plate as prima_search, warmup_session as prima_warmup

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
    return jsonify({"status": "ok", "service": "prima-vehicle-api", "source": "prima-playwright"}), 200


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

    result = lookup_prima(plate, birth_date or "")
    if result:
        return jsonify(result), 200
    return jsonify({"error": "no results found", "plate": plate}), 404


# Warmup au démarrage
try:
    print("[Prima Service] Warming up Playwright session...")
    prima_warmup()
    print("[Prima Service] Warmup complete")
except Exception as e:
    print(f"[Prima Service] Warmup failed (will retry on first request): {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
