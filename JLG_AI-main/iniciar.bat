@echo off
cd /d "%~dp0"
echo Iniciando JLG AI...

if not exist .venv\Scripts\python.exe (
  echo Creando entorno virtual...
  python -m venv .venv
)
call .venv\Scripts\activate
pip install -q -r requirements.txt

:: Lee NGROK_DOMAIN de api.env (opcional: tu dominio fijo gratis de ngrok)
set "NGROK_DOMAIN="
for /f "usebackq tokens=1,* delims==" %%a in ("api.env") do (
  if /i "%%a"=="NGROK_DOMAIN" set "NGROK_DOMAIN=%%b"
)

:: Servidor Flask (en tu PC se permite abrir aplicaciones)
start "JLG AI - Servidor" cmd /k "set "ENABLE_LOCAL_APPS=1" && python app.py"

timeout /t 3 >nul
if defined NGROK_DOMAIN (
  start "JLG AI - ngrok" cmd /k "ngrok http --url=%NGROK_DOMAIN% 5000"
  echo Listo. Tu app: https://%NGROK_DOMAIN%
) else (
  start "JLG AI - ngrok" cmd /k "ngrok http 5000"
  echo Listo. Abre http://localhost:5000 o la URL https que muestra ngrok.
)
