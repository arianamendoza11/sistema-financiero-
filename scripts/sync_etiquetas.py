# -*- coding: utf-8 -*-
"""
Sincroniza etiquetas desde Supabase a caché local (config/etiquetas.json).
Se ejecuta vía CI/CD cada 3 días. El caché se usa en runtime por los workflows.

No cambia nada en Supabase, solo LEE y escribe un JSON local.
"""
import json
import os
import sys
from datetime import datetime

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

REST_HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
}

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")
ETIQUETAS_CACHE_PATH = os.path.join(CONFIG_DIR, "etiquetas.json")


def fetch_etiquetas_desde_supabase():
    """Descarga tabla etiquetas (solo columnas activas con keywords)."""
    url = f"{SUPABASE_URL}/rest/v1/etiquetas"
    params = {
        "estado": "eq.activa",
        "select": "id_etiqueta,nombre_etiqueta,keyword",
    }
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def guardar_cache_local(etiquetas):
    """Escribe el JSON de caché con metadata."""
    cache_data = {
        "timestamp_sync": datetime.utcnow().isoformat(),
        "cantidad": len(etiquetas),
        "etiquetas": {
            e["nombre_etiqueta"]: {
                "id": e["id_etiqueta"],
                "keyword": e.get("keyword") or [],
            }
            for e in etiquetas
        },
    }
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(ETIQUETAS_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache_data, f, indent=2, ensure_ascii=False)


def main():
    try:
        print("📥 Sincronizando etiquetas desde Supabase...", file=sys.stderr)
        etiquetas = fetch_etiquetas_desde_supabase()
        guardar_cache_local(etiquetas)
        print(f"✅ Sincronizadas {len(etiquetas)} etiquetas en {ETIQUETAS_CACHE_PATH}", file=sys.stderr)
    except Exception as e:
        print(f"❌ Error sincronizando etiquetas: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
