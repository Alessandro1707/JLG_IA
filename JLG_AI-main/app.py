"""JLG AI — asistente con Gemini, usuarios, conversaciones, memoria y voz."""
import base64
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import threading
import time
import unicodedata
from functools import wraps
from urllib.parse import quote, urlparse

from dotenv import load_dotenv
from flask import (Flask, Response, abort, jsonify, redirect, render_template, request,
                   send_from_directory, session, stream_with_context, url_for)
from google import genai
from google.genai import types

from database import (Conversacion, Mensaje, Recuerdo, Usuario, ahora, db,
                      importar_historial_antiguo, init_db)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GENERATED_DIR = os.path.join(BASE_DIR, "generated_images")
os.makedirs(GENERATED_DIR, exist_ok=True)

load_dotenv(os.path.join(BASE_DIR, "api.env"))
load_dotenv(os.path.join(BASE_DIR, ".env"))

MODELO = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")          # modo Rápido
MODELO_PRO = os.getenv("GEMINI_MODEL_PRO", "gemini-3.8-flash")        # modo Avanzado
MODELO_IMAGEN = os.getenv("GEMINI_IMAGE_MODEL", "imagen-3.0-generate-002")
BUSQUEDA_GOOGLE = os.getenv("ENABLE_GOOGLE_SEARCH", "0") == "1"
BUSQUEDA_WEB = os.getenv("ENABLE_WEB_SEARCH", "1") == "1"  # búsqueda gratis con DuckDuckGo
SISTEMA = platform.system()  # "Windows", "Darwin" (Mac) o "Linux"
APPS_LOCALES = os.getenv("ENABLE_LOCAL_APPS", "0") == "1" and SISTEMA in ("Windows", "Darwin", "Linux")
REGISTRO_ABIERTO = os.getenv("ALLOW_REGISTRATION", "1") == "1"
MODELO_MEMORIA = os.getenv("GEMINI_MEMORY_MODEL", "gemini-3.5-flash-lite")  # rápido y barato
MAX_HISTORIAL = 30          # mensajes anteriores que se envían a Gemini como contexto
MAX_RECUERDOS = 100
MAX_ARCHIVO_MB = 15

app = Flask(__name__)


def clave_secreta():
    if os.getenv("SECRET_KEY"):
        return os.getenv("SECRET_KEY")
    ruta = os.path.join(BASE_DIR, ".secret_key")  # se crea sola la primera vez
    if not os.path.exists(ruta):
        with open(ruta, "w") as f:
            f.write(secrets.token_hex(32))
    return open(ruta).read().strip()


app.config["SECRET_KEY"] = clave_secreta()
app.config["MAX_CONTENT_LENGTH"] = MAX_ARCHIVO_MB * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30  # 30 días
init_db(app)

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY"),
    http_options=types.HttpOptions(timeout=int(os.getenv("GEMINI_TIMEOUT_S", "25")) * 1000),
)


# ---------------------------------------------------------------- autenticación
def usuario_actual():
    uid = session.get("uid")
    return db.session.get(Usuario, uid) if uid else None


def login_requerido(f):
    @wraps(f)
    def envoltura(*args, **kwargs):
        if not usuario_actual():
            if request.path.startswith("/api/"):
                return jsonify({"error": "No has iniciado sesión"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return envoltura


@app.route("/login", methods=["GET", "POST"])
def login():
    if usuario_actual():
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        u = Usuario.query.filter_by(email=email).first()
        if u and u.check_password(password):
            session.permanent = True
            session["uid"] = u.id
            return redirect(url_for("index"))
        time.sleep(0.5)  # frena intentos masivos
        error = "Correo o contraseña incorrectos."
    return render_template("auth.html", modo="login", error=error, registro_abierto=REGISTRO_ABIERTO)


@app.route("/registro", methods=["GET", "POST"])
def registro():
    hay_usuarios = Usuario.query.first() is not None
    if not REGISTRO_ABIERTO and hay_usuarios:
        return redirect(url_for("login"))
    error = None
    if request.method == "POST":
        nombre = request.form.get("nombre", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not nombre or "@" not in email:
            error = "Escribe tu nombre y un correo válido."
        elif len(password) < 8:
            error = "La contraseña debe tener al menos 8 caracteres."
        elif Usuario.query.filter_by(email=email).first():
            error = "Ya existe una cuenta con ese correo."
        else:
            u = Usuario(nombre=nombre[:80], email=email, es_admin=not hay_usuarios)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            if u.es_admin:
                importar_historial_antiguo(u)
            session.permanent = True
            session["uid"] = u.id
            return redirect(url_for("index"))
    return render_template("auth.html", modo="registro", error=error, registro_abierto=True)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- páginas
@app.route("/")
@login_requerido
def index():
    u = usuario_actual()
    return render_template("index.html", usuario=u, apps_locales=APPS_LOCALES and u.es_admin)


@app.get("/api/imagenes/<path:nombre>")
@login_requerido
def ver_imagen_generada(nombre):
    descargar = request.args.get("descargar") == "1"
    return send_from_directory(GENERATED_DIR, nombre, as_attachment=descargar)


# ---------------------------------------------------------------- conversaciones
def conversacion_del_usuario(cid):
    c = db.session.get(Conversacion, cid)
    if not c or c.usuario_id != session.get("uid"):
        abort(404)
    return c


@app.get("/api/conversaciones")
@login_requerido
def listar_conversaciones():
    convs = (Conversacion.query.filter_by(usuario_id=session["uid"])
             .order_by(Conversacion.actualizado.desc()).limit(200).all())
    return jsonify([c.to_dict() for c in convs])


@app.get("/api/conversaciones/<int:cid>")
@login_requerido
def ver_conversacion(cid):
    c = conversacion_del_usuario(cid)
    return jsonify({**c.to_dict(), "mensajes": [m.to_dict() for m in c.mensajes]})


@app.patch("/api/conversaciones/<int:cid>")
@login_requerido
def renombrar_conversacion(cid):
    c = conversacion_del_usuario(cid)
    titulo = (request.json or {}).get("titulo", "").strip()
    if titulo:
        c.titulo = titulo[:120]
        db.session.commit()
    return jsonify(c.to_dict())


@app.delete("/api/conversaciones/<int:cid>")
@login_requerido
def borrar_conversacion(cid):
    db.session.delete(conversacion_del_usuario(cid))
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- memoria
@app.get("/api/memoria")
@login_requerido
def listar_memoria():
    rs = Recuerdo.query.filter_by(usuario_id=session["uid"]).order_by(Recuerdo.id).all()
    return jsonify([r.to_dict() for r in rs])


@app.post("/api/memoria")
@login_requerido
def agregar_memoria():
    texto = (request.json or {}).get("contenido", "").strip()
    if not texto:
        return jsonify({"error": "Vacío"}), 400
    r = Recuerdo(usuario_id=session["uid"], contenido=texto[:500])
    db.session.add(r)
    db.session.commit()
    return jsonify(r.to_dict())


@app.delete("/api/memoria/<int:rid>")
@login_requerido
def borrar_memoria(rid):
    r = db.session.get(Recuerdo, rid)
    if not r or r.usuario_id != session["uid"]:
        abort(404)
    db.session.delete(r)
    db.session.commit()
    return jsonify({"ok": True})


@app.delete("/api/memoria")
@login_requerido
def borrar_toda_memoria():
    Recuerdo.query.filter_by(usuario_id=session["uid"]).delete()
    db.session.commit()
    return jsonify({"ok": True})


def instrucciones_sistema(u):
    recuerdos = Recuerdo.query.filter_by(usuario_id=u.id).order_by(Recuerdo.id).all()
    texto = (
        f"Eres JLG AI, un asistente personal útil, claro y cercano. Hablas con {u.nombre}. "
        "Responde en el idioma del usuario (normalmente español), usa Markdown cuando ayude "
        "(listas, tablas, bloques de código) y sé directo. "
        "No saludes ni repitas el nombre del usuario en cada respuesta: solo al inicio de una conversación. "
        "Responde a lo que pregunta sin añadir preguntas de cierre forzadas. "
        f"Fecha actual: {ahora().strftime('%Y-%m-%d')}."
    )
    if recuerdos:
        texto += "\n\nLo que recuerdas de este usuario (úsalo solo cuando sea útil para la pregunta; no lo menciones si no viene al caso):\n"
        texto += "\n".join(f"- {r.contenido}" for r in recuerdos)
    return texto


def extraer_recuerdos(uid, mensaje_usuario, respuesta):
    """Corre en segundo plano: pide a Gemini datos duraderos del usuario y los guarda."""
    with app.app_context():
        try:
            existentes = [r.contenido for r in Recuerdo.query.filter_by(usuario_id=uid).all()]
            if len(existentes) >= MAX_RECUERDOS:
                return
            prompt = (
                "Analiza este intercambio y extrae SOLO datos personales duraderos y útiles del usuario "
                "(nombre, trabajo, proyectos, metas, gustos, preferencias de cómo quiere que le respondan, "
                "personas importantes, ubicación...). Si el usuario pide explícitamente que recuerdes algo, "
                "inclúyelo. No incluyas preguntas puntuales ni datos que ya estén en la lista. "
                "No guardes contraseñas, números de tarjeta ni datos sensibles.\n\n"
                f"Ya recordados:\n{json.dumps(existentes, ensure_ascii=False)}\n\n"
                f"Usuario: {mensaje_usuario[:3000]}\nAsistente: {respuesta[:1500]}\n\n"
                'Responde JSON: {"nuevos": ["frase corta en tercera persona", ...]} (lista vacía si no hay nada).'
            )
            r = generar_simple(prompt, types.GenerateContentConfig(
                response_mime_type="application/json", temperature=0), primero=MODELO_MEMORIA)
            nuevos = json.loads(r.text or "{}").get("nuevos", [])
            for n in nuevos[:5]:
                if isinstance(n, str) and n.strip() and n.strip() not in existentes:
                    db.session.add(Recuerdo(usuario_id=uid, contenido=n.strip()[:500]))
            db.session.commit()
        except Exception as e:  # la memoria nunca debe romper el chat
            print(f"⚠️ No se pudo actualizar la memoria: {str(e)[:150]}", flush=True)


# ---------------------------------------------------------------- generador de imágenes
def sin_tildes(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def detectar_solicitud_imagen(texto):
    """Detecta si el usuario pide generar o dibujar una imagen y devuelve la descripción."""
    if not texto:
        return None
    original = texto.strip()
    limpio = re.sub(r"[.!?¡¿,;]+$", "", sin_tildes(original.lower())).strip()

    # Caso 1: "genera una imagen de ...", "crea un logo para ...", "hazme un wallpaper de ..."
    m1 = re.match(
        r"^(?:por favor\s+)?(?:puedes\s+|podrias\s+)?"
        r"(?:genera|generar|generame|crea|crear|creame|haz|hacer|hazme|disena|disenar|disename)\s+"
        r"(?:una?\s+|el\s+|la\s+)?"
        r"(imagen|foto|fotografia|ilustracion|dibujo|logo|logotipo|icono|avatar|wallpaper|fondo\s+de\s+pantalla|retrato|poster|banner|portada)\s+"
        r"(?:de\s+|sobre\s+|con\s+|para\s+)?(.+)$",
        limpio,
    )
    if m1:
        tipo = m1.group(1).strip()
        desc = re.sub(
            r"^(?:por favor\s+)?(?:puedes\s+|podrías\s+|podrias\s+)?"
            r"(?:genera|generar|genérame|generame|crea|crear|créame|creame|haz|hacer|hazme|diseña|disena|diseñar|disenar|diseñame|disename)\s+"
            r"(?:una?\s+|el\s+|la\s+)?"
            r"(?:imagen|foto|fotografía|fotografia|ilustración|ilustracion|dibujo|logo|logotipo|ícono|icono|avatar|wallpaper|fondo\s+de\s+pantalla|retrato|póster|poster|banner|portada)\s+"
            r"(?:de\s+|sobre\s+|con\s+|para\s+)?",
            "",
            original,
            flags=re.IGNORECASE,
        ).strip()
        if tipo in ("logo", "logotipo", "icono", "avatar", "wallpaper", "fondo de pantalla", "poster", "banner", "portada"):
            return f"{tipo} de {desc}"
        return desc

    # Caso 2: "dibuja un ...", "dibújame una ...", "ilustra ..."
    m2 = re.match(
        r"^(?:por favor\s+)?(?:puedes\s+|podrias\s+)?"
        r"(?:dibuja|dibujar|dibujame|ilustra|ilustrar|ilustrame)\s+"
        r"(?:una?\s+|el\s+|la\s+)?(.+)$",
        limpio,
    )
    if m2:
        desc = re.sub(
            r"^(?:por favor\s+)?(?:puedes\s+|podrías\s+|podrias\s+)?"
            r"(?:dibuja|dibujar|dibújame|dibujame|ilustra|ilustrar|ilústrame|ilustrame)\s+",
            "",
            original,
            flags=re.IGNORECASE,
        ).strip()
        return desc

    return None


def generar_imagen_ia(prompt):
    """Genera una imagen con Gemini/Imagen y devuelve (nombre_archivo, error)."""
    errores = []
    modelos_img = []
    for m in [MODELO_IMAGEN, "imagen-3.0-generate-002", "imagen-3.0-fast-generate-001"]:
        if m and m not in modelos_img:
            modelos_img.append(m)

    for m in modelos_img:
        try:
            res = client.models.generate_images(
                model=m,
                prompt=prompt,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    output_mime_type="image/png",
                ),
            )
            if getattr(res, "generated_images", None):
                raw_data = res.generated_images[0].image.image_bytes
                if isinstance(raw_data, str):
                    raw_data = base64.b64decode(raw_data)
                if raw_data:
                    nombre = f"img_{int(time.time())}_{secrets.token_hex(4)}.png"
                    with open(os.path.join(GENERATED_DIR, nombre), "wb") as f:
                        f.write(raw_data)
                    return nombre, None
        except Exception as e:
            errores.append(f"{m}: {str(e)[:140]}")

    for m in ["gemini-2.5-flash-image", "gemini-2.0-flash-exp-image-generation"]:
        try:
            res = client.models.generate_content(
                model=m,
                contents=f"Genera una imagen de alta calidad: {prompt}",
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE", "TEXT"],
                ),
            )
            for cand in (getattr(res, "candidates", None) or []):
                for part in (getattr(cand.content, "parts", None) or []):
                    inline = getattr(part, "inline_data", None)
                    if inline and getattr(inline, "data", None):
                        raw_data = inline.data
                        if isinstance(raw_data, str):
                            raw_data = base64.b64decode(raw_data)
                        mime = getattr(inline, "mime_type", "") or "image/png"
                        ext = "jpg" if "jpeg" in mime or "jpg" in mime else "png"
                        nombre = f"img_{int(time.time())}_{secrets.token_hex(4)}.{ext}"
                        with open(os.path.join(GENERATED_DIR, nombre), "wb") as f:
                            f.write(raw_data)
                        return nombre, None
        except Exception as e:
            errores.append(f"{m}: {str(e)[:140]}")

    return None, (errores[0] if errores else "No se pudo generar la imagen.")


# ---------------------------------------------------------------- comandos, búsquedas y control del sistema
HOME = os.path.expanduser("~")
WEBS = {
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "instagram": "https://www.instagram.com",
    "facebook": "https://www.facebook.com",
    "gmail": "https://mail.google.com",
    "correo": "https://mail.google.com",
    "drive": "https://drive.google.com",
    "google drive": "https://drive.google.com",
    "chatgpt": "https://chatgpt.com",
    "gemini": "https://gemini.google.com",
    "github": "https://github.com",
    "tiktok": "https://www.tiktok.com",
    "twitch": "https://www.twitch.tv",
    "netflix": "https://www.netflix.com",
    "spotify": "https://open.spotify.com",
    "whatsapp": "https://web.whatsapp.com",
    "whatsapp web": "https://web.whatsapp.com",
    "canva": "https://www.canva.com",
    "render": "https://dashboard.render.com",
    "supabase": "https://supabase.com/dashboard",
    "traductor": "https://translate.google.com",
    "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com",
    "discord": "https://discord.com/app",
    "roblox": "https://www.roblox.com",
}

BUSQUEDAS = {
    "youtube": "https://www.youtube.com/results?search_query={q}",
    "google": "https://www.google.com/search?q={q}",
    "spotify": "https://open.spotify.com/search/{q}",
    "tiktok": "https://www.tiktok.com/search?q={q}",
    "github": "https://github.com/search?q={q}",
    "maps": "https://www.google.com/maps/search/{q}",
    "google maps": "https://www.google.com/maps/search/{q}",
    "traductor": "https://translate.google.com/?sl=auto&tl=es&text={q}",
}

PROGRAMAS = {
    "calculadora": {"Windows": "calc", "Darwin": [("-b", "com.apple.calculator")],
                    "Linux": [["gnome-calculator"], ["kcalc"], ["galculator"], ["mate-calc"], ["xcalc"]]},
    "bloc de notas": {"Windows": "notepad", "Darwin": [("-b", "com.apple.TextEdit")],
                      "Linux": [["gnome-text-editor"], ["gedit"], ["kate"], ["mousepad"], ["xed"], ["pluma"]]},
    "paint": {"Windows": "mspaint", "Darwin": [("-b", "com.apple.Preview")],
              "Linux": [["kolourpaint"], ["pinta"], ["gimp"]]},
    "navegador": {"Windows": "start chrome",
                  "Darwin": [("-b", "com.google.Chrome"), ("-b", "com.apple.Safari")],
                  "Linux": [["google-chrome"], ["chromium"], ["chromium-browser"], ["firefox"],
                            ["xdg-open", "https://www.google.com"]]},
    "brave": {"Windows": "start brave",
              "Darwin": [("-b", "com.brave.Browser")],
              "Linux": [["brave-browser"], ["brave"]]},
    "whatsapp": {"Windows": "start https://web.whatsapp.com",
                 "Darwin": [("-b", "net.whatsapp.WhatsApp"), ("url", "https://web.whatsapp.com")],
                 "Linux": [["xdg-open", "https://web.whatsapp.com"]]},
    "spotify": {"Windows": "start spotify:", "Darwin": [("-b", "com.spotify.client"), ("url", "https://open.spotify.com")],
                "Linux": [["spotify"], ["flatpak", "run", "com.spotify.Client"], ["xdg-open", "https://open.spotify.com"]]},
    "steam": {"Windows": "start steam:", "Darwin": [("-b", "com.valvesoftware.steam")],
              "Linux": [["steam"], ["flatpak", "run", "com.valvesoftware.Steam"]]},
    "roblox": {"Windows": "start roblox:", "Darwin": [("-b", "com.roblox.RobloxPlayer"), ("url", "https://www.roblox.com")],
               "Linux": [["xdg-open", "https://www.roblox.com"]]},
    "discord": {"Windows": "start discord:", "Darwin": [("-b", "com.hnc.Discord")],
                "Linux": [["discord"], ["flatpak", "run", "com.discordapp.Discord"]]},
    "codigo": {"Windows": "code", "Darwin": [("-b", "com.microsoft.VSCode")],
               "Linux": [["code"], ["codium"], ["flatpak", "run", "com.visualstudio.code"]]},
    "terminal": {"Windows": "start cmd", "Darwin": [("-b", "com.apple.Terminal")],
                 "Linux": [["x-terminal-emulator"], ["gnome-terminal"], ["konsole"], ["xfce4-terminal"], ["xterm"]]},
    "archivos": {"Windows": "explorer", "Darwin": [("path", HOME)],
                 "Linux": [["xdg-open", HOME], ["nautilus"], ["dolphin"], ["thunar"]]},
    "word": {"Windows": "start winword", "Darwin": [("-b", "com.microsoft.Word")],
             "Linux": [["libreoffice", "--writer"]]},
    "excel": {"Windows": "start excel", "Darwin": [("-b", "com.microsoft.Excel")],
              "Linux": [["libreoffice", "--calc"]]},
    "powerpoint": {"Windows": "start powerpnt", "Darwin": [("-b", "com.microsoft.Powerpoint")],
                   "Linux": [["libreoffice", "--impress"]]},
    "configuracion": {"Windows": "start ms-settings:", "Darwin": [("-b", "com.apple.systempreferences")],
                      "Linux": [["gnome-control-center"]]},
}

PROCESOS_CERRAR = {
    "calculadora": {"Windows": ["CalculatorApp.exe", "calc.exe"], "Linux": ["gnome-calculator", "kcalc", "galculator"], "Darwin": ["Calculator"]},
    "bloc de notas": {"Windows": ["notepad.exe"], "Linux": ["gnome-text-editor", "gedit", "kate", "mousepad"], "Darwin": ["TextEdit"]},
    "paint": {"Windows": ["mspaint.exe"], "Linux": ["kolourpaint", "pinta", "gimp"], "Darwin": ["Preview"]},
    "navegador": {"Windows": ["chrome.exe"], "Linux": ["chrome", "chromium", "firefox"], "Darwin": ["Google Chrome", "Safari"]},
    "brave": {"Windows": ["brave.exe"], "Linux": ["brave"], "Darwin": ["Brave Browser"]},
    "spotify": {"Windows": ["Spotify.exe"], "Linux": ["spotify"], "Darwin": ["Spotify"]},
    "steam": {"Windows": ["steam.exe"], "Linux": ["steam"], "Darwin": ["Steam"]},
    "roblox": {"Windows": ["RobloxPlayerBeta.exe"], "Linux": [], "Darwin": ["RobloxPlayer"]},
    "discord": {"Windows": ["Discord.exe"], "Linux": ["Discord"], "Darwin": ["Discord"]},
    "codigo": {"Windows": ["Code.exe"], "Linux": ["code", "codium"], "Darwin": ["Visual Studio Code"]},
    "word": {"Windows": ["WINWORD.EXE"], "Linux": ["soffice.bin"], "Darwin": ["Microsoft Word"]},
    "excel": {"Windows": ["EXCEL.EXE"], "Linux": ["soffice.bin"], "Darwin": ["Microsoft Excel"]},
    "powerpoint": {"Windows": ["POWERPNT.EXE"], "Linux": ["soffice.bin"], "Darwin": ["Microsoft PowerPoint"]},
}

ALIAS = {
    "vscode": "codigo", "vs code": "codigo", "visual studio code": "codigo", "visual studio": "codigo",
    "editor de codigo": "codigo", "chrome": "navegador", "explorador de archivos": "archivos",
    "explorador": "archivos", "finder": "archivos", "bloc": "bloc de notas", "notas": "bloc de notas",
    "notepad": "bloc de notas", "consola": "terminal", "cmd": "terminal", "ajustes": "configuracion"
}


def lanzar(opciones):
    """Prueba las alternativas del sistema actual. Devuelve True si alguna arrancó."""
    if SISTEMA == "Windows":
        subprocess.Popen(opciones, shell=True)
        return True
    for op in opciones:
        if SISTEMA == "Darwin":
            tipo, valor = op
            if tipo == "url":
                subprocess.Popen(["open", valor])
                return True
            cmd = ["open", valor] if tipo == "path" else ["open", "-b", valor]
            if subprocess.run(cmd, capture_output=True, timeout=15).returncode == 0:
                return True
        elif shutil.which(op[0]):
            subprocess.Popen(op, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return True
    return False


def cerrar_programa(objetivo_alias):
    """Cierra un programa abierto en el equipo local."""
    objetivos = PROCESOS_CERRAR.get(objetivo_alias, {}).get(SISTEMA, [])
    if not objetivos:
        return False
    cerrado = False
    for proc in objetivos:
        if SISTEMA == "Windows":
            r = subprocess.run(["taskkill", "/F", "/IM", proc], capture_output=True)
            if r.returncode == 0:
                cerrado = True
        elif SISTEMA == "Darwin":
            r = subprocess.run(["osascript", "-e", f'quit app "{proc}"'], capture_output=True)
            if r.returncode == 0:
                cerrado = True
        else:
            r = subprocess.run(["pkill", "-f", proc], capture_output=True)
            if r.returncode == 0:
                cerrado = True
    return cerrado


def ejecutar_control_multimedia(accion):
    """Envía teclas multimedia o de volumen al sistema local."""
    if SISTEMA == "Windows":
        codigos = {
            "vol_up": "175,175,175,175,175",
            "vol_down": "174,174,174,174,174",
            "mute": "173",
            "play_pause": "179",
            "next": "176",
            "prev": "177",
        }
        seq = codigos.get(accion)
        if not seq:
            return False
        ps = f"$ws = New-Object -ComObject WScript.Shell; foreach ($c in @({seq})) {{ $ws.SendKeys([char]$c) }}"
        return subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True).returncode == 0

    if SISTEMA == "Linux":
        comandos = {
            "vol_up": ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"],
            "vol_down": ["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"],
            "mute": ["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
            "play_pause": ["playerctl", "play-pause"],
            "next": ["playerctl", "next"],
            "prev": ["playerctl", "previous"],
        }
        cmd = comandos.get(accion)
        if cmd and shutil.which(cmd[0]):
            return subprocess.run(cmd, capture_output=True).returncode == 0
        return False

    if SISTEMA == "Darwin":
        scripts = {
            "vol_up": "set volume output volume ((output volume of (get volume settings)) + 10)",
            "vol_down": "set volume output volume ((output volume of (get volume settings)) - 10)",
            "mute": "set volume output muted not (output muted of (get volume settings))",
            "play_pause": 'tell application "System Events" to key code 16',
            "next": 'tell application "System Events" to key code 17',
            "prev": 'tell application "System Events" to key code 18',
        }
        sc = scripts.get(accion)
        if sc:
            return subprocess.run(["osascript", "-e", sc], capture_output=True).returncode == 0
    return False


def intentar_abrir_app(u, mensaje):
    """Detecta comandos de búsqueda, multimedia, abrir/cerrar apps y webs. Devuelve (texto, url)."""
    if not mensaje:
        return None, None
    texto_original = mensaje.strip()
    m_limpio = re.sub(r"[.!?¡¿,;]+$", "", sin_tildes(texto_original.lower())).strip()

    # 1. Búsquedas directas en plataformas ("busca en youtube ...", "pon ... en spotify", "googlea ...")
    m_busqueda = re.match(
        r"^(?:por favor\s+)?(?:puedes\s+|podrias\s+)?"
        r"(?:busca|buscar|encuentra|encontrar)\s+en\s+"
        r"(youtube|google\s+maps|google|spotify|tiktok|github|maps|traductor)\s+(.+)$",
        m_limpio,
    )
    if m_busqueda:
        plataforma = m_busqueda.group(1).strip()
        consulta = re.sub(
            r"^(?:por favor\s+)?(?:puedes\s+|podrías\s+|podrias\s+)?"
            r"(?:busca|buscar|encuentra|encontrar)\s+en\s+\S+(?:\s+maps)?\s+",
            "",
            texto_original,
            flags=re.IGNORECASE,
        ).strip()
        url = BUSQUEDAS[plataforma].format(q=quote(consulta))
        return f"🔎 Buscando **{consulta}** en **{plataforma.title()}**...", url

    m_pon = re.match(
        r"^(?:por favor\s+)?(?:puedes\s+|podrias\s+)?(?:pon|poner|reproduce|reproducir)\s+(.+?)\s+en\s+(youtube|spotify)$",
        m_limpio,
    )
    if m_pon:
        plataforma = m_pon.group(2).strip()
        consulta = re.sub(
            r"^(?:por favor\s+)?(?:puedes\s+|podrías\s+|podrias\s+)?(?:pon|poner|reproduce|reproducir)\s+",
            "",
            texto_original,
            flags=re.IGNORECASE,
        )
        consulta = re.sub(r"\s+en\s+(?:youtube|spotify)[.!?¡¿,;]*$", "", consulta, flags=re.IGNORECASE).strip()
        url = BUSQUEDAS[plataforma].format(q=quote(consulta))
        return f"🎵 Buscando **{consulta}** en **{plataforma.title()}**...", url

    m_googlea = re.match(r"^(?:por favor\s+)?(?:puedes\s+|podrias\s+)?googlea(?:r)?\s+(.+)$", m_limpio)
    if m_googlea:
        consulta = re.sub(
            r"^(?:por favor\s+)?(?:puedes\s+|podrías\s+|podrias\s+)?googlea(?:r)?\s+",
            "",
            texto_original,
            flags=re.IGNORECASE,
        ).strip()
        url = BUSQUEDAS["google"].format(q=quote(consulta))
        return f"🔎 Buscando **{consulta}** en **Google**...", url

    # 2. Control multimedia y volumen (solo en modo local)
    controles = [
        (r"^(?:por favor\s+)?(?:sube|subir|aumenta|aumentar)\s+(?:el\s+)?volumen$", "vol_up", "🔊 Subiendo el volumen..."),
        (r"^(?:por favor\s+)?(?:baja|bajar|disminuye|disminuir)\s+(?:el\s+)?volumen$", "vol_down", "🔉 Bajando el volumen..."),
        (r"^(?:por favor\s+)?(?:silencia|silenciar|mute|mutea|quita\s+el\s+sonido|activa\s+el\s+sonido)$", "mute", "🔇 Alternando silencio del sistema..."),
        (r"^(?:por favor\s+)?(?:pon\s+pausa|pausa|pausar|reanuda|reanudar|reproduce|play)(?:\s+la\s+musica)?$", "play_pause", "⏯️ Reproducción alternada."),
        (r"^(?:por favor\s+)?(?:siguiente\s+cancion|siguiente\s+pista|pasa\s+de\s+cancion|cambia\s+de\s+cancion)$", "next", "⏭️ Siguiente pista."),
        (r"^(?:por favor\s+)?(?:cancion\s+anterior|pista\s+anterior|regresa\s+la\s+cancion)$", "prev", "⏮️ Pista anterior."),
    ]
    for patron, accion, msg_ok in controles:
        if re.match(patron, m_limpio):
            if APPS_LOCALES and u.es_admin:
                if ejecutar_control_multimedia(accion):
                    return msg_ok, None
                return "⚠️ No pude ejecutar ese control multimedia en este equipo.", None
            return "⚠️ Los controles de volumen y reproducción requieren ejecutar JLG AI en tu PC con `ENABLE_LOCAL_APPS=1`.", None

    # 3. Cerrar aplicaciones de escritorio ("cierra calculadora", "cierra spotify")
    m_cerrar = re.match(
        r"^(?:por favor\s+)?(?:puedes\s+|podrias\s+)?(?:cierra|cerrar|termina|finaliza)\s+(?:la\s+|el\s+|app\s+|aplicacion\s+)*(.+)$",
        m_limpio,
    )
    if m_cerrar:
        obj = m_cerrar.group(1).strip()
        obj_alias = ALIAS.get(obj, obj)
        if obj_alias in PROCESOS_CERRAR:
            if APPS_LOCALES and u.es_admin:
                if cerrar_programa(obj_alias):
                    return f"🛑 Cerrando **{obj_alias}**...", None
                return f"ℹ️ **{obj_alias}** no estaba abierto en este momento.", None
            return f"⚠️ Para cerrar **{obj_alias}**, ejecuta JLG AI en tu PC con `ENABLE_LOCAL_APPS=1`.", None

    # 4. Abrir aplicaciones de escritorio y páginas web
    coincidencia = re.match(
        r"^(?:por favor\s+)?(?:puedes\s+|podrias\s+)?"
        r"(?:abre|abrir|inicia|iniciar|lanza|lanzar|ejecuta|ejecutar)\s+"
        r"(?:la\s+|el\s+|las\s+|los\s+|app\s+|aplicacion\s+|pagina\s+|web\s+|sitio\s+)*(.+)$",
        m_limpio,
    )
    if not coincidencia:
        return None, None

    objetivo = coincidencia.group(1).strip()
    objetivo_alias = ALIAS.get(objetivo, objetivo)

    if objetivo_alias in PROGRAMAS:
        if APPS_LOCALES and u.es_admin:
            try:
                if lanzar(PROGRAMAS[objetivo_alias][SISTEMA]):
                    return f"🚀 Abriendo **{objetivo_alias}** en tu equipo...", None
                return f"⚠️ No encontré **{objetivo_alias}** instalado en este equipo.", None
            except Exception as e:
                return f"⚠️ No pude abrir **{objetivo_alias}**: {e}", None
        if objetivo_alias in WEBS:
            return f"🌐 Abriendo **{objetivo_alias}** en una nueva pestaña...", WEBS[objetivo_alias]
        return (
            f"⚠️ **{objetivo_alias}** es un programa de escritorio. "
            "Para abrirlo, ejecuta JLG AI en tu PC con `ENABLE_LOCAL_APPS=1` en `.env`.",
            None,
        )

    url = WEBS.get(objetivo_alias) or WEBS.get(objetivo)
    if url:
        return f"🌐 Abriendo **{objetivo}** en una nueva pestaña...", url

    es_archivo = re.search(
        r"\.(?:py|js|html|css|txt|pdf|docx?|xlsx?|pptx?|png|jpe?g|gif|zip|rar|exe|bat|sh|db|json|csv|md)$",
        objetivo,
    )
    if not es_archivo and re.match(r"^(?:https?://)?[a-z0-9-]+(\.[a-z0-9-]+)+(/.*)?$", objetivo):
        url_directa = objetivo if objetivo.startswith(("http://", "https://")) else f"https://{objetivo}"
        return f"🌐 Abriendo **{url_directa}** en una nueva pestaña...", url_directa

    return None, None


# ---------------------------------------------------------------- chat
MODELOS_RESPALDO = [m.strip() for m in os.getenv(
    "GEMINI_FALLBACK_MODELS", "gemini-3.5-flash,gemini-3.6-flash,gemini-3.1-flash-lite,gemini-3.7-flash").split(",") if m.strip()]


def error_reintentable(e):
    s = str(e)
    if "timed out" in s.lower() or "timeout" in type(e).__name__.lower():
        return True
    return any(c in s for c in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED", "500", "INTERNAL", "504", "DEADLINE"))


def mensaje_error(e):
    s = str(e)
    if "429" in s or "RESOURCE_EXHAUSTED" in s:
        return "Se agotó la cuota gratuita de la API de Gemini. Espera un poco o revisa tu plan en Google AI Studio."
    if "503" in s or "UNAVAILABLE" in s or "timed out" in s.lower():
        return "Los servidores de Gemini están saturados ahora mismo. Inténtalo de nuevo en unos segundos."
    return f"Error al consultar Gemini: {s[:300]}"


PAUSA_MODELO_S = 180
_modelos_caidos = {}


def marcar_caido(modelo):
    _modelos_caidos[modelo] = time.time() + PAUSA_MODELO_S


def orden_modelos(primero=None):
    todos = []
    pro = [MODELO_PRO] if primero == MODELO_PRO else []
    for m in ([primero] if primero else []) + [MODELO] + MODELOS_RESPALDO + pro:
        if m and m not in todos:
            todos.append(m)
    ahora_ts = time.time()
    sanos = [m for m in todos if _modelos_caidos.get(m, 0) < ahora_ts]
    return sanos or todos


def intentos_gemini(config, primero=None):
    """Orden de intentos: modelos sanos primero; la búsqueda de Google solo en el primero."""
    sin_busqueda = config.model_copy(update={"tools": None})
    for i, m in enumerate(orden_modelos(primero)):
        yield m, (config if i == 0 else sin_busqueda)


def generar_simple(contenidos, config=None, primero=None):
    """Llamada sin streaming con los mismos modelos de respaldo."""
    error = None
    for m in orden_modelos(primero):
        try:
            return client.models.generate_content(model=m, contents=contenidos, config=config)
        except Exception as e:
            error = e
            if not error_reintentable(e):
                break
            marcar_caido(m)
    raise error


def historial_gemini(conv):
    previos = conv.mensajes[-MAX_HISTORIAL:]
    return [types.Content(role=m.rol, parts=[types.Part.from_text(text=m.contenido)])
            for m in previos if m.contenido]


# ---------------------------------------------------------------- búsqueda en internet
def decidir_busqueda(texto, contexto):
    """Un modelo rápido decide si la pregunta necesita información actual de internet."""
    hoy = ahora().strftime("%Y-%m-%d")
    prompt = (
        f"Hoy es {hoy}. Tu conocimiento interno puede estar desactualizado (llega hasta 2025 aprox.).\n"
        "Decide si para responder bien al ÚLTIMO mensaje del usuario hace falta buscar en internet.\n"
        "Busca SÍ cuando pregunte por: noticias, sucesos recientes, resultados deportivos, quién ganó algo, "
        "eventos de 2025 en adelante, personas en cargos actuales, precios, clima, lanzamientos, estrenos, "
        "datos que cambian con el tiempo, o cuando pida explícitamente buscar.\n"
        "Busca NO para saludos, conversación, opiniones, ideas creativas, explicaciones generales, "
        "programación, matemáticas, o abrir aplicaciones.\n\n"
        f"Conversación previa:\n{contexto or '(ninguna)'}\n\n"
        f"Último mensaje: {texto[:1500]}\n\n"
        'Responde JSON: {"buscar": true/false, "consulta": "búsqueda corta y precisa en el idioma más útil"}'
    )
    try:
        r = generar_simple(prompt, types.GenerateContentConfig(
            response_mime_type="application/json", temperature=0), primero=MODELO_MEMORIA)
        d = json.loads(r.text or "{}")
        if d.get("buscar") and str(d.get("consulta", "")).strip():
            return str(d["consulta"]).strip()[:200]
    except Exception as e:
        print(f"⚠️ No se pudo decidir la búsqueda: {str(e)[:120]}", flush=True)
    return None


def buscar_web(consulta, maximo=8):
    """Busca en DuckDuckGo (web + noticias). Gratis, sin clave."""
    from ddgs import DDGS
    resultados, vistos = [], set()
    for tipo in ("news", "text"):
        try:
            with DDGS(timeout=8) as d:
                filas = (d.news(consulta, region="es-es", max_results=4) if tipo == "news"
                         else d.text(consulta, region="es-es", max_results=6))
        except Exception as e:
            print(f"⚠️ Búsqueda {tipo}: {str(e)[:100]}", flush=True)
            continue
        for f in filas or []:
            url = f.get("url") or f.get("href")
            if not url or url in vistos:
                continue
            vistos.add(url)
            resultados.append({"titulo": f.get("title", "").strip(), "url": url,
                               "resumen": (f.get("body") or f.get("excerpt") or "").strip()[:500],
                               "fecha": (f.get("date") or "")[:10]})
    return resultados[:maximo]


def texto_resultados(consulta, resultados):
    lineas = [f"[Resultados de búsqueda en internet, obtenidos hoy {ahora().strftime('%Y-%m-%d')}, "
              f"para «{consulta}». Úsalos como fuente principal: son más actuales que tu conocimiento. "
              "Si se contradicen, prioriza las fuentes más fiables y recientes. No inventes datos que no estén aquí.]"]
    for i, r in enumerate(resultados, 1):
        fecha = f" ({r['fecha']})" if r["fecha"] else ""
        lineas.append(f"{i}. {r['titulo']}{fecha} — {r['resumen']} [{r['url']}]")
    return "\n".join(lineas)


def fuentes_markdown(resultados):
    enlaces = []
    for r in resultados[:5]:
        dominio = urlparse(r["url"]).netloc.replace("www.", "")
        enlaces.append(f"[{dominio}]({r['url']})")
    return "\n\n---\n**Fuentes:** " + " · ".join(enlaces) if enlaces else ""


@app.post("/api/chat")
@login_requerido
def chat():
    u = usuario_actual()
    texto = request.form.get("message", "").strip()
    archivo = request.files.get("file")
    cid = request.form.get("conversacion_id", type=int)
    primero = MODELO_PRO if request.form.get("modo") == "avanzado" else MODELO

    if not texto and not archivo:
        return jsonify({"error": "No enviaste texto ni archivo."}), 400

    if cid:
        conv = conversacion_del_usuario(cid)
    else:
        titulo = (texto or (archivo.filename if archivo else "Archivo"))[:60]
        conv = Conversacion(usuario_id=u.id, titulo=titulo)
        db.session.add(conv)
        db.session.commit()

    contenidos = historial_gemini(conv)
    contexto_previo = "\n".join(
        f"{'Usuario' if m.rol == 'user' else 'Asistente'}: {m.contenido[:300]}" for m in conv.mensajes[-4:])
    partes = []
    nombre_adjunto = None
    if archivo:
        nombre_adjunto = archivo.filename or "imagen-pegada.png"
        partes.append(types.Part.from_bytes(data=archivo.read(), mime_type=archivo.mimetype or "image/png"))
    if texto:
        partes.append(types.Part.from_text(text=texto))
    else:
        partes.append(types.Part.from_text(text="Analiza el archivo adjunto."))
    contenidos.append(types.Content(role="user", parts=partes))

    db.session.add(Mensaje(conversacion=conv, rol="user", contenido=texto, adjunto=nombre_adjunto))
    conv.actualizado = ahora()
    db.session.commit()
    conv_id, uid = conv.id, u.id

    config = types.GenerateContentConfig(
        system_instruction=instrucciones_sistema(u),
        tools=[types.Tool(google_search=types.GoogleSearch())] if BUSQUEDA_GOOGLE else None,
    )
    respuesta_app, url_abrir = intentar_abrir_app(u, texto)
    prompt_imagen = None if (respuesta_app or archivo) else detectar_solicitud_imagen(texto)

    def generar():
        trozos = []
        if respuesta_app:
            trozos.append(respuesta_app)
            yield respuesta_app
        elif prompt_imagen:
            aviso_img = f"🎨 *Generando imagen: «{prompt_imagen}»...*\n\n"
            trozos.append(aviso_img)
            yield aviso_img
            nombre_img, err_img = generar_imagen_ia(prompt_imagen)
            if nombre_img:
                bloque_img = (
                    f"![{prompt_imagen}](/api/imagenes/{nombre_img})\n\n"
                    f"[⬇️ Descargar imagen](/api/imagenes/{nombre_img}?descargar=1)"
                )
                trozos.append(bloque_img)
                yield bloque_img
            else:
                aviso_err = f"⚠️ No se pudo generar la imagen con los modelos disponibles ({err_img})."
                trozos.append(aviso_err)
                yield aviso_err
        else:
            fuentes = ""
            if BUSQUEDA_WEB and texto:
                consulta = decidir_busqueda(texto, contexto_previo)
                if consulta:
                    aviso_busqueda = f"🔎 *Busqué en internet: «{consulta}»*\n\n"
                    trozos.append(aviso_busqueda)
                    yield aviso_busqueda
                    resultados = buscar_web(consulta)
                    if resultados:
                        contenidos[-1].parts.insert(0, types.Part.from_text(text=texto_resultados(consulta, resultados)))
                        fuentes = fuentes_markdown(resultados)
                    else:
                        contenidos[-1].parts.insert(0, types.Part.from_text(
                            text="[La búsqueda en internet no devolvió resultados. Dilo con honestidad si no sabes el dato actual.]"))
            ultimo_error = None
            for modelo, cfg in intentos_gemini(config, primero):
                try:
                    for chunk in client.models.generate_content_stream(
                            model=modelo, contents=contenidos, config=cfg):
                        if chunk.text:
                            trozos.append(chunk.text)
                            yield chunk.text
                    ultimo_error = None
                    break
                except Exception as e:
                    ultimo_error = e
                    if error_reintentable(e) and not trozos:
                        marcar_caido(modelo)
                    print(f"⚠️ {modelo}: {str(e)[:120]}", flush=True)
                    if trozos or not error_reintentable(e):
                        break
            if ultimo_error:
                aviso = "\n\n⚠️ " + mensaje_error(ultimo_error)
                trozos.append(aviso)
                yield aviso
            elif fuentes:
                trozos.append(fuentes)
                yield fuentes
        respuesta = "".join(trozos).strip() or "(sin respuesta)"
        db.session.add(Mensaje(conversacion_id=conv_id, rol="model", contenido=respuesta))
        db.session.commit()
        if texto and not respuesta_app and not prompt_imagen:
            threading.Thread(target=extraer_recuerdos, args=(uid, texto, respuesta), daemon=True).start()

    resp = Response(stream_with_context(generar()), mimetype="text/plain; charset=utf-8")
    resp.headers["X-Conversacion-Id"] = str(conv_id)
    if url_abrir:
        resp.headers["X-Abrir-Url"] = quote(url_abrir, safe=":/?#[]@!$&'()*+,;=%")
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.post("/api/transcribir")
@login_requerido
def transcribir():
    """Respaldo para navegadores sin reconocimiento de voz: Gemini transcribe el audio."""
    audio = request.files.get("audio")
    if not audio:
        return jsonify({"error": "Sin audio"}), 400
    try:
        r = generar_simple(
            [types.Part.from_bytes(data=audio.read(), mime_type=audio.mimetype or "audio/webm"),
             "Transcribe exactamente lo que dice este audio, sin comentarios ni comillas."])
        return jsonify({"texto": (r.text or "").strip()})
    except Exception as e:
        return jsonify({"error": mensaje_error(e)}), 500


@app.errorhandler(413)
def demasiado_grande(_):
    return jsonify({"error": f"El archivo supera {MAX_ARCHIVO_MB} MB."}), 413


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=os.getenv("FLASK_DEBUG") == "1", threaded=True)