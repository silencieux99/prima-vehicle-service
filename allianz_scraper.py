"""
Lookup targa italiana via Allianz Direct — session Playwright persistante.

Optimisé pour <15s par lookup (session chaude) :
- Page devis réutilisée (pas de reload complet à chaque targa)
- Fast path API via context.request + token WAF
- UI fallback avec wait_for_response (pas de sleep 25s)
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
from typing import Any, Optional
from urllib.parse import urlencode

import requests
from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

ENTRY_URL = "https://www.allianzdirect.it/"
QUOTE_URL = (
    "https://www.allianzdirect.it/assicurazione-auto/calcola-preventivo/#/vehicleSearch"
)
API_BASE = "https://pro-edp.apis.allianz.com/prod/sales-service/product-route"
PRODUCT_ROUTE_PREFIX = (
    "https://pro-edp.apis.allianz.com/prod/sales-service/product-route"
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SESSION_TTL_SECONDS = int(os.environ.get("ALLIANZ_SESSION_TTL_SECONDS", "1500"))
API_TIMEOUT_MS = int(os.environ.get("ALLIANZ_API_TIMEOUT_MS", "5000"))
UI_RESPONSE_TIMEOUT_MS = int(os.environ.get("ALLIANZ_UI_TIMEOUT_MS", "8000"))
# Budget total — API-first vise <3s ; UI fallback <12s
LOOKUP_DEADLINE_SECONDS = float(os.environ.get("ALLIANZ_LOOKUP_DEADLINE_SECONDS", "12"))
TOKEN_REFRESH_INTERVAL_SECONDS = float(os.environ.get("ALLIANZ_TOKEN_REFRESH_SECONDS", "240"))


def _normalize_plate(plate: str) -> str:
    return plate.upper().replace(" ", "").replace("-", "")


def _normalize_birth_date_ui(birth_date: str) -> str:
    value = (birth_date or "").strip()
    if not value:
        return "01/01/1990"
    if re.match(r"^\d{2}/\d{2}/\d{4}$", value):
        return value
    match = re.match(r"^(\d{2})[-/](\d{2})[-/](\d{4})$", value)
    if match:
        return f"{match.group(1)}/{match.group(2)}/{match.group(3)}"
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", value)
    if match:
        return f"{match.group(3)}/{match.group(2)}/{match.group(1)}"
    return "01/01/1990"


def _birth_date_api(birth_date: str) -> str:
    ui = _normalize_birth_date_ui(birth_date)
    match = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", ui)
    if match:
        return f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
    return "1990-01-01"


def _is_product_route_url(url: str) -> bool:
    return url.startswith(PRODUCT_ROUTE_PREFIX)


def _year_from_date(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    match = re.search(r"(19|20)\d{2}", value)
    return match.group(0) if match else None


def _map_fuel(code: Any) -> Optional[str]:
    if not isinstance(code, str):
        return None
    mapping = {
        "B": "Benzina",
        "D": "Diesel",
        "G": "GPL",
        "M": "Metano",
        "E": "Elettrico",
        "H": "Ibrido",
        "L": "Benzina/GPL",
    }
    return mapping.get(code.upper(), code)


def map_allianz_response(plate: str, data: dict) -> Optional[dict]:
    car = data.get("carDetails") or {}
    if not car.get("brand") and not car.get("model"):
        return None

    marque = (car.get("brand") or "").strip() or None
    modele = (car.get("model") or "").strip() or None
    version = (car.get("modelDetail") or "").strip() or None
    first_reg = car.get("firstRegistrationDate")
    build_year = car.get("buildYear")
    annee = _year_from_date(first_reg) or (str(build_year) if build_year else None)

    price = car.get("price") or {}
    prix_estime = None
    if price.get("amount") is not None:
        currency = price.get("currency") or "EUR"
        prix_estime = f"{price.get('amount')} {currency}".strip()

    puissance_kw = str(car.get("power")) if car.get("power") is not None else None
    puissance_cv = str(car.get("horsePower")) if car.get("horsePower") is not None else None
    puissance = None
    if puissance_cv and puissance_kw:
        puissance = f"{puissance_cv} CV ({puissance_kw} kW)"
    elif puissance_cv:
        puissance = f"{puissance_cv} CV"
    elif puissance_kw:
        puissance = f"{puissance_kw} kW"

    nom = " ".join(x for x in [marque, modele, version] if x) or None

    return {
        "plate": _normalize_plate(plate),
        "marque": marque,
        "modele": modele,
        "version": version,
        "nom_complet": nom,
        "annee": annee,
        "date_mec": first_reg,
        "energie": _map_fuel(car.get("fuelType")),
        "cylindree": str(car.get("cubicCapacity")) if car.get("cubicCapacity") else None,
        "puissance_kw": puissance_kw,
        "puissance_cv": puissance_cv,
        "puissance": puissance,
        "prix_estime": prix_estime,
        "vin": car.get("vehicleIdentificationNumber"),
        "source": "allianz",
        "action": data.get("action"),
        "raw": data,
    }


class AllianzBrowserSession:
    """Singleton Chromium — session WAF + page devis réutilisée."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._quote_page: Optional[Page] = None
        self._session_started_at = 0.0
        self._waf_token: Optional[str] = None
        self._token_refreshed_at = 0.0
        self._cookies_accepted = False
        self._api_fail_streak = 0
        self._owner_thread_id: Optional[int] = None

    def _session_expired(self) -> bool:
        if not self._context:
            return True
        return (time.time() - self._session_started_at) >= SESSION_TTL_SECONDS

    def _close(self) -> None:
        if self._quote_page:
            try:
                self._quote_page.close()
            except Exception:
                pass
            self._quote_page = None
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        self._waf_token = None
        self._token_refreshed_at = 0.0
        self._cookies_accepted = False
        self._api_fail_streak = 0

    def _read_waf_token_from_page(self, page: Page) -> None:
        try:
            token = page.evaluate(
                """async () => {
                    try {
                        if (window.AwsWafIntegration?.getToken) {
                            return await window.AwsWafIntegration.getToken();
                        }
                        if (window.gAwsWafIntegration?.getToken) {
                            return await window.gAwsWafIntegration.getToken();
                        }
                    } catch (e) {
                        return null;
                    }
                    return null;
                }"""
            )
            if isinstance(token, str) and token.strip():
                self._waf_token = token.strip()
                self._token_refreshed_at = time.time()
                self._api_fail_streak = 0
        except Exception as exc:
            print(f"[Allianz IT] getToken JS: {exc}")

    def _read_waf_token_from_cookies(self) -> None:
        if not self._context:
            return
        for cookie in self._context.cookies():
            name = cookie.get("name", "").lower()
            if "waf" in name or name == "aws-waf-token":
                value = cookie.get("value")
                if value:
                    self._waf_token = value
                    self._token_refreshed_at = time.time()
                    return

    def _refresh_waf_token(self, page: Optional[Page] = None) -> None:
        target = page or self._quote_page
        if target:
            self._read_waf_token_from_page(target)
        self._read_waf_token_from_cookies()

    def _wait_for_waf_token(self, page: Page, timeout_ms: int = 8000) -> None:
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            self._read_waf_token_from_page(page)
            if self._waf_token:
                return
            self._read_waf_token_from_cookies()
            if self._waf_token:
                return
            page.wait_for_timeout(400)

    def _token_stale(self) -> bool:
        if not self._waf_token:
            return True
        return (time.time() - self._token_refreshed_at) >= TOKEN_REFRESH_INTERVAL_SECONDS

    def _ensure_fresh_token(self) -> None:
        """Rafraîchit le token WAF sans remplir le formulaire (léger)."""
        if not self._token_stale() and self._api_fail_streak < 2:
            return
        page = self._quote_page
        if not page or page.is_closed():
            self._bootstrap()
            return
        try:
            page.goto(QUOTE_URL, wait_until="domcontentloaded", timeout=15000)
            self._accept_cookies(page)
            self._wait_for_waf_token(page, timeout_ms=5000)
            print(f"[Allianz IT] token refresh={'ok' if self._waf_token else 'fail'}")
        except Exception as exc:
            print(f"[Allianz IT] token refresh error: {exc}")
            self._bootstrap()

    def _accept_cookies(self, page: Page) -> None:
        if self._cookies_accepted:
            return
        for selector in (
            "button:has-text('Accetta tutti i cookies')",
            "#onetrust-accept-btn-handler",
            "button:has-text('Accetta')",
        ):
            try:
                btn = page.locator(selector).first
                if btn.is_visible(timeout=800):
                    btn.click(timeout=2000)
                    page.wait_for_timeout(400)
                    self._cookies_accepted = True
                    return
            except Exception:
                pass

    def _block_heavy_assets(self) -> None:
        if not self._context:
            return

        def handler(route) -> None:
            if route.request.resource_type in {"image", "media", "font"}:
                route.abort()
            else:
                route.continue_()

        try:
            self._context.route("**/*", handler)
        except Exception:
            pass

    def _drop_thread_greenlets(self) -> None:
        """Detache les references Playwright dont les greenlets appartiennent
        a un thread deja termine (les appeler depuis un autre thread leve
        greenlet.error)."""
        self._quote_page = None
        self._context = None
        self._browser = None
        self._playwright = None

    def _bootstrap(self) -> None:
        tid = threading.get_ident()
        if self._owner_thread_id is not None and self._owner_thread_id != tid:
            print("[Allianz IT] Rebind session to current thread")
            self._drop_thread_greenlets()

        print("[Allianz IT] Bootstrap session Playwright…")
        if not self._playwright:
            self._playwright = sync_playwright().start()
        else:
            self._close()
        self._owner_thread_id = tid

        self._browser = self._playwright.chromium.launch(
            headless=os.environ.get("ALLIANZ_HEADLESS", "false").lower() == "true",
            ignore_default_args=["--enable-automation"],
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        self._context = self._browser.new_context(
            locale="it-IT",
            timezone_id="Europe/Rome",
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 720},
        )

        page = self._context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        def on_request(request) -> None:
            if "pro-edp.apis.allianz.com" not in request.url:
                return
            token = request.headers.get("x-aws-waf-token") or request.headers.get(
                "X-Aws-Waf-Token"
            )
            if token:
                self._waf_token = token

        page.on("request", on_request)

        page.goto(QUOTE_URL, wait_until="domcontentloaded", timeout=45000)
        self._accept_cookies(page)
        self._refresh_waf_token(page)

        self._quote_page = page
        self._session_started_at = time.time()
        print(f"[Allianz IT] Session prête (token={'oui' if self._waf_token else 'non'})")

    def _attach_request_listener(self, page: Page) -> None:
        def on_request(request) -> None:
            if "pro-edp.apis.allianz.com" not in request.url:
                return
            token = request.headers.get("x-aws-waf-token") or request.headers.get(
                "X-Aws-Waf-Token"
            )
            if token:
                self._waf_token = token

        page.on("request", on_request)

    def _open_quote_page(self) -> Page:
        """Nouvelle page devis — évite l'état SPA corrompu entre lookups."""
        assert self._context is not None
        if self._quote_page and not self._quote_page.is_closed():
            try:
                self._quote_page.close()
            except Exception:
                pass
            self._quote_page = None

        page = self._context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        self._attach_request_listener(page)
        page.goto(QUOTE_URL, wait_until="domcontentloaded", timeout=30000)
        self._accept_cookies(page)
        self._wait_form_ready(page)
        self._refresh_waf_token(page)
        self._quote_page = page
        return page

    def _ensure_session(self, force_refresh: bool = False) -> None:
        if force_refresh or self._session_expired():
            self._bootstrap()
        elif not self._quote_page or self._quote_page.is_closed():
            self._bootstrap()
    def _api_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Backend-Tenant": "IT",
            "Line-Of-Business": "motor",
            "Mapped-Lang": "it-IT",
            "Origin": "https://www.allianzdirect.it",
            "Referer": "https://www.allianzdirect.it/",
            "User-Agent": USER_AGENT,
        }
        if self._waf_token:
            headers["X-Aws-Waf-Token"] = self._waf_token
        return headers

    def _product_route_url(self, clean_plate: str, birth_date: str) -> str:
        params = {
            "licensePlate": clean_plate,
            "birthDate": _birth_date_api(birth_date),
        }
        return f"{API_BASE}?{urlencode(params)}"

    def _lookup_with_env_token(self, clean_plate: str, birth_date: str) -> Optional[dict]:
        token = os.environ.get("ALLIANZ_WAF_TOKEN", "").strip()
        if not token:
            return None
        try:
            resp = requests.get(
                self._product_route_url(clean_plate, birth_date),
                headers={**self._api_headers(), "X-Aws-Waf-Token": token},
                timeout=4,
            )
            print(f"[Allianz IT] env-token {clean_plate} -> HTTP {resp.status_code}")
            if resp.status_code != 200:
                return None
            return map_allianz_response(clean_plate, resp.json())
        except requests.RequestException as exc:
            print(f"[Allianz IT] env-token error: {exc}")
            return None

    def _lookup_via_api(self, clean_plate: str, birth_date: str) -> Optional[dict]:
        """Fast path ~0.5-2s si token WAF valide."""
        assert self._context is not None
        self._ensure_fresh_token()
        self._refresh_waf_token()
        if not self._waf_token:
            return None

        url = self._product_route_url(clean_plate, birth_date)
        t0 = time.time()
        try:
            response = self._context.request.get(
                url,
                headers=self._api_headers(),
                timeout=API_TIMEOUT_MS,
            )
            elapsed = time.time() - t0
            print(
                f"[Allianz IT] API fast {clean_plate} -> HTTP {response.status} ({elapsed:.1f}s)"
            )
            if response.status == 200:
                self._api_fail_streak = 0
                return map_allianz_response(clean_plate, response.json())
            if response.status in (401, 403):
                self._waf_token = None
                self._token_refreshed_at = 0.0
                self._api_fail_streak += 1
                # Un seul retry après refresh token
                if self._api_fail_streak <= 2:
                    self._ensure_fresh_token()
                    if self._waf_token:
                        retry = self._context.request.get(
                            url,
                            headers=self._api_headers(),
                            timeout=API_TIMEOUT_MS,
                        )
                        print(
                            f"[Allianz IT] API retry {clean_plate} -> HTTP {retry.status}"
                        )
                        if retry.status == 200:
                            self._api_fail_streak = 0
                            return map_allianz_response(clean_plate, retry.json())
        except Exception as exc:
            print(f"[Allianz IT] API fast error: {exc}")
            self._api_fail_streak += 1
        return None

    def _wait_form_ready(self, page: Page) -> None:
        """Attend que le formulaire targa soit interactif (overlay Angular disparu)."""
        try:
            page.locator(".loading-overlay").first.wait_for(state="hidden", timeout=12000)
        except Exception:
            pass
        plate_input = page.locator('input[id*="nx-input"]').first
        plate_input.wait_for(state="visible", timeout=12000)
        page.wait_for_function(
            """() => {
                const input = document.querySelector('input[id*="nx-input"]');
                if (!input || input.disabled) return false;
                const overlay = document.querySelector('.loading-overlay');
                return !overlay || overlay.offsetParent === null;
            }""",
            timeout=8000,
        )

    def _reset_quote_page(self, page: Page) -> None:
        """Recharge le formulaire targa (le SPA avance après un succès)."""
        page.goto(QUOTE_URL, wait_until="domcontentloaded", timeout=30000)
        self._accept_cookies(page)
        self._wait_form_ready(page)
        self._refresh_waf_token(page)

    def _lookup_via_ui(self, clean_plate: str, birth_date: str) -> Optional[dict]:
        """Parcours devis Allianz — fiable sur Railway, ~8-12s avec expect_response."""
        assert self._context is not None
        captured: dict[str, Any] = {}
        dob = _normalize_birth_date_ui(birth_date)
        t0 = time.time()

        page = self._context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        def on_response(response) -> None:
            if not _is_product_route_url(response.url):
                return
            if clean_plate not in response.url.upper().replace("-", ""):
                return
            captured["status"] = response.status
            if response.status == 200:
                try:
                    captured["payload"] = response.json()
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            page.goto(QUOTE_URL, wait_until="domcontentloaded", timeout=20000)
            self._accept_cookies(page)
            self._wait_for_waf_token(page, timeout_ms=6000)

            plate_input = page.locator(
                'input[data-testid="userdata.insuredProperty.licensePlate"], input[id*="nx-input"]'
            ).first
            plate_input.wait_for(state="visible", timeout=10000)
            plate_input.click(force=True, timeout=5000)
            plate_input.fill("")
            plate_input.press_sequentially(clean_plate, delay=20)
            plate_input.press("Tab")
            page.wait_for_timeout(400)

            dob_input = page.locator("#userdata\\.carOwner\\.dateOfBirth")
            if dob_input.is_visible(timeout=1500):
                dob_input.click(force=True)
                dob_input.fill("")
                dob_input.press_sequentially(dob, delay=15)
                dob_input.press("Tab")
                page.wait_for_timeout(300)

            with page.expect_response(
                lambda r: _is_product_route_url(r.url)
                and clean_plate in r.url.upper().replace("-", ""),
                timeout=UI_RESPONSE_TIMEOUT_MS,
            ):
                page.get_by_role("button", name="Calcola Preventivo").click(force=True, timeout=8000)

            elapsed = time.time() - t0
            print(
                f"[Allianz IT] UI {clean_plate} -> {captured.get('status')} ({elapsed:.1f}s)"
            )
            if captured.get("payload"):
                self._refresh_waf_token(page)
                return map_allianz_response(clean_plate, captured["payload"])
            return None
        except Exception as exc:
            elapsed = time.time() - t0
            print(f"[Allianz IT] UI lookup error ({elapsed:.1f}s): {exc}")
            if captured.get("payload"):
                return map_allianz_response(clean_plate, captured["payload"])
            return None
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
            try:
                page.close()
            except Exception:
                pass

    def lookup(self, plate: str, birth_date: str = "") -> Optional[dict]:
        clean_plate = _normalize_plate(plate)
        if len(clean_plate) < 5:
            return None

        t_start = time.time()
        deadline = t_start + LOOKUP_DEADLINE_SECONDS

        # 1) Token env (ms) — sans Playwright
        env_result = self._lookup_with_env_token(clean_plate, birth_date)
        if env_result:
            print(f"[Allianz IT] env OK {clean_plate} ({time.time() - t_start:.1f}s)")
            return env_result

        with self._lock:
            if time.time() >= deadline:
                print(f"[Allianz IT] deadline avant session {clean_plate}")
                return None

            self._ensure_session(force_refresh=False)

            # 2) API product-route avec token session (~0.5-2s)
            api_result = self._lookup_via_api(clean_plate, birth_date)
            if api_result:
                print(f"[Allianz IT] API OK {clean_plate} ({time.time() - t_start:.1f}s)")
                return api_result

            # 3) UI fallback uniquement si budget restant
            if time.time() >= deadline - 3:
                print(f"[Allianz IT] skip UI (deadline) {clean_plate}")
                return None

            ui_result = self._lookup_via_ui(clean_plate, birth_date)
            if ui_result:
                # Token capturé pendant l’UI → prochains lookups en API
                self._refresh_waf_token()
                self._api_fail_streak = 0
                print(f"[Allianz IT] UI OK {clean_plate} ({time.time() - t_start:.1f}s)")
                return ui_result

            print(f"[Allianz IT] Échec {clean_plate} ({time.time() - t_start:.1f}s)")
            return None


class _PlaywrightThread:
    """Un seul thread Playwright dédié (pas de restart à chaud).

    Redémarrer sync_playwright pendant qu'un ancien job tourne provoque :
    "Sync API inside the asyncio loop". Sur timeout on abandonne le résultat
    mais on laisse le worker finir — gunicorn max-requests recycle le process.
    """

    def __init__(self, factory, name: str) -> None:
        self._factory = factory
        self._name = name
        self._jobs: "queue.Queue" = queue.Queue()
        self._busy = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name=name
        )
        self._thread.start()
        print(f"[{self._name}] worker started")

    def _run(self) -> None:
        # Boucle événementielle propre — évite Sync API / asyncio conflict
        try:
            import asyncio

            asyncio.set_event_loop(asyncio.new_event_loop())
        except Exception:
            pass

        session = None
        try:
            session = self._factory()
        except Exception as exc:  # pragma: no cover
            print(f"[{self._name}] session init failed: {exc}")

        while True:
            job = self._jobs.get()
            if job is None:
                return
            fn, args, kwargs, box = job
            if box.get("abandoned"):
                continue
            self._busy.set()
            try:
                if session is None:
                    session = self._factory()
                box["result"] = fn(session, *args, **kwargs)
            except Exception as exc:
                box["error"] = exc
                msg = str(exc)
                # Session corrompue → recrée dans LE MÊME thread (pas un 2e playwright)
                if "asyncio" in msg.lower() or "Sync API" in msg:
                    print(f"[{self._name}] reset session after: {msg[:120]}")
                    try:
                        if session is not None and hasattr(session, "_close"):
                            session._close()
                    except Exception:
                        pass
                    try:
                        session = self._factory()
                    except Exception as exc2:
                        print(f"[{self._name}] session recreate failed: {exc2}")
                        session = None
            finally:
                box["done"].set()
                self._busy.clear()

    def call(self, fn, *args, timeout: float = 120, **kwargs):
        box = {
            "done": threading.Event(),
            "result": None,
            "error": None,
            "abandoned": False,
        }
        self._jobs.put((fn, args, kwargs, box))
        if not box["done"].wait(timeout):
            box["abandoned"] = True
            print(f"[{self._name}] TIMEOUT {timeout:.0f}s — abandon (worker continues)")
            raise TimeoutError(f"[{self._name}] job timeout ({timeout:.0f}s)")
        if box["error"] is not None:
            raise box["error"]
        return box["result"]


_session = _PlaywrightThread(AllianzBrowserSession, 'Allianz IT')


def warmup_session() -> None:
    try:
        _session.call(lambda s: s._ensure_session(force_refresh=True), timeout=45)
    except Exception as exc:
        print(f"[Allianz IT] Warmup error: {exc}")


def search_by_plate(plate: str, birth_date: str = "") -> Optional[dict]:
    return _session.call(
        lambda s: s.lookup(plate, birth_date),
        timeout=LOOKUP_DEADLINE_SECONDS + 8,
    )
