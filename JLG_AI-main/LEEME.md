# JLG AI

Asistente con Gemini: cuentas de usuario, conversaciones guardadas, memoria del usuario,
micrófono (voz a texto), lectura en voz alta, adjuntar imágenes/PDF, interfaz oscura tipo Gemini.

## Usarlo en tu PC (con ngrok)
1. Copia `.env.example` como `api.env` y pon tu `GEMINI_API_KEY` (ya lo tienes).
2. **Windows:** doble clic en `iniciar.bat`.
   **Mac / Linux:** abre una terminal en esta carpeta y ejecuta `./iniciar.sh`
   (ngrok en Mac: `brew install ngrok`; en Linux: https://ngrok.com/download).
3. Abre http://localhost:5000 o la URL `https://...ngrok-free.app` que muestra ngrok.
4. La **primera cuenta que crees es la de administrador** (y recibe el historial antiguo).
   Luego, si no quieres que otros se registren, pon `ALLOW_REGISTRATION=0` en `api.env`.

**URL fija:** en dashboard.ngrok.com → Domains reclama tu dominio gratis y ponlo en `api.env` como
`NGROK_DOMAIN=tu-nombre.ngrok-free.app`. Así la dirección no cambia cada vez que arrancas.

El micrófono necesita HTTPS (ngrok ya lo da) o localhost.
"Abre YouTube", "abre la calculadora", "abre VS Code"... funcionan en Windows, Mac y Linux
(solo en tu propio equipo y solo para el admin). Si una app no está instalada, te lo dice.

## Subirlo a un servidor (siempre encendido, sin tu PC)
**Render** (servidor gratis) + **Supabase** (base de datos Postgres gratis).
1. Supabase → New project → botón **Connect** → copia la cadena **Session pooler**
   (empieza por `postgresql://postgres.xxxx:...@aws-...pooler.supabase.com:5432/postgres`)
   y cambia `[YOUR-PASSWORD]` por la contraseña de la base de datos.
2. Pasa tus datos actuales: `DESTINO_URL="esa-cadena" .venv-unix/bin/python migrar_datos.py`
3. Render → New → **Blueprint** → elige el repo `JLG_AI` (usa `render.yaml`).
4. Rellena `GEMINI_API_KEY` y `DATABASE_URL` (la misma cadena de Supabase). Listo.

En el plan gratis la app se duerme tras ~15 min sin uso y tarda ~1 min en despertar.
En el servidor la función de abrir aplicaciones queda desactivada (no hay escritorio).

## Variables (api.env)
Mira `.env.example`: modelo, modelos de respaldo, tiempo de espera, registro abierto, búsqueda en Google.
