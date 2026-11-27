import os
import re
import subprocess
import unicodedata

# Diccionario de sitios web (funciona en Local y en Render)
WEBS = {
    "youtube": "https://www.youtube.com",
    "google": "https://www.google.com",
    "github": "https://github.com",
    "chatgpt": "https://chatgpt.com",
    "gemini": "https://gemini.google.com",
    "whatsapp": "https://web.whatsapp.com",
    "whatsapp web": "https://web.whatsapp.com",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "tiktok": "https://www.tiktok.com",
    "twitch": "https://www.twitch.tv",
    "netflix": "https://www.netflix.com",
    "spotify": "https://open.spotify.com",
    "gmail": "https://mail.google.com",
    "correo": "https://mail.google.com",
    "drive": "https://drive.google.com",
    "google drive": "https://drive.google.com",
    "canva": "https://www.canva.com",
    "render": "https://dashboard.render.com",
    "supabase": "https://supabase.com/dashboard",
    "traductor": "https://translate.google.com",
    "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com",
}

# Diccionario de aplicaciones de Windows (funciona en modo local)
APPS_WINDOWS = {
    "calculadora": "calc.exe",
    "bloc de notas": "notepad.exe",
    "notas": "notepad.exe",
    "notepad": "notepad.exe",
    "paint": "mspaint.exe",
    "explorador": "explorer.exe",
    "explorador de archivos": "explorer.exe",
    "archivos": "explorer.exe",
    "cmd": "cmd.exe",
    "consola": "cmd.exe",
    "terminal": "wt.exe",
    "powershell": "powershell.exe",
    "vscode": "code",
    "visual studio code": "code",
    "vs code": "code",
    "brave": "brave",
    "chrome": "chrome",
    "edge": "msedge",
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "discord": "discord://",
    "steam": "steam://open/main",
    "configuracion": "ms-settings:",
    "ajustes": "ms-settings:",
}


def _normalizar(texto: str) -> str:
    """Convierte a minúsculas, quita tildes y signos de puntuación finales."""
    texto = texto.strip().lower()
    texto = unicodedata.normalize("NFD", texto)
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = re.sub(r"[.!?¡¿,;]+$", "", texto).strip()
    return texto


def procesar_comando_abrir(mensaje_usuario: str):
    """
    Detecta si el usuario pide abrir una web o app.
    Retorna un dict con:
      - ejecutado (bool): True si se reconoció el comando 'abre ...'
      - respuesta (str): Texto que mostrará la IA en el chat
      - abrir_url (str | None): URL para que el navegador la abra
    """
    limpio = _normalizar(mensaje_usuario)

    # Detecta frases como: "abre youtube", "por favor abre la calculadora", "puedes abrir github"
    patron = (
        r"^(?:por favor\s+)?(?:puedes\s+|podrias\s+)?"
        r"(?:abre|abrir|inicia|iniciar|ejecuta|ejecutar)\s+"
        r"(?:la\s+|el\s+|las\s+|los\s+|app\s+|aplicacion\s+|pagina\s+|web\s+|sitio\s+)*(.+)$"
    )
    coincidencia = re.match(patron, limpio)
    if not coincidencia:
        return {"ejecutado": False, "respuesta": None, "abrir_url": None}

    objetivo = coincidencia.group(1).strip()

    # 1. Buscar en aplicaciones locales de Windows primero (si estamos en Windows)
    es_windows = os.name == "nt"
    if objetivo in APPS_WINDOWS:
        comando = APPS_WINDOWS[objetivo]
        if es_windows:
            try:
                if comando.endswith(":") or "://" in comando:
                    os.startfile(comando)
                else:
                    subprocess.Popen(comando, shell=True)
                return {
                    "ejecutado": True,
                    "respuesta": f"Abriendo **{objetivo.title()}** en tu computadora...",
                    "abrir_url": None,
                }
            except Exception as e:
                return {
                    "ejecutado": True,
                    "respuesta": f"Intenté abrir **{objetivo.title()}**, pero ocurrió un error: `{e}`",
                    "abrir_url": None,
                }
        else:
            # Si está en Render (Linux), pero el objetivo también existe como web (ej. Spotify)
            if objetivo in WEBS:
                return {
                    "ejecutado": True,
                    "respuesta": f"Abriendo **{objetivo.title()}** en una nueva pestaña...",
                    "abrir_url": WEBS[objetivo],
                }
            return {
                "ejecutado": True,
                "respuesta": (
                    f"**{objetivo.title()}** es una aplicación de escritorio de Windows. "
                    "Para abrir programas de tu PC, ejecuta JLG AI en modo local."
                ),
                "abrir_url": None,
            }

    # 2. Buscar en el diccionario de páginas web
    if objetivo in WEBS:
        url = WEBS[objetivo]
        return {
            "ejecutado": True,
            "respuesta": f"Abriendo **{objetivo.title()}** en una nueva pestaña...",
            "abrir_url": url,
        }

    # 3. Detectar si escribió un dominio directo (ej. "abre ucv.edu.pe" o "abre twitch.tv")
    if re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(/.*)?$", objetivo):
        url = objetivo if objetivo.startswith("http") else f"https://{objetivo}"
        return {
            "ejecutado": True,
            "respuesta": f"Abriendo **{url}** en una nueva pestaña...",
            "abrir_url": url,
        }

    # Si dijo "abre algo" que no está en la lista, devolvemos ejecutado=False
    # para que Gemini responda normalmente o le dé el enlace.
    return {"ejecutado": False, "respuesta": None, "abrir_url": None}