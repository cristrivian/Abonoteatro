#!/usr/bin/env python3
"""
Detecta novedades en la programación de Abonoteatro.

La primera ejecución abre un navegador visible para iniciar sesión manualmente.
La sesión se guarda en .abonoteatro-session/ y se reutiliza en ejecuciones
posteriores. No se guardan contraseñas.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


PROGRAM_URL = "https://www.abonoteatro.com/programacion"
BASE_URL = "https://www.abonoteatro.com"
STATE_FILE = Path(os.environ.get("STATE_FILE", "shows_vistos.json"))
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID") or "-1004359686735"


@dataclass(frozen=True)
class Actuacion:
    title: str
    url: str
    details: str
    fingerprint: str


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def fingerprint(title: str, url: str, details: str) -> str:
    # El texto descriptivo puede cambiar; el título y la URL identifican
    # la ficha y permiten distinguir "modificada" de "nueva".
    raw = f"{title.lower()}|{url}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def looks_like_event(text: str, href: str) -> bool:
    """Descarta navegación y conserva enlaces que parecen fichas de obras."""
    if not text or len(text) < 5 or len(text) > 300:
        return False
    path = urlparse(href).path.lower()
    blocked = (
        "/auth/",
        "/legal",
        "/contact",
        "/cookies",
        "/faq",
        "/blog",
        "/programacion",
    )
    if any(part in path for part in blocked):
        return False
    words = ("teatro", "obra", "musical", "espectáculo", "comedia", "drama")
    return any(word in text.lower() for word in words) or len(path.strip("/").split("/")) >= 2


def extract_actuaciones(html: str) -> list[Actuacion]:
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, Actuacion] = {}

    # En primer lugar intenta tarjetas y artículos, que suelen representar
    # una actuación completa y evitan mezclar enlaces de la cabecera.
    candidates = soup.select(
        "main article, main li, main [class*='card'], main [class*='event']"
    )
    if not candidates:
        candidates = soup.select("main h1, main h2, main h3, main h4, main a[href]")
    if not candidates:
        candidates = soup.select("h3, h4")

    for candidate in candidates:
        anchor = (
            candidate
            if candidate.name == "a"
            else candidate.select_one("a[href]")
        )
        heading = (
            candidate
            if candidate.name in {"h3", "h4"}
            else candidate.select_one("h3, h4")
        )
        if not anchor and candidate.name in {"h1", "h2", "h3", "h4"}:
            anchor = candidate.find_parent("a", href=True)
        if not anchor and candidate.name in {"h3", "h4"}:
            # Algunas versiones de la web muestran la ficha como una tarjeta
            # no enlazada. En ese caso usamos la página de programación como
            # URL estable y el título como identificador.
            title = clean_text(candidate.get_text(" ", strip=True))
            href = PROGRAM_URL
            details = clean_text(
                candidate.parent.get_text(" ", strip=True)
                if candidate.parent
                else title
            )
            if (
                len(title) >= 5
                and title.lower() not in {"programación", "filtros", "categorías"}
            ):
                item = Actuacion(
                    title,
                    href,
                    details,
                    fingerprint(title, href, details),
                )
                found[item.fingerprint] = item
            continue
        if not anchor and heading:
            title = clean_text(heading.get_text(" ", strip=True))
            details = clean_text(candidate.get_text(" ", strip=True))
            if len(title) >= 5:
                item = Actuacion(
                    title,
                    PROGRAM_URL,
                    details,
                    fingerprint(title, PROGRAM_URL, details),
                )
                found[item.fingerprint] = item
            continue
        if not anchor:
            continue
        title = clean_text(anchor.get_text(" ", strip=True))
        href = urljoin(BASE_URL, anchor.get("href", ""))
        details = clean_text(candidate.get_text(" ", strip=True))
        if not looks_like_event(title, href):
            continue
        # Si el título es solo "Ver más", usa la primera línea útil de la tarjeta.
        if title.lower() in {"ver más", "más información", "detalles"}:
            title = details[:120]
        item = Actuacion(title, href, details, fingerprint(title, href, details))
        found[item.fingerprint] = item

    return sorted(found.values(), key=lambda item: (item.title.lower(), item.url))


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print(f"Aviso: no se pudo leer {STATE_FILE}; se creará de nuevo.", file=sys.stderr)
        return {}


def save_state(items: list[Actuacion]) -> None:
    payload = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "items": {item.fingerprint: asdict(item) for item in items},
    }
    STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def fetch_page() -> str:
    email = os.environ.get("EMAIL")
    password = os.environ.get("PASSWORD")
    if not email or not password:
        raise RuntimeError("Faltan las variables de entorno EMAIL o PASSWORD.")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(PROGRAM_URL, wait_until="domcontentloaded", timeout=60_000)

        if "/auth/login" in page.url:
            page.locator("input[type='email'], input[name='email']").first.fill(email)
            page.locator("input[type='password'], input[name='password']").first.fill(password)
            page.locator("button[type='submit']").first.click()
            page.wait_for_load_state("domcontentloaded", timeout=60_000)

        page.goto(PROGRAM_URL, wait_until="domcontentloaded", timeout=60_000)
        if "/auth/login" in page.url:
            browser.close()
            raise RuntimeError("No se pudo iniciar sesión; comprueba EMAIL y PASSWORD.")

        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(1_000)
        html = page.content()
        browser.close()
        return html


def print_changes(new_items: list[Actuacion], changed_items: list[Actuacion]) -> None:
    if not new_items and not changed_items:
        print("Sin novedades.")
        return
    if new_items:
        print(f"\nNUEVAS ACTUACIONES ({len(new_items)}):")
        for item in new_items:
            print(f"- {item.title}\n  {item.url}")
            if item.details != item.title:
                print(f"  {item.details}")


def enviar_telegram(new_items: list[Actuacion], changed_items: list[Actuacion]) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token or not (new_items or changed_items):
        return
    import requests

    lines = ["🎭 <b>Novedades en Abonoteatro</b>", ""]
    for label, items in (("Nuevas", new_items), ("Modificadas", changed_items)):
        if items:
            lines.append(f"<b>{label}:</b>")
            for item in items:
                lines.append(f"• {item.title}\n{item.url}")
            lines.append("")
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": "\n".join(lines), "parse_mode": "HTML"},
        timeout=30,
    )
    response.raise_for_status()
    if changed_items:
        print(f"\nACTUACIONES MODIFICADAS ({len(changed_items)}):")
        for item in changed_items:
            print(f"- {item.title}\n  {item.url}\n  {item.details}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--notify-initial",
        action="store_true",
        help="muestra toda la programación en la primera ejecución",
    )
    args = parser.parse_args()

    try:
        html = fetch_page()
        current = extract_actuaciones(html)
        if not current:
            raise RuntimeError(
                "No se encontraron actuaciones. La página puede haber cambiado "
                "o la sesión puede no tener acceso a la programación."
            )

        previous = load_state().get("items", {})
        new_items = [item for item in current if item.fingerprint not in previous]
        changed_items = [
            item
            for item in current
            if item.fingerprint in previous
            and item.details != previous[item.fingerprint].get("details")
        ]

        if previous or args.notify_initial:
            print_changes(new_items, changed_items)
        else:
            print(f"Estado inicial guardado: {len(current)} actuaciones.")
            print("En la próxima ejecución solo se mostrarán las novedades.")
        enviar_telegram(new_items, changed_items)
        save_state(current)
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())