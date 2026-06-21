# cc-browser

Servicio de automatización de navegador (Playwright + Chromium) para **CreationContent / Tecnobichos**.
Genera **infografías con Gemini web** usando la sesión logueada del dueño (Google AI Plus) — la versión
web no tiene las restricciones de pago de la API.

## Endpoints
- `GET /health` → estado + si hay sesión cargada.
- `POST /gen-image` `{prompt, timeout_s}` → maneja gemini.google.com, genera la imagen y devuelve PNG. Header `X-API-Key`.

## Sesión
La sesión se inyecta por la env var `STORAGE_STATE_JSON` (contenido de storage_state.json, ~12KB). NO se commitea.
Se captura con los scripts de `capturar-sesion-gemini/`.

## Env
| Var | Uso |
|---|---|
| `BROWSER_API_KEY` | Si se define, exige header `X-API-Key`. |
| `STORAGE_STATE_JSON` | JSON de la sesión de Google (cookies). |
