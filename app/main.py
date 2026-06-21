"""
cc-browser — servicio de automatización de navegador para Tecnobichos.

Genera infografías con Gemini web (gemini.google.com) reusando la sesión
logueada del dueño (Google AI Plus). La versión web no tiene las restricciones
de pago de la API de imágenes.

Endpoints:
  GET  /health    → estado + si hay sesión cargada.
  POST /gen-image → {prompt, timeout_s} -> PNG (image/png). Header X-API-Key.
"""
import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from playwright.async_api import async_playwright

API_KEY = os.environ.get("BROWSER_API_KEY", "").strip()
STATE_PATH = "/tmp/storage_state.json"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Marca el <img> generado por Gemini (grande, no avatar) con un id para capturarlo.
TAG_JS = """() => {
  const imgs = [...document.querySelectorAll('img')];
  const big = imgs.filter(i => i.naturalWidth >= 380 && i.naturalHeight >= 380
                               && !((i.src || '').includes('/a/')));
  big.sort((a, b) => (b.naturalWidth * b.naturalHeight) - (a.naturalWidth * a.naturalHeight));
  if (big.length) { big[0].id = 'cc_genimg'; big[0].scrollIntoView(); return true; }
  return false;
}"""

app = FastAPI(title="cc-browser", version="0.1.0")


@app.on_event("startup")
async def _startup():
    import base64
    b64 = os.environ.get("STORAGE_STATE_JSON_B64", "").strip()
    raw = os.environ.get("STORAGE_STATE_JSON", "").strip()
    if b64:
        with open(STATE_PATH, "wb") as f:
            f.write(base64.b64decode(b64))
    elif raw:
        with open(STATE_PATH, "w") as f:
            f.write(raw)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "cc-browser", "session": os.path.exists(STATE_PATH)}


class GenReq(BaseModel):
    prompt: str = Field(..., description="Instrucción para que Gemini genere la imagen.")
    timeout_s: int = Field(default=170, description="Máximo a esperar la generación (s).")


async def _delete_conversation(page):
    """Borra la conversación actual de Gemini (best-effort) para no llenar el historial."""
    # 1) Abrir el menú de "más opciones" de la conversación activa (varios selectores posibles).
    opened = False
    for sel in (
        '[data-test-id="actions-menu-button"]',
        'button[aria-label*="opcion" i]',
        'button[aria-label*="más" i]',
        'button[aria-label*="more" i]',
        'button[aria-label*="menu" i]',
    ):
        try:
            btn = page.locator(sel).first
            if await btn.count() > 0:
                await btn.click(timeout=2500)
                opened = True
                break
        except Exception:
            continue
    if not opened:
        return
    await page.wait_for_timeout(500)
    # 2) Click en "Eliminar"/"Delete" del menú.
    for sel in (
        '[role="menuitem"]:has-text("Eliminar")',
        '[role="menuitem"]:has-text("Delete")',
        'button:has-text("Eliminar")',
        'button:has-text("Delete")',
    ):
        try:
            it = page.locator(sel).first
            if await it.count() > 0:
                await it.click(timeout=2500)
                break
        except Exception:
            continue
    await page.wait_for_timeout(500)
    # 3) Confirmar en el diálogo.
    for sel in (
        '[role="dialog"] button:has-text("Eliminar")',
        '[role="dialog"] button:has-text("Delete")',
        'button:has-text("Eliminar")',
        'button:has-text("Delete")',
    ):
        try:
            c = page.locator(sel).last
            if await c.count() > 0:
                await c.click(timeout=2500)
                break
        except Exception:
            continue
    await page.wait_for_timeout(600)


@app.post("/gen-image")
async def gen_image(req: GenReq, x_api_key: Optional[str] = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválido o ausente.")
    if not os.path.exists(STATE_PATH):
        raise HTTPException(status_code=503, detail="Sin sesión cargada (STORAGE_STATE_JSON).")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            storage_state=STATE_PATH, locale="es-ES", device_scale_factor=2,
            user_agent=UA, viewport={"width": 1280, "height": 1500},
        )
        await ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = await ctx.new_page()
        try:
            await page.goto("https://gemini.google.com/app",
                            wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(7000)

            # ¿sesión válida? (si pide login, falla claro)
            body = (await page.inner_text("body"))[:400].lower()
            if "iniciar sesión" in body or "sign in to" in body:
                raise HTTPException(status_code=403, detail="Sesión inválida/expirada en este entorno.")

            box = page.locator('div[contenteditable="true"]').first
            await box.click()
            await box.type(req.prompt, delay=5)
            await page.wait_for_timeout(400)
            await page.keyboard.press("Enter")

            found = False
            tries = max(10, req.timeout_s // 3)
            for _ in range(tries):
                await page.wait_for_timeout(3000)
                if await page.evaluate(TAG_JS):
                    found = True
                    break
            if not found:
                raise HTTPException(status_code=504, detail="Gemini no generó imagen a tiempo.")

            await page.wait_for_timeout(1500)
            png = await page.locator("#cc_genimg").screenshot()
            # La imagen ya está en memoria: borramos la conversación en Gemini para no
            # llenar el historial del dueño (best-effort, no rompe si la UI cambia).
            try:
                await _delete_conversation(page)
            except Exception:
                pass
            return Response(content=png, media_type="image/png")
        finally:
            await browser.close()
