"""
Lookup targa italiana via Prima.it — session Playwright persistente.

Flux:
1. preventivo.prima.it/targa → targa + data di nascita
2. Click "Procedi" → dialog conferma → "Continua come privato"
3. Click "Procedi" → pagina "Acquisto veicolo" (mese/anno immatricolazione)
4. Click "Procedi" → pagina "Informazioni veicolo" (marca, modello, allestimento)
5. Estrae e ritorna i dati veicolo strutturati
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any, Optional

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

ENTRY_URL = "https://preventivo.prima.it/targa"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SESSION_TTL_SECONDS = int(os.environ.get("PRIMA_SESSION_TTL_SECONDS", "1800"))
UI_TIMEOUT_MS = int(os.environ.get("PRIMA_UI_TIMEOUT_MS", "15000"))
# Budget total par lookup (cible client ≤25s)
LOOKUP_DEADLINE_SECONDS = float(os.environ.get("PRIMA_LOOKUP_DEADLINE_SECONDS", "22"))


def _normalize_plate(plate: str) -> str:
    return plate.upper().replace(" ", "").replace("-", "")


def _year_from_date(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    match = re.search(r"(19|20)\d{2}", value)
    return match.group(0) if match else None


class PrimaBrowserSession:
    """Singleton Chromium — session Prima.it réutilisée."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._session_started_at = 0.0
        self._cookies_accepted = False

    def _session_expired(self) -> bool:
        if not self._context:
            return True
        return (time.time() - self._session_started_at) >= SESSION_TTL_SECONDS

    def _close(self) -> None:
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
        self._cookies_accepted = False

    def _accept_cookies(self, page: Page) -> None:
        if self._cookies_accepted:
            return
        for selector in (
            "button:has-text('Accetta tutti')",
            "button:has-text('Accetta')",
            "[data-testid='accept-all']",
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

    def _bootstrap(self) -> None:
        print("[Prima IT] Bootstrap session Playwright…")
        if not self._playwright:
            self._playwright = sync_playwright().start()

        self._close()

        self._browser = self._playwright.chromium.launch(
            headless=os.environ.get("PRIMA_HEADLESS", "true").lower() == "true",
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
        self._session_started_at = time.time()
        print("[Prima IT] Session prête")

    def _ensure_session(self, force_refresh: bool = False) -> None:
        if force_refresh or self._session_expired():
            self._bootstrap()
        elif not self._context:
            self._bootstrap()

    def _get_select_value(self, page: Page, label: str) -> Optional[str]:
        """Extrait la valeur d'un combobox/input par son label."""
        try:
            locator = page.get_by_label(label)
            if locator.count() > 0:
                value = locator.input_value(timeout=2000)
                if value and value.strip() and "Seleziona" not in value:
                    return value.strip()
        except Exception:
            pass
        return None

    def _get_select_options(self, page: Page, label: str) -> list[str]:
        """Extrait les options disponibles d'un combobox."""
        options = []
        try:
            locator = page.get_by_label(label)
            if locator.count() > 0:
                # Le combobox peut être un <select> ou un custom dropdown
                value = locator.input_value(timeout=1000)
                if value:
                    options.append(value)
        except Exception:
            pass
        return options

    def _extract_vehicle_data(self, page: Page) -> dict:
        """Extrait les données véhicule de la page 'Informazioni aggiuntive veicolo'."""
        data: dict[str, Any] = {}

        # Marque
        marca = self._get_select_value(page, "Qual è il marchio?")
        if marca:
            data["marca"] = marca

        # Modèle
        modello = self._get_select_value(page, "Qual è il modello?")
        if modello:
            # Nettoyer le suffixe "(2005-2011)" etc.
            clean_modello = re.sub(r"\s*\(.*?\)\s*$", "", modello).strip()
            data["modello"] = modello
            data["modello_clean"] = clean_modello

        # Allestimento
        allestimento = self._get_select_value(page, "Quali sono allestimento e alimentazione?")
        if allestimento:
            data["allestimento"] = allestimento
            # Extraire l'année de l'allestimento: "Kona 1.0 T-GDI Hybrid 48V iMT XLine (2023)"
            year_match = re.search(r"\((\d{4})\)", allestimento)
            if year_match:
                data["anno_allestimento"] = year_match.group(1)

        # Kilométrage annuel sélectionné
        km = self._get_select_value(page, "Quanti sono i km percorsi in un anno?")
        if km:
            data["km_annui"] = km

        # GPL/Metano
        try:
            gpl_si = page.get_by_role("radio", name="Sì").nth(0)
            gpl_no = page.get_by_role("radio", name="No").nth(0)
            if gpl_no.is_checked(timeout=500):
                data["gpl_metano"] = False
            elif gpl_si.is_checked(timeout=500):
                data["gpl_metano"] = True
        except Exception:
            pass

        return data

    def _extract_registration_date(self, page: Page) -> dict:
        """Extrait mois/année d'immatriculation de la page 'Acquisto veicolo'."""
        data: dict[str, Any] = {}

        mese = self._get_select_value(page, "Mese")
        anno = self._get_select_value(page, "Anno")

        if mese:
            try:
                data["mese_immatricolazione"] = int(mese)
            except ValueError:
                data["mese_immatricolazione"] = mese

        if anno:
            try:
                data["anno_immatricolazione"] = int(anno)
            except ValueError:
                data["anno_immatricolazione"] = anno

        # Switch "data di acquisto diversa"
        try:
            switch = page.get_by_role("switch").first
            if switch.is_checked(timeout=500):
                data["data_acquisto_diversa"] = True
        except Exception:
            pass

        # Switch "leasing/finanziamento"
        try:
            switches = page.get_by_role("switch").all()
            if len(switches) > 1 and switches[1].is_checked(timeout=500):
                data["leasing_finanziamento"] = True
            else:
                data["leasing_finanziamento"] = False
        except Exception:
            pass

        return data

    def lookup(self, plate: str, birth_date: str = "") -> Optional[dict]:
        """Lookup complet Prima.it pour une plaque donnée."""
        clean_plate = _normalize_plate(plate)
        if len(clean_plate) < 5:
            return None

        deadline = time.time() + LOOKUP_DEADLINE_SECONDS
        t0 = time.time()

        with self._lock:
            self._ensure_session(force_refresh=False)
            assert self._context is not None

            remaining = deadline - time.time()
            if remaining < 5:
                print(f"[Prima IT] budget trop court {clean_plate} ({remaining:.1f}s)")
                return None

            page = self._context.new_page()
            page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )

            captured_data: dict[str, Any] = {}
            application_id: Optional[str] = None

            try:
                # Étape 1: Page targa
                print(f"[Prima IT] Loading targa page for {clean_plate}")
                page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=20000)
                self._accept_cookies(page)
                page.wait_for_timeout(500)

                # Étape 2: Remplir le formulaire
                plate_input = page.get_by_placeholder("Inserisci numero di targa")
                plate_input.click(timeout=5000)
                plate_input.fill(clean_plate, timeout=4000)

                # Date de naissance
                dob = (birth_date or "01/01/1990").strip()
                dob_parts = dob.replace("-", "/").replace(".", "/").split("/")
                if len(dob_parts) == 3:
                    day, month, year = dob_parts[0], dob_parts[1], dob_parts[2]
                else:
                    day, month, year = "01", "01", "1990"

                page.get_by_placeholder("GG").fill(day, timeout=2000)
                page.get_by_placeholder("MM").fill(month, timeout=2000)
                page.get_by_placeholder("AAAA").fill(year, timeout=2000)

                page.wait_for_timeout(800)

                # Étape 3: Click "Procedi"
                procedi_btn = page.get_by_role("button", name="Procedi")
                if procedi_btn.is_disabled(timeout=1000):
                    # Attendre que le site reconnaisse la plaque
                    page.wait_for_timeout(3000)

                procedi_btn.click(timeout=5000)
                page.wait_for_timeout(1500)

                # Étape 4: Dialog de confirmation → "Continua come privato"
                continua_privato = page.get_by_role("button", name="Continua come privato")
                if continua_privato.is_visible(timeout=2000):
                    continua_privato.click(timeout=3000)
                    page.wait_for_timeout(1500)
                    # Re-cliquer Procedi après le dialog
                    try:
                        page.get_by_role("button", name="Procedi").click(timeout=5000)
                        page.wait_for_timeout(2000)
                    except Exception:
                        pass

                # Capturer l'application_id de l'URL
                current_url = page.url
                match = re.search(r"/form/([^/]+)/", current_url)
                if match:
                    application_id = match.group(1)
                    captured_data["application_id"] = application_id

                # Étape 5: Page "Acquisto veicolo" → extraire date immatricolazione
                reg_data = self._extract_registration_date(page)
                captured_data.update(reg_data)

                if "mese_immatricolazione" not in captured_data:
                    print(f"[Prima IT] No registration date found for {clean_plate}")
                    return None

                # Étape 6: Click "Procedi" → page "Informazioni veicolo"
                page.get_by_role("button", name="Procedi").click(timeout=5000)
                page.wait_for_timeout(2000)

                # Étape 7: Extraire les données véhicule
                vehicle_data = self._extract_vehicle_data(page)
                captured_data.update(vehicle_data)

                elapsed = time.time() - t0
                print(f"[Prima IT] OK {clean_plate} -> {captured_data.get('marca')} {captured_data.get('modello')} ({elapsed:.1f}s)")

                # Construire le résultat structuré
                marca = captured_data.get("marca")
                modello = captured_data.get("modello_clean") or captured_data.get("modello")
                allestimento = captured_data.get("allestimento", "")

                # Déduire l'année
                anno = captured_data.get("anno_allestimento") or \
                       str(captured_data.get("anno_immatricolazione", ""))

                # Déduire le carburant de l'allestimento
                fuel = None
                if allestimento:
                    if "Hybrid" in allestimento or "hybrid" in allestimento.lower():
                        fuel = "Ibrido"
                    elif "Elettrica" in allestimento or "Electric" in allestimento:
                        fuel = "Elettrico"
                    elif "GPL" in allestimento:
                        fuel = "GPL"
                    elif "Metano" in allestimento or "Methane" in allestimento:
                        fuel = "Metano"
                    elif "Diesel" in allestimento or "diesel" in allestimento.lower():
                        fuel = "Diesel"
                    else:
                        fuel = "Benzina"

                # Déduire la cylindrée de l'allestimento
                cylindree = None
                cc_match = re.search(r"(\d+\.?\d*)\s*(?:T-GDI|TDI|TFSI|TSI|JTD|MultiJet|HDi|THP|PureTech|TCe|Ecoboost)", allestimento)
                if cc_match:
                    cylindree = f"{cc_match.group(1)}L"

                nom_complet = " ".join(x for x in [marca, modello, allestimento] if x) or None

                result = {
                    "plate": clean_plate,
                    "marca": marca,
                    "modello": modello,
                    "version": allestimento,
                    "nom_complet": nom_complet,
                    "anno": anno,
                    "date_mec": f"{captured_data.get('anno_immatricolazione', '')}-{captured_data.get('mese_immatricolazione', 1):02d}-01" if captured_data.get("anno_immatricolazione") else None,
                    "mese_immatricolazione": captured_data.get("mese_immatricolazione"),
                    "anno_immatricolazione": captured_data.get("anno_immatricolazione"),
                    "energie": fuel,
                    "cylindree": cylindree,
                    "puissance": None,
                    "puissance_kw": None,
                    "puissance_cv": None,
                    "prix_estime": None,
                    "vin": None,
                    "km_annui": captured_data.get("km_annui"),
                    "gpl_metano": captured_data.get("gpl_metano"),
                    "leasing_finanziamento": captured_data.get("leasing_finanziamento"),
                    "application_id": application_id,
                    "source": "prima",
                    "raw": captured_data,
                }

                return result

            except Exception as exc:
                elapsed = time.time() - t0
                print(f"[Prima IT] Error for {clean_plate} ({elapsed:.1f}s): {exc}")
                # Retourner ce qu'on a capturé même en cas d'erreur
                if captured_data.get("marca") or captured_data.get("modello"):
                    return {
                        "plate": clean_plate,
                        "marca": captured_data.get("marca"),
                        "modello": captured_data.get("modello"),
                        "version": captured_data.get("allestimento"),
                        "source": "prima",
                        "raw": captured_data,
                        "error": str(exc),
                    }
                return None
            finally:
                try:
                    page.close()
                except Exception:
                    pass


def warmup_session() -> None:
    try:
        _session._ensure_session(force_refresh=True)
    except Exception as exc:
        print(f"[Prima IT] Warmup error: {exc}")


_session = PrimaBrowserSession()


def search_by_plate(plate: str, birth_date: str = "") -> Optional[dict]:
    """Lookup Prima.it plafonné à LOOKUP_DEADLINE_SECONDS (défaut 22s)."""
    return _session.lookup(plate, birth_date)
