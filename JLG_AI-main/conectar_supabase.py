"""Conecta JLG AI con tu base de datos de Supabase y copia tus datos.

Uso:  .venv-unix/bin/python conectar_supabase.py <id-del-proyecto>
      (el id es la parte de https://<id>.supabase.co)

- Pide la contraseña de la base de datos sin mostrarla.
- Busca el "Session pooler" correcto (compatible con Render gratis).
- Copia usuarios, chats y memoria (migrar_datos.py).
- Guarda la dirección en .database_url (no se sube a GitHub) y la copia al portapapeles.
"""
import getpass
import os
import subprocess
import sys
from urllib.parse import quote

import psycopg2

BASE = os.path.dirname(os.path.abspath(__file__))
REGION = os.getenv("SUPABASE_REGION", "us-east-1")


def probar(url):
    try:
        psycopg2.connect(url, connect_timeout=10).close()
        return None
    except Exception as e:
        return str(e).strip().splitlines()[0]


def main():
    if len(sys.argv) < 2:
        sys.exit("Uso: python conectar_supabase.py <id-del-proyecto>")
    ref = sys.argv[1].strip()
    clave = getpass.getpass("🔑 Pega la contraseña de la base de datos de Supabase (no se verá) y pulsa Enter: ").strip()
    if not clave:
        sys.exit("❌ No escribiste la contraseña.")

    url = None
    for n in range(0, 3):
        host = f"aws-{n}-{REGION}.pooler.supabase.com"
        candidata = f"postgresql://postgres.{ref}:{quote(clave, safe='')}@{host}:5432/postgres"
        print(f"   Probando {host} ...", end=" ", flush=True)
        error = probar(candidata)
        if error is None:
            print("✅")
            url = candidata
            break
        print("no")
        if "password authentication failed" in error:
            sys.exit("❌ La contraseña no es correcta. Revísala en tus notas, o en Supabase usa "
                     "'Restablecer contraseña de la base de datos' y vuelve a intentarlo.")
    if not url:
        sys.exit(f"❌ No pude conectar. Último error: {error}")

    ruta = os.path.join(BASE, ".database_url")
    with open(ruta, "w") as f:
        f.write(url + "\n")
    os.chmod(ruta, 0o600)

    print("\n📦 Copiando tus usuarios, chats y memoria a Supabase...")
    r = subprocess.run([sys.executable, os.path.join(BASE, "migrar_datos.py")],
                       env={**os.environ, "DESTINO_URL": url})
    if r.returncode != 0:
        sys.exit("❌ La copia de datos falló (mira el mensaje de arriba).")

    try:
        subprocess.run(["pbcopy"], input=url.encode(), check=True)
        print("\n📋 La dirección de la base de datos quedó copiada en tu portapapeles (para Render).")
    except Exception:
        pass
    print("✅ ¡Listo! Supabase está conectado y tiene tus datos.")


if __name__ == "__main__":
    main()
