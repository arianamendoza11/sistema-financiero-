# -*- coding: utf-8 -*-
"""
Funciones compartidas por todos los scripts de ingesta: resolver datos contra
Supabase y notificar por Telegram. No contiene logica de negocio de ningun
tipo de ingesta en particular - eso vive en cada script (ingesta_estandarizada,
ingesta_dinamica, etc).
"""
import json
import os
import time

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

REST_HEADERS = {
    "apikey": SERVICE_KEY,
    "Authorization": f"Bearer {SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def notificar_telegram(texto):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": texto}, timeout=10)
    except requests.RequestException:
        pass  # si Telegram falla, no debe tapar el error original


SIGNO_POR_TIPO_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "signo_por_tipo.json")
with open(SIGNO_POR_TIPO_PATH, encoding="utf-8") as _f:
    _SIGNO_POR_TIPO = json.load(_f)


def resolver_signo(tipo):
    """Devuelve +1/-1 segun config/signo_por_tipo.json para el 'tipo' de una
    categoria (ingreso, gasto, etc.), o None si ese tipo no esta permitido en
    un registro de una sola linea (ej. 'traspaso' necesita 2 filas ligadas,
    no lo puede decidir un signo suelto)."""
    return _SIGNO_POR_TIPO.get(tipo)


def resolver_categoria(codigo_categoria, fecha):
    """Devuelve {id_categoria, tipo, glosa} de la version vigente en 'fecha',
    o None si no existe ninguna version vigente para ese codigo+fecha."""
    url = f"{SUPABASE_URL}/rest/v1/categorias"
    params = {
        "codigo_categoria": f"eq.{codigo_categoria}",
        "fecha_inicio": f"lte.{fecha}",
        "or": f"(fecha_fin.is.null,fecha_fin.gte.{fecha})",
        "select": "id_categoria,tipo,glosa",
    }
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    filas = r.json()
    return filas[0] if filas else None


def resolver_id_cuenta_por_nombre(nombre_cuenta):
    """Devuelve el id_cuenta cuyo nombre_cuenta coincide exacto, o None."""
    url = f"{SUPABASE_URL}/rest/v1/cuentas"
    params = {"nombre_cuenta": f"eq.{nombre_cuenta}", "select": "id_cuenta"}
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    filas = r.json()
    return filas[0]["id_cuenta"] if filas else None


def resolver_id_etiqueta_por_nombre(nombre_etiqueta):
    """Devuelve el id_etiqueta cuyo nombre_etiqueta coincide exacto, o None."""
    url = f"{SUPABASE_URL}/rest/v1/etiquetas"
    params = {"nombre_etiqueta": f"eq.{nombre_etiqueta}", "select": "id_etiqueta"}
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    filas = r.json()
    return filas[0]["id_etiqueta"] if filas else None


def insertar_filas(filas):
    """POST atomico (una sola sentencia INSERT) de 1-N filas. Devuelve la
    respuesta cruda de requests; el llamador decide como tratar el error."""
    url = f"{SUPABASE_URL}/rest/v1/seguimiento_efectivo"
    return requests.post(url, headers=REST_HEADERS, json=filas, timeout=15)


def listar_objetos_storage(bucket):
    """Devuelve la lista de objetos de un bucket (cada uno con 'name' y
    'created_at' entre otros campos), tal como los expone Supabase Storage."""
    url = f"{SUPABASE_URL}/storage/v1/object/list/{bucket}"
    body = {"limit": 1000, "sortBy": {"column": "created_at", "order": "asc"}}
    r = requests.post(url, headers=REST_HEADERS, json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def borrar_objetos_storage(bucket, nombres):
    """Borra en un solo lote una lista de objetos (por nombre, relativo al
    bucket) de Supabase Storage. Devuelve la respuesta cruda de requests."""
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}"
    return requests.delete(url, headers=REST_HEADERS, json={"prefixes": nombres}, timeout=15)


def listar_etiquetas_activas():
    """Vocabulario vigente de etiquetas (consultado en vivo, nunca hardcodeado)."""
    url = f"{SUPABASE_URL}/rest/v1/etiquetas"
    params = {"estado": "eq.activa", "select": "nombre_etiqueta"}
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return sorted({fila["nombre_etiqueta"] for fila in r.json()})


GLOSARIO_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "glosario_ocr_recibos.md")


def leer_glosario():
    with open(GLOSARIO_PATH, encoding="utf-8") as f:
        return f.read()


MODELO_GEMINI = "gemini-3.8-flash-TEST-FALLBACK-TEMPORAL"


REINTENTOS_503 = 3
ESPERA_ENTRE_REINTENTOS_SEG = 5


def llamar_gemini_json(parts, api_key):
    """Llama a Gemini generateContent con las 'parts' ya armadas por el
    caller (solo texto, o texto+imagen) y devuelve el JSON parseado. Lanza
    RuntimeError con un mensaje claro si algo falla - cada script decide como
    reportarlo con su propio error_salir/Telegram.

    Reintenta solo ante 503 (servidores de Google saturados, transitorio y
    le pasa igual a free que a pago) - cualquier otro codigo falla directo,
    reintentar no lo arregla."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODELO_GEMINI}:generateContent?key={api_key}"
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseMimeType": "application/json"},
    }

    r = None
    for intento in range(1, REINTENTOS_503 + 1):
        r = requests.post(url, json=body, timeout=60)
        if r.ok or r.status_code != 503 or intento == REINTENTOS_503:
            break
        time.sleep(ESPERA_ENTRE_REINTENTOS_SEG)

    if not r.ok:
        raise RuntimeError(f"Gemini rechazo la peticion ({r.status_code}): {r.text}")

    data = r.json()
    try:
        texto = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise RuntimeError(f"Respuesta de Gemini con forma inesperada: {data}")

    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        raise RuntimeError(f"Gemini no devolvio JSON valido: {texto}")
