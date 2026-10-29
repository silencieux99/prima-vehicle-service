"""API Flask — lookup targa italienne via Prima.it (+ Allianz optionnel)."""

from __future__ import annotations

import os
import threading
import time

from flask import Flask, jsonify, request
from flask_cors import CORS

from prima_scraper import search_by_plate as prima_search, warmup_session as prima_warmup
from allianz_scraper import search_by_plate as allianz_search, warmup_session as allianz_warmup

app = Flask(__name__)
CORS(
    app,
    resources={
        r"/api/*": {
            "origins": [
                "https://targastorico.it",
                "https://www.targastorico.it",
                "https://targarapporto.it",
                "https://www.targarapporto.it",
                "https://rapportoauto.it",
                "https://www.rapportoauto.it",
                "https://storico-auto.it",
                "https://www.storico-auto.it",
                "https://verificaauto.it",
                "https://www.verificaauto.it",
                "https://*.vercel.app",
                "http://localhost:3000",
                "http://localhost:3002",
            ]
        }
    },
)

_LOOKUP_CACHE: dict[str, tuple[float, dict]] = {}
CACHE_TTL_SECONDS = int(os.environ.get("LOOKUP_CACHE_TTL_SECONDS", "3600"))
_ALLIANZ_CACHE: dict[str, tuple[float, dict]] = {}
ALLIANZ_CACHE_TTL = int(os.environ.get("LOOKUP_CACHE_TTL_SECONDS", "3600"))

_WARMED = {"prima": False, "allianz": False}
_WARM_LOCK = threading.Lock()


def _normalize_plate(plate: str) -> str:
    return plate.upper().replace(" ", "").replace("-", "")


def ensure_warm(which: str, background: bool = False) -> None:
    """Warm Playwright. En background pour ne jamais bloquer /health."""

    def _do() -> None:
        with _WARM_LOCK:
            if _WARMED.get(which):
                return
            label = "Prima" if which == "prima" else "Allianz"
            try:
                print(f"[Service] Warming {label}...")
                (prima_warmup if which == "prima" else allianz_warmup)()
                _WARMED[which] = True
                print(f"[Service] {label} warm OK")
            except Exception as exc:
                print(f"[Service] {label} warm failed: {exc}")

    if background:
        threading.Thread(target=_do, daemon=True, name=f"warm-{which}").start()
        return
    _do()


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
    # Jamais de Playwright ici — sinon gunicorn se sature et la sonde Railway tombe.
    ensure_warm("prima", background=True)
    return (
        jsonify(
            {
                "status": "ok",
                "service": "prima-vehicle-api",
                "sources": ["prima-playwright"],
                "version": "prima-resilient-2026-10-29",
                "warmed": dict(_WARMED),
            }
        ),
        200,
    )


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
    ensure_warm("prima")

    t0 = time.time()
    try:
        result = lookup_prima(plate, birth_date or "")
    except TimeoutError as exc:
        elapsed = int((time.time() - t0) * 1000)
        print(f"[Prima Service] TIMEOUT {plate} ({elapsed}ms): {exc}")
        return (
            jsonify(
                {
                    "success": False,
                    "found": False,
                    "error": "timeout",
                    "plate": plate,
                    "elapsed_ms": elapsed,
                }
            ),
            504,
        )
    except Exception as exc:
        elapsed = int((time.time() - t0) * 1000)
        print(f"[Prima Service] ERROR {plate}: {exc}")
        return (
            jsonify(
                {
                    "success": False,
                    "found": False,
                    "error": str(exc),
                    "plate": plate,
                    "elapsed_ms": elapsed,
                }
            ),
            502,
        )

    elapsed = int((time.time() - t0) * 1000)
    if result:
        return jsonify({**result, "elapsed_ms": elapsed}), 200
    return (
        jsonify(
            {
                "success": True,
                "found": False,
                "error": "no results found",
                "plate": plate,
                "elapsed_ms": elapsed,
            }
        ),
        404,
    )


@app.route("/api/allianz", methods=["GET", "POST"])
def lookup_allianz_endpoint():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        plate = data.get("plate") or data.get("targa")
        birth_date = (
            data.get("birth_date") or data.get("birthDate") or data.get("data_nascita")
        )
    else:
        plate = request.args.get("plate") or request.args.get("targa")
        birth_date = (
            request.args.get("birth_date")
            or request.args.get("birthDate")
            or request.args.get("data_nascita")
        )

    if not plate:
        return jsonify({"error": "plate parameter required"}), 400

    plate = _normalize_plate(plate)
    now = time.time()
    hit = _ALLIANZ_CACHE.get(plate)
    if hit and hit[0] > now:
        print(f"[Allianz] cache hit {plate}")
        return jsonify(
            {"success": True, "found": True, "data": hit[1], "source": "allianz", "elapsed_ms": 0}
        ), 200

    t0 = time.time()
    print(f"[Allianz] Lookup {plate}")
    ensure_warm("allianz")
    try:
        result = allianz_search(plate, birth_date or "")
    except TimeoutError:
        elapsed = int((time.time() - t0) * 1000)
        return jsonify(
            {
                "success": False,
                "found": False,
                "error": "timeout",
                "plate": plate,
                "source": "allianz",
                "elapsed_ms": elapsed,
            }
        ), 504
    except Exception as exc:
        elapsed = int((time.time() - t0) * 1000)
        return jsonify(
            {
                "success": False,
                "found": False,
                "error": str(exc),
                "plate": plate,
                "source": "allianz",
                "elapsed_ms": elapsed,
            }
        ), 502

    elapsed = int((time.time() - t0) * 1000)
    if result:
        _ALLIANZ_CACHE[plate] = (now + ALLIANZ_CACHE_TTL, result)
        return jsonify(
            {
                "success": True,
                "found": True,
                "data": result,
                "source": "allianz",
                "elapsed_ms": elapsed,
            }
        ), 200
    return jsonify(
        {
            "success": True,
            "found": False,
            "data": None,
            "plate": plate,
            "source": "allianz",
            "elapsed_ms": elapsed,
        }
    ), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
