"""Copia usuarios, conversaciones, mensajes y memoria de la base local (asistente.db)
a otra base de datos (por ejemplo, Postgres de Supabase).

Uso:
    DESTINO_URL="postgresql://..." python migrar_datos.py

No borra nada de la base local. Si el destino ya tiene usuarios, se detiene
para no duplicar datos.
"""
import os
import sys

from sqlalchemy import create_engine, func, select, text

import database  # registra las tablas (modelos)

ORIGEN = os.getenv("ORIGEN_URL") or "sqlite:///" + os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "asistente.db")
DESTINO = os.getenv("DESTINO_URL", "").strip()
_archivo = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".database_url")
if not DESTINO and os.path.exists(_archivo):  # guardada por conectar_supabase.py
    DESTINO = open(_archivo).read().strip()
DESTINO = database.normalizar_postgres(DESTINO)

TABLAS = ["usuarios", "conversaciones", "mensajes", "recuerdos"]  # en orden por dependencias


def main():
    if not DESTINO:
        sys.exit("❌ Falta DESTINO_URL (la conexión de Supabase).")
    meta = database.db.metadata
    origen = create_engine(ORIGEN)
    destino = create_engine(DESTINO, pool_pre_ping=True)

    meta.create_all(destino)  # crea las tablas si no existen

    with destino.connect() as d:
        ya = d.execute(select(func.count()).select_from(meta.tables["usuarios"])).scalar()
    if ya:
        sys.exit(f"⚠️  El destino ya tiene {ya} usuario(s). No copio nada para no duplicar.")

    with origen.connect() as o, destino.begin() as d:
        for nombre in TABLAS:
            tabla = meta.tables[nombre]
            filas = [dict(f._mapping) for f in o.execute(select(tabla).order_by(tabla.c.id))]
            if filas:
                d.execute(tabla.insert(), filas)
            print(f"  {nombre}: {len(filas)} copiados")
        if destino.dialect.name == "postgresql":
            # que los próximos IDs sigan después de los copiados
            for nombre in TABLAS:
                d.execute(text(
                    f"SELECT setval(pg_get_serial_sequence('{nombre}', 'id'), "
                    f"COALESCE((SELECT MAX(id) FROM {nombre}), 0) + 1, false)"))
    print("✅ Datos copiados.")


if __name__ == "__main__":
    main()
