# -*- coding: utf-8 -*-
"""
Loader de etiquetas dinámicas con fallback.

Intenta leer caché local primero (rápido, 0ms). Si no existe, consulta Supabase
en vivo (fallback para compatibilidad durante la transición).
"""
import json
import os
import sys

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

REST_HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
}

ETIQUETAS_CACHE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "etiquetas.json"
)


def cargar_etiquetas():
    """
    Retorna un diccionario {nombre_etiqueta: {id, keyword}}.
    
    Estrategia:
    1. Si existe config/etiquetas.json → devuelve eso (caché local)
    2. Si no existe → consulta Supabase en vivo (fallback, para transición)
    """
    # Intenta caché local
    if os.path.exists(ETIQUETAS_CACHE_PATH):
        try:
            with open(ETIQUETAS_CACHE_PATH, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
                return cache_data.get("etiquetas", {})
        except (json.JSONDecodeError, IOError) as e:
            print(f"⚠️  Error leyendo caché local, recurriendo a Supabase: {e}", file=sys.stderr)

    # Fallback: Supabase en vivo
    print("📡 Caché local no encontrada, consultando Supabase...", file=sys.stderr)
    url = f"{SUPABASE_URL}/rest/v1/etiquetas"
    params = {
        "estado": "eq.activa",
        "select": "id_etiqueta,nombre_etiqueta,keyword",
    }
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()

    return {
        e["nombre_etiqueta"]: {
            "id": e["id_etiqueta"],
            "keyword": e.get("keyword") or [],
        }
        for e in r.json()
    }


def construir_glosario_para_prompt(etiquetas_dict):
    """
    Transforma el dict de etiquetas en un glosario de texto para el prompt de Gemini.
    
    Retorna una cadena como:
    ```
    - `despensa`: pan, carne, pescado, filete, pollo, ...
    - `comida fuera`: menú del día, kebab, pizza, ...
    ```
    
    Usa `keyword` (array) si existe, si no deja la línea vacía.
    """
    lineas = []
    for nombre in sorted(etiquetas_dict.keys()):
        data = etiquetas_dict[nombre]
        keywords = data.get("keyword") or []
        if keywords:
            keywords_str = ", ".join(keywords)
            lineas.append(f"- `{nombre}`: {keywords_str}")
        else:
            lineas.append(f"- `{nombre}`: (sin keywords aún)")
    return "\n".join(lineas)
