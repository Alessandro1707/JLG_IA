"""Modelos de base de datos de JLG AI.

Funciona con SQLite en local (asistente.db) y con PostgreSQL en el servidor
(Supabase, Render, Railway...) solo cambiando la variable DATABASE_URL.
"""
import os
import sqlite3
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def ahora():
    return datetime.now(timezone.utc)


def database_url():
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asistente.db")
        return f"sqlite:///{ruta}"
    # Heroku/Render/Supabase a veces entregan "postgres://", SQLAlchemy necesita "postgresql://"
    return normalizar_postgres(url)


def normalizar_postgres(url):
    """Usa siempre el driver psycopg2 (el que está en requirements.txt)."""
    for prefijo in ("postgres://", "postgresql://"):
        if url.startswith(prefijo):
            return "postgresql+psycopg2://" + url[len(prefijo):]
    return url


class Usuario(db.Model):
    __tablename__ = "usuarios"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    es_admin = db.Column(db.Boolean, default=False, nullable=False)
    creado = db.Column(db.DateTime(timezone=True), default=ahora)

    conversaciones = db.relationship("Conversacion", backref="usuario", cascade="all, delete-orphan")
    recuerdos = db.relationship("Recuerdo", backref="usuario", cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Conversacion(db.Model):
    __tablename__ = "conversaciones"
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False, index=True)
    titulo = db.Column(db.String(120), default="Nueva conversación")
    creado = db.Column(db.DateTime(timezone=True), default=ahora)
    actualizado = db.Column(db.DateTime(timezone=True), default=ahora, index=True)

    mensajes = db.relationship(
        "Mensaje", backref="conversacion", cascade="all, delete-orphan", order_by="Mensaje.id"
    )

    def to_dict(self):
        return {"id": self.id, "titulo": self.titulo, "actualizado": self.actualizado.isoformat()}


class Mensaje(db.Model):
    __tablename__ = "mensajes"
    id = db.Column(db.Integer, primary_key=True)
    conversacion_id = db.Column(db.Integer, db.ForeignKey("conversaciones.id"), nullable=False, index=True)
    rol = db.Column(db.String(20), nullable=False)  # "user" o "model"
    contenido = db.Column(db.Text, nullable=False)
    adjunto = db.Column(db.String(255))
    creado = db.Column(db.DateTime(timezone=True), default=ahora)

    def to_dict(self):
        return {"id": self.id, "rol": self.rol, "contenido": self.contenido, "adjunto": self.adjunto}


class Recuerdo(db.Model):
    """Datos que la IA recuerda del usuario entre conversaciones."""
    __tablename__ = "recuerdos"
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuarios.id"), nullable=False, index=True)
    contenido = db.Column(db.String(500), nullable=False)
    creado = db.Column(db.DateTime(timezone=True), default=ahora)

    def to_dict(self):
        return {"id": self.id, "contenido": self.contenido}


def init_db(app):
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url()
    opciones = {"pool_pre_ping": True}
    if not app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        # Postgres gratis (Supabase) admite pocas conexiones: mantenemos el pool pequeño
        opciones.update(pool_size=3, max_overflow=2, pool_recycle=280)
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = opciones
    db.init_app(app)
    with app.app_context():
        db.create_all()


def importar_historial_antiguo(usuario):
    """Pasa los mensajes de la tabla vieja 'historial' (versión anterior) al primer usuario."""
    if not db.engine.url.drivername.startswith("sqlite"):
        return
    ruta = db.engine.url.database
    try:
        with sqlite3.connect(ruta) as conn:
            filas = conn.execute("SELECT rol, mensaje FROM historial ORDER BY id").fetchall()
    except sqlite3.Error:
        return
    if not filas:
        return
    conv = Conversacion(usuario=usuario, titulo="Historial anterior")
    db.session.add(conv)
    for rol, mensaje in filas:
        db.session.add(Mensaje(
            conversacion=conv,
            rol="user" if rol in ("usuario", "user") else "model",
            contenido=mensaje,
        ))
    db.session.commit()
