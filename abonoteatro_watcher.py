import json
import os
import requests
from playwright.sync_api import sync_playwright

# --- CONFIGURACIÓN ---
EMAIL = os.environ.get("EMAIL")
PASSWORD = os.environ.get("PASSWORD")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")

# ID del canal fija en el código
TELEGRAM_CHAT_ID = "-1004359686735"

DB_FILE = "shows_vistos.json"

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
    try:
        res = requests.post(url, json=payload)
        res.raise_for_status()
    except Exception as e:
        print(f"Error enviando notificación a Telegram: {e}")

def cargar_vistos():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def guardar_vistos(lista_shows):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(lista_shows, f, ensure_ascii=False, indent=2)

def rastrear_abonoteatro():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print("Iniciando sesión en Abono Teatro...")
        page.goto("https://www.abonoteatro.com/acceso")
        
        # Formulario de login
        page.fill("input[name='email']", EMAIL)
        page.fill("input[name='password']", PASSWORD)
        page.click("button[type='submit']")
        page.wait_for_load_state("networkidle")

        print("Navegando a la programación...")
        page.goto("https://www.abonoteatro.com/programacion")
        page.wait_for_timeout(3000)

        # Captura de espectáculos
        elementos = page.query_selector_all("h3, h4, .titulo")
        titulos_actuales = list(set([el.inner_text().strip() for el in elementos if el.inner_text().strip()]))

        browser.close()

        vistos = cargar_vistos()
        nuevos = [t for t in titulos_actuales if t not in vistos]

        if nuevos:
            mensaje = f"🎭 <b>¡Nuevos espectáculos en Abono Teatro!</b>\n\n" + "\n".join([f"• {t}" for t in nuevos])
            enviar_telegram(mensaje)
            guardar_vistos(list(set(vistos + nuevos)))
            print(f"Se encontraron {len(nuevos)} novedades y se envió la alerta.")
        else:
            print("No se encontraron espectáculos nuevos.")

if __name__ == "__main__":
    rastrear_abonoteatro()
