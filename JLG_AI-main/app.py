"""JLG AI — asistente completo con Gemini/DeepSeek/Grok, archivos, web, memoria y voz."""
import base64
import csv
import html
import io
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
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from docx import Document
from dotenv import load_dotenv
from flask import (Flask, Response, abort, jsonify, redirect, render_template, request,
                   send_from_directory, session, stream_with_context, url_for)
from fpdf import FPDF
from google import genai
from google.genai import types
import openpyxl
from openpyxl.styles import Font, PatternFill

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

from database import (Conversacion, Mensaje, Recuerdo, Usuario, ahora, db,
                      importar_historial_antiguo, init_db)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GENERATED_DIR = os.path.join(BASE_DIR, "generated_images")
FILES_DIR = os.path.join(BASE_DIR, "generated_files")
os.makedirs(GENERATED_DIR, exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)

load_dotenv(os.path.join(BASE_DIR, "api.env"))
load_dotenv(os.path.join(BASE_DIR, ".env"))

MODELO = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")          # modo Rápido
MODELO_PRO = os.getenv("GEMINI_MODEL_PRO", "gemini-3.8-flash")        # modo Avanzado
MODELO_IMAGEN = os.getenv("GEMINI_IMAGE_MODEL", "imagen-3.0-generate-002")
MODELO_DEEPSEEK = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
MODELO_GROK = os.getenv("GROK_MODEL", "grok-3-mini")

BUSQUEDA_GOOGLE = os.getenv("ENABLE_GOOGLE_SEARCH", "0") == "1"
BUSQUEDA_WEB = os.getenv("ENABLE_WEB_SEARCH", "1") == "1"
SISTEMA = platform.system()
APPS_LOCALES = os.getenv("ENABLE_LOCAL_APPS", "0") == "1" and SISTEMA in ("Windows", "Darwin", "Linux")
REGISTRO_ABIERTO = os.getenv("ALLOW_REGISTRATION", "1") == "1"
MODELO_MEMORIA = os.getenv("GEMINI_MEMORY_MODEL", "gemini-3.5-flash-lite")
MAX_HISTORIAL = 30
MAX_RECUERDOS = 100
MAX_ARCHIVO_MB = 15
MAX_CHARS_ADJUNTO = 32000

app = Flask(__name__)


def clave_secreta():
    if os.getenv("SECRET_KEY"):
        return os.getenv("SECRET_KEY")
    ruta = os.path.join(BASE_DIR, ".secret_key")
    if not os.path.exists(ruta):
        with open(ruta, "w") as f:
            f.write(secrets.token_hex(32))
    return open(ruta).read().strip()


app.config["SECRET_KEY"] = clave_secreta()
app.config["MAX_CONTENT_LENGTH"] = MAX_ARCHIVO_MB * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 30
init_db(app)

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY"),
    http_options=types.HttpOptions(timeout=int(os.getenv("GEMINI_TIMEOUT_S", "60")) * 1000),
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
        time.sleep(0.5)
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


# ---------------------------------------------------------------- páginas y descargas
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


@app.get("/api/archivos/<path:nombre>")
@login_requerido
def descargar_archivo_generado(nombre):
    return send_from_directory(FILES_DIR, nombre, as_attachment=True)


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


# ---------------------------------------------------------------- memoria y personalidades
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


ESTILOS_PERSONALIDAD = {
    "default": "",
    "directo": "Sé ultra conciso y ve directo al grano en pocas líneas o viñetas sin rodeos.",
    "tutor": "Actúa como un tutor paciente: explica paso a paso con ejemplos claros y verifica que se entienda.",
    "codigo": "Enfócate en ingeniería de software limpia: entrega código completo, optimizado y listo para producción.",
    "creativo": "Usa un tono expresivo, ingenioso y original con buenas metáforas e ideas fuera de lo común.",
}


def instrucciones_sistema(u, personalidad="default"):
    recuerdos = Recuerdo.query.filter_by(usuario_id=u.id).order_by(Recuerdo.id).all()
    extra_estilo = ESTILOS_PERSONALIDAD.get(personalidad, "")
    texto = (
        f"Eres JLG AI, un asistente personal útil, claro y cercano. Hablas con {u.nombre}. "
        "Responde en el idioma del usuario (normalmente español), usa Markdown cuando ayude "
        "(listas, tablas, bloques de código) y sé directo. "
        "No saludes ni repitas el nombre del usuario en cada respuesta: solo al inicio de una conversación. "
        "Responde a lo que pregunta sin añadir preguntas de cierre forzadas. "
        f"Fecha actual: {ahora().strftime('%Y-%m-%d')}."
    )
    if extra_estilo:
        texto += f"\n\nEstilo solicitado para esta respuesta: {extra_estilo}"
    if recuerdos:
        texto += "\n\nLo que recuerdas de este usuario (úsalo solo cuando sea útil para la pregunta; no lo menciones si no viene al caso):\n"
        texto += "\n".join(f"- {r.contenido}" for r in recuerdos)
    return texto


def extraer_recuerdos(uid, mensaje_usuario, respuesta):
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
        except Exception as e:
            print(f"⚠️ No se pudo actualizar la memoria: {str(e)[:150]}", flush=True)


def sin_tildes(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


# ---------------------------------------------------------------- lectura real de archivos adjuntos
EXTENSIONES_TEXTO = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".jsx", ".tsx",
    ".html", ".htm", ".css", ".scss", ".json", ".sql", ".cs", ".java",
    ".c", ".cpp", ".h", ".hpp", ".php", ".rb", ".go", ".rs", ".kt",
    ".swift", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".sh", ".bat", ".ps1", ".log", ".r",
}


def extraer_texto_archivo(datos, nombre, mimetype=""):
    """Extrae texto legible de Word, Excel, PDF, CSV y archivos de código/texto.

    Devuelve (texto_extraido, enviar_bytes_multimodal).
    """
    ext = os.path.splitext((nombre or "").lower())[1]
    mime = (mimetype or "").lower()

    if mime.startswith(("image/", "audio/", "video/")) or ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        return None, True

    texto = ""
    enviar_bytes = False

    try:
        if ext == ".docx" or "wordprocessingml" in mime:
            doc = Document(io.BytesIO(datos))
            bloques = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
            for idx_t, tabla in enumerate(doc.tables, 1):
                filas_t = []
                for fila in tabla.rows:
                    celdas = [c.text.strip().replace("\n", " ") for c in fila.cells]
                    if any(celdas):
                        filas_t.append(" | ".join(celdas))
                if filas_t:
                    bloques.append(f"[Tabla {idx_t}]\n" + "\n".join(filas_t))
            texto = "\n\n".join(bloques)

        elif ext in (".xlsx", ".xlsm") or "spreadsheetml" in mime:
            wb = openpyxl.load_workbook(io.BytesIO(datos), data_only=True, read_only=True)
            hojas_txt = []
            for hoja in wb.sheetnames[:10]:
                ws = wb[hoja]
                filas_h = []
                for fila in ws.iter_rows(values_only=True):
                    vals = [str(v).strip() if v is not None else "" for v in fila]
                    if any(vals):
                        filas_h.append(" | ".join(vals))
                if filas_h:
                    hojas_txt.append(f"=== Hoja: {hoja} ===\n" + "\n".join(filas_h))
            wb.close()
            texto = "\n\n".join(hojas_txt)

        elif ext in (".csv", ".tsv") or "csv" in mime:
            crudo = datos.decode("utf-8-sig", errors="replace")
            delimitador = "\t" if ext == ".tsv" else ","
            lector = csv.reader(io.StringIO(crudo), delimiter=delimitador)
            filas = [" | ".join(c.strip() for c in f) for f in lector if any(c.strip() for c in f)]
            texto = "\n".join(filas)

        elif ext == ".pdf" or mime == "application/pdf":
            enviar_bytes = True  # Gemini también ve gráficos/escaneos nativos
            if PdfReader is not None:
                reader = PdfReader(io.BytesIO(datos))
                paginas = []
                for i, page in enumerate(reader.pages[:60], 1):
                    t_pag = (page.extract_text() or "").strip()
                    if t_pag:
                        paginas.append(f"--- Página {i} ---\n{t_pag}")
                texto = "\n\n".join(paginas)

        elif ext in EXTENSIONES_TEXTO or mime.startswith("text/") or "json" in mime or "xml" in mime:
            try:
                texto = datos.decode("utf-8-sig")
            except UnicodeDecodeError:
                texto = datos.decode("latin-1", errors="replace")

        else:
            # Archivo desconocido: intentamos leerlo como texto si no tiene bytes nulos
            if b"\x00" not in datos[:2048]:
                texto = datos.decode("utf-8", errors="replace")
            else:
                enviar_bytes = True
    except Exception as e:
        texto = f"[Error al leer el archivo '{nombre}': {str(e)[:150]}]"

    texto = (texto or "").strip()
    if len(texto) > MAX_CHARS_ADJUNTO:
        texto = texto[:MAX_CHARS_ADJUNTO] + "\n\n[... contenido truncado por longitud ...]"

    if texto:
        bloque = f"[Contenido extraído del archivo adjunto «{nombre}»:\n\n{texto}\n]"
        return bloque, enviar_bytes

    if ext == ".pdf":
        return (
            f"[El archivo PDF «{nombre}» no contiene texto seleccionable (parece un documento escaneado o imagen).]",
            True,
        )

    return None, enviar_bytes


# ---------------------------------------------------------------- #4: lector de enlaces web y recordatorios
class _ExtractorTextoHTML(HTMLParser):
    ETIQUETAS_IGNORADAS = {"script", "style", "noscript", "svg", "head", "iframe", "nav", "footer"}

    def __init__(self):
        super().__init__()
        self._ignorar = 0
        self._en_titulo = False
        self.titulo = ""
        self.trozos = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in self.ETIQUETAS_IGNORADAS:
            self._ignorar += 1
        elif t == "title":
            self._en_titulo = True

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in self.ETIQUETAS_IGNORADAS and self._ignorar > 0:
            self._ignorar -= 1
        elif t == "title":
            self._en_titulo = False

    def handle_data(self, data):
        if self._ignorar:
            return
        txt = data.strip()
        if not txt:
            return
        if self._en_titulo and not self.titulo:
            self.titulo = txt
        else:
            self.trozos.append(txt)


def extraer_urls_mensaje(texto):
    if not texto:
        return []
    crudos = re.findall(r"https?://[^\s<>\"'()]+", texto)
    limpios = []
    for u in crudos:
        u = u.rstrip(".,;:!?)]}")
        if u and u not in limpios:
            limpios.append(u)
    return limpios[:2]


def leer_pagina_web(url, max_chars=12000):
    """Descarga y extrae el texto limpio de una página web."""
    try:
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
            },
        )
        with urlopen(req, timeout=9) as resp:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            raw = resp.read(700_000)
            charset = "utf-8"
            m_cs = re.search(r"charset=([a-zA-Z0-9_-]+)", ctype)
            if m_cs:
                charset = m_cs.group(1)
            contenido = raw.decode(charset, errors="replace")

        if "text/plain" in ctype or "application/json" in ctype:
            texto = re.sub(r"\s+", " ", contenido).strip()
            return url, texto[:max_chars], None

        parser = _ExtractorTextoHTML()
        parser.feed(contenido)
        titulo = html.unescape(parser.titulo).strip() or urlparse(url).netloc
        cuerpo = re.sub(r"\s+", " ", html.unescape(" ".join(parser.trozos))).strip()
        if not cuerpo:
            return titulo, "", "La página no devolvió texto legible (puede requerir JavaScript)."
        return titulo, cuerpo[:max_chars], None
    except Exception as e:
        return urlparse(url).netloc, "", str(e)[:140]


def detectar_recordatorio(texto):
    """Detecta recordatorios o temporizadores por tiempo relativo o reloj."""
    if not texto:
        return None, None
    original = texto.strip()
    limpio = re.sub(r"[.!?¡¿,;]+$", "", sin_tildes(original.lower())).strip()

    m_rel = re.match(
        r"^(?:por favor\s+)?(?:puedes\s+|podrias\s+)?"
        r"(?:recuerdame|recordarme|avisame|avisarme|pon\s+(?:un\s+|una\s+)?(?:temporizador|alarma|recordatorio)\s+(?:de|en))\s+"
        r"(?:en\s+)?(\d+)\s*(segundos?|segs?|s|minutos?|mins?|m|horas?|hrs?|h)\b(?:\s+(?:para|que|de)\s+)?(.*)$",
        limpio,
    )
    if m_rel:
        cant = int(m_rel.group(1))
        unidad = m_rel.group(2)
        if unidad.startswith("h"):
            segundos = cant * 3600
            etiq_tiempo = f"{cant} {'hora' if cant == 1 else 'horas'}"
        elif unidad.startswith("m"):
            segundos = cant * 60
            etiq_tiempo = f"{cant} {'minuto' if cant == 1 else 'minutos'}"
        else:
            segundos = cant
            etiq_tiempo = f"{cant} {'segundo' if cant == 1 else 'segundos'}"

        if segundos <= 0 or segundos > 86400 * 7:
            return None, None

        tarea = re.sub(
            r"^(?:por favor\s+)?(?:puedes\s+|podrías\s+|podrias\s+)?"
            r"(?:recuérdame|recuerdame|recordarme|avísame|avisame|avisarme|pon\s+(?:un\s+|una\s+)?(?:temporizador|alarma|recordatorio)\s+(?:de|en))\s+"
            r"(?:en\s+)?\d+\s*(?:segundos?|segs?|s|minutos?|mins?|m|horas?|hrs?|h)\b(?:\s+(?:para|que|de)\s+)?",
            "",
            original,
            flags=re.IGNORECASE,
        ).strip().rstrip(".!?") or "Tiempo cumplido"

        payload = {"segundos": segundos, "texto": tarea}
        return f"⏰ Listo, te recordaré **{tarea}** en **{etiq_tiempo}**.", payload

    m_hora = re.match(
        r"^(?:por favor\s+)?(?:puedes\s+|podrias\s+)?"
        r"(?:recuerdame|recordarme|avisame|avisarme|pon\s+(?:una\s+)?alarma)\s+"
        r"a\s+las\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\b(?:\s+(?:para|que|de)\s+)?(.*)$",
        limpio,
    )
    if m_hora:
        hh = int(m_hora.group(1))
        mm = int(m_hora.group(2) or 0)
        ampm = (m_hora.group(3) or "").replace(".", "")
        if ampm == "pm" and hh < 12:
            hh += 12
        elif ampm == "am" and hh == 12:
            hh = 0
        if hh > 23 or mm > 59:
            return None, None

        hora_fmt = f"{hh:02d}:{mm:02d}"
        tarea = re.sub(
            r"^(?:por favor\s+)?(?:puedes\s+|podrías\s+|podrias\s+)?"
            r"(?:recuérdame|recuerdame|recordarme|avísame|avisame|avisarme|pon\s+(?:una\s+)?alarma)\s+"
            r"a\s+las\s+\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?\b(?:\s+(?:para|que|de)\s+)?",
            "",
            original,
            flags=re.IGNORECASE,
        ).strip().rstrip(".!?") or f"Alarma de las {hora_fmt}"

        payload = {"hora_reloj": hora_fmt, "texto": tarea}
        return f"⏰ Alarma programada para las **{hora_fmt}**: **{tarea}**.", payload

    return None, None


# ---------------------------------------------------------------- #1: generador de Word, Excel y PDF
def limpiar_latin1(texto):
    """Normaliza caracteres Unicode para fuentes estándar de FPDF."""
    reemplazos = {
        "—": "-", "–": "-", "“": '"', "”": '"', "‘": "'", "’": "'",
        "•": "-", "…": "...", "→": "->", "←": "<-", "✓": "OK",
    }
    for k, v in reemplazos.items():
        texto = texto.replace(k, v)
    return texto.encode("latin-1", errors="ignore").decode("latin-1")


def detectar_solicitud_archivo(texto):
    """Detecta si el usuario pide crear un archivo Word (.docx), Excel (.xlsx) o PDF (.pdf)."""
    if not texto:
        return None, None
    original = texto.strip()
    limpio = re.sub(r"[.!?¡¿,;]+$", "", sin_tildes(original.lower())).strip()

    m = re.match(
        r"^(?:por favor\s+)?(?:puedes\s+|podrias\s+)?"
        r"(?:crea|crear|creame|genera|generar|generame|haz|has|hacer|hazme|hasme|redacta|redactar|redactame|arma|armar|armame|elabora|elaborar)\s+"
        r"(?:un\s+|una\s+|el\s+|la\s+)?"
        r"(?:documento\s+(?:de\s+|en\s+)?|archivo\s+(?:de\s+|en\s+)?|hoja\s+de\s+calculo\s+(?:de\s+|en\s+)?|plantilla\s+(?:de\s+|en\s+)?|tabla\s+(?:de\s+|en\s+)?|informe\s+(?:de\s+|en\s+)?)?"
        r"(word|docx|excel|xlsx|pdf)\b(?:\s+(?:de|sobre|con|para|acerca\s+de)\s+)?(.*)$",
        limpio,
    )
    if not m:
        return None, None

    fmt = m.group(1)
    tipo = "docx" if fmt in ("word", "docx") else ("xlsx" if fmt in ("excel", "xlsx") else "pdf")
    tema = re.sub(
        r"^(?:por favor\s+)?(?:puedes\s+|podrías\s+|podrias\s+)?"
        r"(?:crea|crear|créame|creame|genera|generar|genérame|generame|haz|has|hacer|hazme|hasme|redacta|redactar|redáctame|redactame|arma|armar|ármame|armame|elabora|elaborar)\s+"
        r"(?:un\s+|una\s+|el\s+|la\s+)?"
        r"(?:documento\s+(?:de\s+|en\s+)?|archivo\s+(?:de\s+|en\s+)?|hoja\s+de\s+cálculo\s+(?:de\s+|en\s+)?|hoja\s+de\s+calculo\s+(?:de\s+|en\s+)?|plantilla\s+(?:de\s+|en\s+)?|tabla\s+(?:de\s+|en\s+)?|informe\s+(?:de\s+|en\s+)?)?"
        r"(?:word|docx|excel|xlsx|pdf)\b(?:\s+(?:de|sobre|con|para|acerca\s+de)\s+)?",
        "",
        original,
        flags=re.IGNORECASE,
    ).strip() or "Documento generado"

    return tipo, tema


def investigar_para_documento(tema):
    """Busca en internet desde dos ángulos para nutrir documentos con datos y cifras reales."""
    if not BUSQUEDA_WEB:
        return "", []
    try:
        resultados = buscar_web(tema, maximo=7)
        extra = buscar_web(f"{tema} datos estadisticas analisis", maximo=5)
        vistos = {r["url"] for r in resultados}
        for r in extra:
            if r["url"] not in vistos:
                vistos.add(r["url"])
                resultados.append(r)
        resultados = resultados[:10]
        if not resultados:
            return "", []
        return texto_resultados(tema, resultados), resultados
    except Exception as e:
        print(f"⚠️ Error investigando para documento: {str(e)[:120]}", flush=True)
        return "", []


def generar_archivo_ia(tipo, tema, u, info_web="", fuentes=None):
    """Investiga y construye un archivo .docx, .xlsx o .pdf extenso y bien fundamentado."""
    try:
        if not info_web and fuentes is None and BUSQUEDA_WEB:
            info_web, fuentes = investigar_para_documento(tema)
        fuentes = fuentes or []

        bloque_web = (
            f"\n\nINFORMACIÓN INVESTIGADA EN INTERNET (úsala como fuente principal; incluye datos, cifras, fechas, nombres y hechos reales):\n{info_web}\n"
            if info_web else ""
        )

        if tipo == "xlsx":
            prompt = (
                f"Crea un libro de Excel completo, analítico y detallado sobre: {tema}.{bloque_web}\n"
                "Genera entre 2 y 4 hojas temáticas (por ejemplo: Resumen y KPIs, Datos Detallados, Comparativa/Proyección).\n"
                "Usa fórmulas reales de Excel en inglés (como =SUM(B2:B10), =AVERAGE(C2:C10), =B2*C2) en filas de totales o métricas.\n"
                'Responde SOLO JSON válido con este formato:\n'
                '{"nombre_archivo": "nombre_sin_espacios", "titulo": "Título legible", '
                '"hojas": [{"nombre": "Hoja 1", "columnas": ["Col1", "Col2"], '
                '"filas": [["Dato", 100], ["Total", "=SUM(B2:B2)"]]}]}'
            )
        else:
            prompt = (
                f"Redacta un informe de investigación exhaustivo, profundo y riguroso en español sobre: {tema}.{bloque_web}\n"
                "REGLAS DE PROFUNDIDAD:\n"
                "- NO hagas un resumen corto ni superficial.\n"
                "- Desarrolla entre 5 y 7 secciones bien estructuradas (por ejemplo: Resumen Ejecutivo, Contexto y Antecedentes, "
                "Análisis Técnico/Temático Detallado, Datos y Comparativa, Retos e Impacto, y Conclusiones y Recomendaciones).\n"
                "- En cada sección escribe entre 3 y 4 párrafos largos, informativos y bien argumentados (mínimo 4-5 líneas por párrafo).\n"
                "- Incluye viñetas con hallazgos clave y al menos 2 tablas comparativas o cuantitativas distribuidas en el documento.\n"
                'Responde SOLO JSON válido con este formato:\n'
                '{"nombre_archivo": "nombre_sin_espacios", "titulo": "Título principal", '
                '"subtitulo": "Subtítulo descriptivo", '
                '"secciones": [{"encabezado": "1. Sección", "parrafos": ["Párrafo extenso 1...", "Párrafo extenso 2..."], '
                '"vinetas": ["Hallazgo 1"], "tabla": [["Col 1", "Col 2"], ["Fila 1", "Valor 1"]]}]}'
            )

        r = generar_simple(
            prompt,
            types.GenerateContentConfig(
                system_instruction=instrucciones_sistema(u),
                response_mime_type="application/json",
                temperature=0.35,
            ),
            primero=MODELO_PRO,
        )
        data = json.loads(r.text or "{}")
        base_slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", sin_tildes(data.get("nombre_archivo") or "investigacion")).strip("_")[:45] or "investigacion"
        nombre_final = f"{base_slug}_{secrets.token_hex(3)}.{tipo}"
        ruta_final = os.path.join(FILES_DIR, nombre_final)
        titulo_doc = data.get("titulo") or tema[:80]

        if tipo == "xlsx":
            wb = openpyxl.Workbook()
            wb.remove(wb.active)
            hojas = list(data.get("hojas") or [{"nombre": "Datos", "columnas": ["Item", "Detalle"], "filas": [[tema, ""]]}])
            if fuentes:
                hojas.append({
                    "nombre": "Fuentes",
                    "columnas": ["N°", "Título", "Fecha", "URL"],
                    "filas": [[i, f["titulo"], f.get("fecha", ""), f["url"]] for i, f in enumerate(fuentes, 1)],
                })

            header_fill = PatternFill("solid", fgColor="4F46E5")
            header_font = Font(bold=True, color="FFFFFF")

            for idx, h in enumerate(hojas[:6]):
                ws = wb.create_sheet(title=(h.get("nombre") or f"Hoja {idx + 1}")[:31])
                cols = h.get("columnas") or []
                if cols:
                    ws.append(cols)
                    for cell in ws[1]:
                        cell.fill = header_fill
                        cell.font = header_font
                for fila in (h.get("filas") or []):
                    if isinstance(fila, list):
                        ws.append(fila)
                for col in ws.columns:
                    max_len = max((len(str(c.value or "")) for c in col), default=10)
                    ws.column_dimensions[col[0].column_letter].width = min(max(max_len + 4, 12), 50)
            wb.save(ruta_final)

        elif tipo == "docx":
            doc = Document()
            doc.add_heading(titulo_doc, 0)
            if data.get("subtitulo"):
                doc.add_paragraph(str(data["subtitulo"]))
            for sec in (data.get("secciones") or []):
                if sec.get("encabezado"):
                    doc.add_heading(str(sec["encabezado"]), level=1)
                for p in (sec.get("parrafos") or []):
                    if p:
                        doc.add_paragraph(str(p))
                for v in (sec.get("vinetas") or []):
                    if v:
                        doc.add_paragraph(str(v), style="List Bullet")
                tabla = sec.get("tabla") or []
                if len(tabla) >= 1 and isinstance(tabla[0], list) and tabla[0]:
                    t = doc.add_table(rows=len(tabla), cols=len(tabla[0]))
                    t.style = "Table Grid"
                    for r_i, fila in enumerate(tabla):
                        for c_i in range(min(len(fila), len(tabla[0]))):
                            t.cell(r_i, c_i).text = str(fila[c_i] if fila[c_i] is not None else "")
            if fuentes:
                doc.add_heading("Fuentes consultadas", level=1)
                for i, f in enumerate(fuentes, 1):
                    fecha = f" ({f['fecha']})" if f.get("fecha") else ""
                    doc.add_paragraph(f"{i}. {f['titulo']}{fecha} — {f['url']}", style="List Bullet")
            doc.save(ruta_final)

        elif tipo == "pdf":
            pdf = FPDF()
            pdf.set_margins(15, 15, 15)
            pdf.set_auto_page_break(auto=True, margin=15)
            pdf.add_page()
            ancho = pdf.epw

            pdf.set_x(pdf.l_margin)
            pdf.set_font("Helvetica", "B", 16)
            pdf.multi_cell(ancho, 9, limpiar_latin1(titulo_doc))

            if data.get("subtitulo"):
                pdf.set_x(pdf.l_margin)
                pdf.set_font("Helvetica", "I", 11)
                pdf.multi_cell(ancho, 7, limpiar_latin1(str(data["subtitulo"])))
            pdf.ln(4)

            for sec in (data.get("secciones") or []):
                if sec.get("encabezado"):
                    pdf.set_x(pdf.l_margin)
                    pdf.set_font("Helvetica", "B", 13)
                    pdf.multi_cell(ancho, 8, limpiar_latin1(str(sec["encabezado"])))
                    pdf.ln(1)

                pdf.set_font("Helvetica", "", 11)
                for p in (sec.get("parrafos") or []):
                    if p:
                        pdf.set_x(pdf.l_margin)
                        pdf.multi_cell(ancho, 6, limpiar_latin1(str(p)))
                        pdf.ln(2)

                for v in (sec.get("vinetas") or []):
                    if v:
                        pdf.set_x(pdf.l_margin)
                        pdf.multi_cell(ancho, 6, limpiar_latin1(f"- {v}"))

                tabla = sec.get("tabla") or []
                if len(tabla) >= 1 and isinstance(tabla[0], list) and tabla[0]:
                    pdf.ln(2)
                    n_cols = min(len(tabla[0]), 6)
                    ancho_col = ancho / max(n_cols, 1)
                    max_chars = max(int(ancho_col / 2.1), 18)
                    for r_i, fila in enumerate(tabla):
                        pdf.set_x(pdf.l_margin)
                        pdf.set_font("Helvetica", "B" if r_i == 0 else "", 8.5)
                        for c_i in range(n_cols):
                            val = str(fila[c_i]) if c_i < len(fila) and fila[c_i] is not None else ""
                            pdf.cell(ancho_col, 7, limpiar_latin1(val)[:max_chars], border=1)
                        pdf.ln()
                pdf.ln(3)

            if fuentes:
                pdf.set_x(pdf.l_margin)
                pdf.set_font("Helvetica", "B", 13)
                pdf.multi_cell(ancho, 8, "Fuentes consultadas")
                pdf.ln(1)
                pdf.set_font("Helvetica", "", 9.5)
                for i, f in enumerate(fuentes, 1):
                    fecha = f" ({f['fecha']})" if f.get("fecha") else ""
                    pdf.set_x(pdf.l_margin)
                    pdf.multi_cell(ancho, 5.5, limpiar_latin1(f"{i}. {f['titulo']}{fecha}: {f['url']}"))
                    pdf.ln(1)

            pdf.output(ruta_final)

        return nombre_final, titulo_doc, None
    except Exception as e:
        return None, None, str(e)[:180]


# ---------------------------------------------------------------- #3: generador de imágenes
def detectar_solicitud_imagen(texto):
    if not texto:
        return None
    original = texto.strip()
    limpio = re.sub(r"[.!?¡¿,;]+$", "", sin_tildes(original.lower())).strip()

    m1 = re.match(
        r"^(?:por favor\s+)?(?:puedes\s+|podrias\s+)?"
        r"(?:genera|generar|generame|crea|crear|creame|haz|has|hacer|hazme|hasme|disena|disenar|disename|pinta|pintame)\s+"
        r"(?:una?\s+|el\s+|la\s+)?"
        r"(imagen|foto|fotografia|ilustracion|dibujo|logo|logotipo|icono|avatar|wallpaper|fondo\s+de\s+pantalla|retrato|poster|banner|portada)\b\s*"
        r"(?:de\s+|sobre\s+|con\s+|para\s+)?(.*)$",
        limpio,
    )
    if m1:
        tipo = m1.group(1).strip()
        desc = re.sub(
            r"^(?:por favor\s+)?(?:puedes\s+|podrías\s+|podrias\s+)?"
            r"(?:genera|generar|genérame|generame|crea|crear|créame|creame|haz|has|hacer|hazme|hasme|diseña|disena|diseñar|disenar|diseñame|disename|pinta|píntame|pintame)\s+"
            r"(?:una?\s+|el\s+|la\s+)?"
            r"(?:imagen|foto|fotografía|fotografia|ilustración|ilustracion|dibujo|logo|logotipo|ícono|icono|avatar|wallpaper|fondo\s+de\s+pantalla|retrato|póster|poster|banner|portada)\b\s*"
            r"(?:de\s+|sobre\s+|con\s+|para\s+)?",
            "",
            original,
            flags=re.IGNORECASE,
        ).strip()
        if desc.lower() in ("", "cualquiera", "random", "aleatoria", "al azar"):
            desc = "un paisaje digital futurista vibrante con luces neón y detalles cinematográficos"
        if tipo in ("logo", "logotipo", "icono", "avatar", "wallpaper", "fondo de pantalla", "poster", "banner", "portada"):
            return f"{tipo} de {desc}"
        return desc

    m2 = re.match(
        r"^(?:por favor\s+)?(?:puedes\s+|podrias\s+)?"
        r"(?:dibuja|dibujar|dibujame|ilustra|ilustrar|ilustrame)\s+"
        r"(?:una?\s+|el\s+|la\s+)?(.+)$",
        limpio,
    )
    if m2:
        return re.sub(
            r"^(?:por favor\s+)?(?:puedes\s+|podrías\s+|podrias\s+)?"
            r"(?:dibuja|dibujar|dibújame|dibujame|ilustra|ilustrar|ilústrame|ilustrame)\s+",
            "",
            original,
            flags=re.IGNORECASE,
        ).strip()

    return None


def generar_imagen_ia(prompt):
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
                config=types.GenerateImagesConfig(number_of_images=1, output_mime_type="image/png"),
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
            errores.append(f"{m}: {str(e)[:120]}")

    for m in ["gemini-2.5-flash-image", "gemini-2.0-flash-exp-image-generation"]:
        try:
            res = client.models.generate_content(
                model=m,
                contents=f"Genera una imagen de alta calidad: {prompt}",
                config=types.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"]),
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
            errores.append(f"{m}: {str(e)[:120]}")

    # Respaldo gratuito sin clave si la cuota gratuita de Google bloquea Imagen
    try:
        url_fb = f"https://image.pollinations.ai/prompt/{quote(prompt)}?width=1024&height=1024&nologo=true"
        req = Request(url_fb, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=25) as resp:
            raw_data = resp.read()
        if raw_data and len(raw_data) > 2000:
            nombre = f"img_{int(time.time())}_{secrets.token_hex(4)}.jpg"
            with open(os.path.join(GENERATED_DIR, nombre), "wb") as f:
                f.write(raw_data)
            return nombre, None
    except Exception as e:
        errores.append(f"fallback: {str(e)[:120]}")

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
    if not mensaje:
        return None, None
    texto_original = mensaje.strip()
    m_limpio = re.sub(r"[.!?¡¿,;]+$", "", sin_tildes(texto_original.lower())).strip()

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


# ---------------------------------------------------------------- #2: motores IA (Gemini, DeepSeek, Grok)
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
    return f"Error al consultar el modelo: {s[:300]}"


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
    sin_busqueda = config.model_copy(update={"tools": None})
    for i, m in enumerate(orden_modelos(primero)):
        yield m, (config if i == 0 else sin_busqueda)


def generar_simple(contenidos, config=None, primero=None):
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


def stream_openai_compatible(base_url, api_key, modelo, system_prompt, contenidos):
    """Streaming SSE para APIs compatibles con OpenAI (DeepSeek / Grok)."""
    mensajes = [{"role": "system", "content": system_prompt}]
    for c in contenidos:
        rol = "assistant" if c.role == "model" else "user"
        textos = [p.text for p in (c.parts or []) if getattr(p, "text", None)]
        if textos:
            mensajes.append({"role": rol, "content": "\n\n".join(textos)})

    cuerpo = json.dumps({"model": modelo, "messages": mensajes, "stream": True}).encode("utf-8")
    req = Request(
        base_url,
        data=cuerpo,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "JLG-AI/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=45) as resp:
            for linea_raw in resp:
                linea = linea_raw.decode("utf-8", errors="ignore").strip()
                if not linea.startswith("data:"):
                    continue
                dato = linea[5:].strip()
                if dato == "[DONE]":
                    break
                try:
                    ev = json.loads(dato)
                    delta = ev.get("choices", [{}])[0].get("delta", {}).get("content")
                    if delta:
                        yield delta
                except Exception:
                    continue
    except HTTPError as e:
        detalle = e.read().decode("utf-8", errors="ignore")[:160]
        raise RuntimeError(f"HTTP {e.code}: {detalle}")


def historial_gemini(conv):
    previos = conv.mensajes[-MAX_HISTORIAL:]
    return [types.Content(role=m.rol, parts=[types.Part.from_text(text=m.contenido)])
            for m in previos if m.contenido]


# ---------------------------------------------------------------- búsqueda en internet
def decidir_busqueda(texto, contexto):
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


# ---------------------------------------------------------------- chat principal
@app.post("/api/chat")
@login_requerido
def chat():
    u = usuario_actual()
    texto = request.form.get("message", "").strip()
    archivo = request.files.get("file")
    cid = request.form.get("conversacion_id", type=int)
    primero = MODELO_PRO if request.form.get("modo") == "avanzado" else MODELO
    motor = (request.form.get("motor") or "gemini").strip().lower()
    personalidad = (request.form.get("personalidad") or "default").strip().lower()

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
    requiere_vision_binaria = False

    if archivo:
        nombre_adjunto = archivo.filename or "imagen-pegada.png"
        mime_adjunto = archivo.mimetype or "application/octet-stream"
        datos_adjunto = archivo.read()
        texto_adjunto, enviar_bytes = extraer_texto_archivo(datos_adjunto, nombre_adjunto, mime_adjunto)

        if enviar_bytes:
            partes.append(types.Part.from_bytes(data=datos_adjunto, mime_type=mime_adjunto))
            # Solo exige visión multimodal si no pudimos extraer texto (imágenes, audio o PDF escaneado)
            if not texto_adjunto or texto_adjunto.startswith("[El archivo PDF"):
                requiere_vision_binaria = True

        if texto_adjunto:
            partes.append(types.Part.from_text(text=texto_adjunto))

    if texto:
        partes.append(types.Part.from_text(text=texto))
    else:
        partes.append(types.Part.from_text(text=f"Analiza en detalle el archivo adjunto ({nombre_adjunto})."))
    contenidos.append(types.Content(role="user", parts=partes))

    db.session.add(Mensaje(conversacion=conv, rol="user", contenido=texto, adjunto=nombre_adjunto))
    conv.actualizado = ahora()
    db.session.commit()
    conv_id, uid = conv.id, u.id

    sys_prompt = instrucciones_sistema(u, personalidad)
    config = types.GenerateContentConfig(
        system_instruction=sys_prompt,
        tools=[types.Tool(google_search=types.GoogleSearch())] if BUSQUEDA_GOOGLE else None,
    )

    respuesta_app, url_abrir = intentar_abrir_app(u, texto)
    respuesta_rec, payload_rec = (None, None) if respuesta_app else detectar_recordatorio(texto)
    tipo_archivo, tema_archivo = (None, None) if (respuesta_app or respuesta_rec or archivo) else detectar_solicitud_archivo(texto)
    prompt_imagen = None if (respuesta_app or respuesta_rec or tipo_archivo or archivo) else detectar_solicitud_imagen(texto)
    urls_leer = [] if (respuesta_app or respuesta_rec or tipo_archivo or prompt_imagen) else extraer_urls_mensaje(texto)

    def generar():
        trozos = []
        if respuesta_app:
            trozos.append(respuesta_app)
            yield respuesta_app
        elif respuesta_rec:
            trozos.append(respuesta_rec)
            yield respuesta_rec
        elif tipo_archivo:
            nombres_fmt = {"docx": "documento Word", "xlsx": "hoja de cálculo Excel", "pdf": "documento PDF"}
            info_web, fuentes_doc = "", []
            if BUSQUEDA_WEB:
                aviso_inv = f"🔎 *Investigando a fondo sobre «{tema_archivo}» en internet...*\n\n"
                trozos.append(aviso_inv)
                yield aviso_inv
                info_web, fuentes_doc = investigar_para_documento(tema_archivo)

            aviso_doc = f"📄 *Redactando y generando {nombres_fmt[tipo_archivo]}...*\n\n"
            trozos.append(aviso_doc)
            yield aviso_doc
            nombre_f, titulo_f, err_f = generar_archivo_ia(tipo_archivo, tema_archivo, u, info_web, fuentes_doc)
            if nombre_f:
                bloque_doc = (
                    f"**{titulo_f}**\n\n"
                    f"[⬇️ Descargar {nombre_f}](/api/archivos/{nombre_f})"
                )
                trozos.append(bloque_doc)
                yield bloque_doc
            else:
                aviso_err = f"⚠️ No se pudo generar el archivo ({err_f})."
                trozos.append(aviso_err)
                yield aviso_err
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
            leyo_url = False
            if urls_leer:
                for u_link in urls_leer:
                    titulo_u, cuerpo_u, err_u = leer_pagina_web(u_link)
                    aviso_u = f"🌐 *Leyendo enlace: [{titulo_u}]({u_link})*\n\n"
                    trozos.append(aviso_u)
                    yield aviso_u
                    if cuerpo_u:
                        leyo_url = True
                        contenidos[-1].parts.insert(0, types.Part.from_text(
                            text=f"[Contenido extraído de {u_link} ({titulo_u}):\n{cuerpo_u}\n]"
                        ))
                    else:
                        contenidos[-1].parts.insert(0, types.Part.from_text(
                            text=f"[No se pudo extraer el texto de {u_link}: {err_u}. Indícalo brevemente.]"
                        ))

            if BUSQUEDA_WEB and texto and not leyo_url and not urls_leer and not archivo:
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

            usar_gemini = True
            if motor == "deepseek" and not requiere_vision_binaria:
                ds_key = os.getenv("DEEPSEEK_API_KEY")
                if ds_key:
                    try:
                        for chunk in stream_openai_compatible(
                            "https://api.deepseek.com/chat/completions", ds_key, MODELO_DEEPSEEK, sys_prompt, contenidos
                        ):
                            trozos.append(chunk)
                            yield chunk
                        usar_gemini = False
                    except Exception as e:
                        aviso_fb = f"*(DeepSeek no disponible: {str(e)[:80]} — usando Gemini)*\n\n"
                        trozos.append(aviso_fb)
                        yield aviso_fb
                else:
                    aviso_fb = "*(Falta `DEEPSEEK_API_KEY` en `.env` — respondiendo con Gemini)*\n\n"
                    trozos.append(aviso_fb)
                    yield aviso_fb

            elif motor == "grok" and not requiere_vision_binaria:
                grok_key = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")
                if grok_key:
                    try:
                        for chunk in stream_openai_compatible(
                            "https://api.x.ai/v1/chat/completions", grok_key, MODELO_GROK, sys_prompt, contenidos
                        ):
                            trozos.append(chunk)
                            yield chunk
                        usar_gemini = False
                    except Exception as e:
                        aviso_fb = f"*(Grok no disponible: {str(e)[:80]} — usando Gemini)*\n\n"
                        trozos.append(aviso_fb)
                        yield aviso_fb
                else:
                    aviso_fb = "*(Falta `XAI_API_KEY` en `.env` — respondiendo con Gemini)*\n\n"
                    trozos.append(aviso_fb)
                    yield aviso_fb

            if usar_gemini:
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
            elif fuentes:
                trozos.append(fuentes)
                yield fuentes

        respuesta = "".join(trozos).strip() or "(sin respuesta)"
        db.session.add(Mensaje(conversacion_id=conv_id, rol="model", contenido=respuesta))
        db.session.commit()
        if texto and not (respuesta_app or respuesta_rec or tipo_archivo or prompt_imagen):
            threading.Thread(target=extraer_recuerdos, args=(uid, texto, respuesta), daemon=True).start()

    resp = Response(stream_with_context(generar()), mimetype="text/plain; charset=utf-8")
    resp.headers["X-Conversacion-Id"] = str(conv_id)
    if url_abrir:
        resp.headers["X-Abrir-Url"] = quote(url_abrir, safe=":/?#[]@!$&'()*+,;=%")
    if payload_rec:
        resp.headers["X-Recordatorio"] = quote(json.dumps(payload_rec, ensure_ascii=False))
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.post("/api/transcribir")
@login_requerido
def transcribir():
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