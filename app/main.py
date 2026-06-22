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
ALERT_URL = os.environ.get("ALERT_URL", "https://n8n.axchisan.com/webhook/cc-alert").strip()
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://db.quanta.axchisan.com").strip().rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()


async def _load_session_from_db():
    """Carga el storage_state más reciente desde cc_config (clave 'gemini_session') a STATE_PATH.
    La extensión del navegador mantiene esa fila fresca → la sesión ya no depende del env var.
    Devuelve True si cargó una sesión de la DB."""
    if not SUPABASE_KEY:
        return False
    import json as _json
    import urllib.request

    def _get():
        try:
            url = f"{SUPABASE_URL}/rest/v1/cc_config?clave=eq.gemini_session&select=valor"
            req = urllib.request.Request(
                url, headers={"apikey": SUPABASE_KEY, "Authorization": "Bearer " + SUPABASE_KEY})
            rows = _json.loads(urllib.request.urlopen(req, timeout=15).read())
            if rows and rows[0].get("valor") and rows[0]["valor"].get("cookies"):
                with open(STATE_PATH, "w") as f:
                    _json.dump(rows[0]["valor"], f)
                return True
        except Exception:
            pass
        return False

    try:
        import asyncio
        return await asyncio.to_thread(_get)
    except Exception:
        return False


async def _alert(title, detail=None, level="error", context=None, source="cc-browser"):
    """Reporta al webhook central (cc_logs + WhatsApp si error/critical). Best-effort, sin deps."""
    if not ALERT_URL:
        return
    import json as _json
    import urllib.request

    def _post():
        try:
            data = _json.dumps({
                "source": source, "level": level, "title": title,
                "detail": detail or {}, "context": context,
            }).encode("utf-8")
            req = urllib.request.Request(
                ALERT_URL, data=data, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            pass

    try:
        import asyncio
        await asyncio.to_thread(_post)
    except Exception:
        pass
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
    """Borra la conversación recién creada en Gemini (la más reciente) para no llenar
    el historial. Selectores verificados en la UI de Gemini (2026-06)."""
    sel = 'gem-nav-list-item[data-test-id="conversation"]'
    conv = page.locator(sel).first
    if await conv.count() == 0:
        return
    # 1) Hover sobre la conversación + abrir su menú "Más opciones".
    await conv.hover()
    await page.wait_for_timeout(500)
    btn = conv.locator('button[aria-label^="Más opciones"]').first
    if await btn.count() == 0:
        btn = conv.locator("button").last
    await btn.click(timeout=3000)
    await page.wait_for_timeout(600)
    # 2) Click en "Eliminar".
    await page.locator('[data-test-id="delete-button"]').first.click(timeout=3000)
    await page.wait_for_timeout(700)
    # 3) Confirmar en el diálogo.
    for s in (
        '[data-test-id="confirm-button"]',
        '[role="dialog"] button:has-text("Eliminar")',
        'button:has-text("Eliminar")',
    ):
        try:
            c = page.locator(s).last
            if await c.count() > 0:
                await c.click(timeout=3000)
                break
        except Exception:
            continue
    await page.wait_for_timeout(800)


@app.post("/gen-image")
async def gen_image(req: GenReq, x_api_key: Optional[str] = Header(default=None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválido o ausente.")
    # Refresca la sesión desde la DB (cc_config 'gemini_session'); la extensión la mantiene viva.
    await _load_session_from_db()
    if not os.path.exists(STATE_PATH):
        raise HTTPException(status_code=503, detail="Sin sesión cargada (ni DB ni env).")

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
                await _alert(
                    "🔴 Sesión de Gemini CAÍDA — re-loguear urgente",
                    level="critical",
                    detail={"accion": "re-capturar storage_state (cookies_to_state.py) y actualizar STORAGE_STATE_JSON_B64 en Coolify"},
                    context="cc-browser /gen-image")
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
                await _alert("Gemini no generó imagen a tiempo (timeout)", level="error",
                             detail={"timeout_s": req.timeout_s}, context="cc-browser /gen-image")
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
