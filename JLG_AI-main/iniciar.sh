#!/bin/bash
# Arranque de JLG AI en Mac y Linux:  ./iniciar.sh
cd "$(dirname "$0")" || exit 1
echo "Iniciando JLG AI..."

if ! command -v python3 >/dev/null; then
  echo "❌ Falta Python 3. Mac: brew install python  |  Ubuntu/Debian: sudo apt install python3 python3-venv"
  exit 1
fi

if [ ! -x .venv-unix/bin/python ]; then
  echo "Creando entorno virtual..."
  python3 -m venv .venv-unix || { echo "❌ En Ubuntu/Debian instala: sudo apt install python3-venv"; exit 1; }
fi
source .venv-unix/bin/activate
pip install -q -r requirements.txt

if [ ! -f api.env ]; then
  cp .env.example api.env
  echo "⚠️  Creé api.env: pon tu GEMINI_API_KEY dentro y vuelve a ejecutar."
  exit 1
fi

NGROK_DOMAIN=$(grep -E '^NGROK_DOMAIN=' api.env | cut -d= -f2- | tr -d '\r" ')
PORT=5050  # en Mac el 5000 lo usa AirPlay

# ¿Ya hay algo usando el puerto? (por ejemplo, JLG AI abierto en otra ventana)
if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "❌ El puerto $PORT ya está en uso: JLG AI (u otro programa) ya está abierto."
  echo "   Ciérralo con Ctrl+C en su ventana, o ejecuta:  kill \$(lsof -t -iTCP:$PORT -sTCP:LISTEN)"
  exit 1
fi
if pgrep -x ngrok >/dev/null; then
  echo "❌ ngrok ya está abierto en otra ventana. Ciérralo (Ctrl+C) o ejecuta:  pkill ngrok"
  exit 1
fi

# Servidor (en tu equipo se permite abrir aplicaciones)
ENABLE_LOCAL_APPS=1 PORT=$PORT python app.py &
SERVER_PID=$!
trap 'echo; echo "Cerrando JLG AI..."; kill $SERVER_PID 2>/dev/null' EXIT INT TERM
sleep 3

if ! command -v ngrok >/dev/null; then
  echo "⚠️  ngrok no está instalado. Mac: brew install ngrok  |  Linux: https://ngrok.com/download"
  echo "   Mientras tanto, abre http://localhost:$PORT  (Ctrl+C para salir)"
  wait $SERVER_PID
elif [ -n "$NGROK_DOMAIN" ]; then
  echo "✅ Tu app: https://$NGROK_DOMAIN"
  ngrok http --url="$NGROK_DOMAIN" $PORT
else
  echo "✅ Abre la URL https que muestra ngrok (o http://localhost:$PORT)"
  ngrok http $PORT
fi
