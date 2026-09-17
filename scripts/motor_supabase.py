# -*- coding: utf-8 -*-
"""
Funciones compartidas por todos los scripts de ingesta: resolver datos contra
Supabase y notificar por Telegram. No contiene logica de negocio de ningun
tipo de ingesta en particular - eso vive en cada script (ingesta_estandarizada,
ingesta_dinamica, etc).
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

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


def resolver_nombre_etiqueta_por_id(id_etiqueta):
    """Devuelve el nombre_etiqueta cuyo id_etiqueta coincide exacto, o None."""
    url = f"{SUPABASE_URL}/rest/v1/etiquetas"
    params = {"id_etiqueta": f"eq.{id_etiqueta}", "select": "nombre_etiqueta"}
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    filas = r.json()
    return filas[0]["nombre_etiqueta"] if filas else None


def insertar_filas(filas):
    """POST atomico (una sola sentencia INSERT) de 1-N filas. Devuelve la
    respuesta cruda de requests; el llamador decide como tratar el error."""
    url = f"{SUPABASE_URL}/rest/v1/seguimiento_efectivo"
    return requests.post(url, headers=REST_HEADERS, json=filas, timeout=15)


def listar_objetos_storage(bucket):
    """Devuelve la lista de objetos de un bucket (cada uno con 'name' y
    'created_at' entre otros campos), tal como los expone Supabase Storage."""
    url = f"{SUPABASE_URL}/storage/v1/object/list/{bucket}"
    body = {"prefix": "", "limit": 1000, "sortBy": {"column": "created_at", "order": "asc"}}
    r = requests.post(url, headers=REST_HEADERS, json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def borrar_objetos_storage(bucket, nombres):
    """Borra en un solo lote una lista de objetos (por nombre, relativo al
    bucket) de Supabase Storage. Devuelve la respuesta cruda de requests."""
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}"
    return requests.delete(url, headers=REST_HEADERS, json={"prefixes": nombres}, timeout=15)


_PATRONES_NO_REINTENTABLES = (
    "no es JSON valido",
    "debe ser una lista",
    "No existe ninguna cuenta",
    "No hay version vigente",
    "no se puede registrar en una sola linea",
    "Falta '",
    "que no existe",
    "Linea incompleta",
    "no esta en la lista preseleccionada",
)


def es_error_reintentable(excepcion):
    """Decide si vale la pena que el cron nocturno reintente este fallo.
    Transitorio (Gemini/Supabase saturado o caido, timeout de red) -> True,
    reintentar puede funcionar. Estructural (dato invalido, config faltante,
    payload malformado) -> False: el mismo input va a fallar exactamente
    igual todas las noches, hace falta que Diego lo arregle a mano antes de
    que reintentar tenga sentido. Ante la duda, True - reintentar de mas sale
    barato (segundos de Actions), reintentar de menos pierde datos."""
    if isinstance(excepcion, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True

    mensaje = str(excepcion)
    if any(codigo in mensaje for codigo in ("503", "504", "502", "500")):
        return True
    if any(patron in mensaje for patron in _PATRONES_NO_REINTENTABLES):
        return False

    return True


def guardar_registro_pendiente(origen, payload, foto_bucket_path=None, error_detalle=None, reintentable=True):
    """Inserta un registro pendiente nuevo a partir de un fallo recien
    ocurrido en vivo. 'Estar en esta tabla' ES el estado de 'pendiente' - no
    hace falta un campo de estado aparte, se borra al resolverse. Devuelve el
    id_pendiente creado."""
    url = f"{SUPABASE_URL}/rest/v1/registros_pendientes"
    body = {
        "origen": origen,
        "payload": payload,
        "foto_bucket_path": foto_bucket_path,
        "error_detalle": error_detalle,
        "reintentable": reintentable,
    }
    r = requests.post(url, headers=REST_HEADERS, json=[body], timeout=15)
    r.raise_for_status()
    return r.json()[0]["id_pendiente"]


def listar_registros_pendientes(solo_reintentables=True):
    url = f"{SUPABASE_URL}/rest/v1/registros_pendientes"
    params = {"reintentable": "eq.true"} if solo_reintentables else {}
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def listar_fotos_pendientes_de_borrado():
    """Rutas de Storage que siguen referenciadas en registros_pendientes
    (origen gasto_foto) - sin importar si son reintentables o no, mientras
    sigan en la tabla es porque todavia no se dieron por perdidas. Lo usa
    limpieza_recibos.py para nunca borrar una foto que el sistema todavia
    necesita, sin importar cuantos dias de antiguedad tenga."""
    url = f"{SUPABASE_URL}/rest/v1/registros_pendientes"
    params = {"origen": "eq.gasto_foto", "select": "foto_bucket_path"}
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return {fila["foto_bucket_path"] for fila in r.json() if fila["foto_bucket_path"]}


def borrar_registro_pendiente(id_pendiente):
    url = f"{SUPABASE_URL}/rest/v1/registros_pendientes"
    requests.delete(url, headers=REST_HEADERS, params={"id_pendiente": f"eq.{id_pendiente}"}, timeout=15)


def borrar_registros_pendientes_de_foto(foto_bucket_path):
    """Borra cualquier pendiente de origen gasto_foto asociado a esa ruta -
    por si esta ejecucion resuelve en vivo un fallo previo de la misma foto."""
    url = f"{SUPABASE_URL}/rest/v1/registros_pendientes"
    requests.delete(
        url,
        headers=REST_HEADERS,
        params={"foto_bucket_path": f"eq.{foto_bucket_path}", "origen": "eq.gasto_foto"},
        timeout=15,
    )


def actualizar_intento_fallido(id_pendiente, intentos, error_detalle, reintentable):
    url = f"{SUPABASE_URL}/rest/v1/registros_pendientes"
    body = {
        "intentos": intentos,
        "error_detalle": error_detalle,
        "reintentable": reintentable,
        "ultimo_intento": datetime.now(timezone.utc).isoformat(),
    }
    requests.patch(url, headers=REST_HEADERS, params={"id_pendiente": f"eq.{id_pendiente}"}, json=body, timeout=15)


def marcar_alertado(id_pendiente):
    url = f"{SUPABASE_URL}/rest/v1/registros_pendientes"
    requests.patch(url, headers=REST_HEADERS, params={"id_pendiente": f"eq.{id_pendiente}"}, json={"alertado": True}, timeout=15)


def listar_etiquetas_activas():
    """Vocabulario vigente de etiquetas (consultado en vivo, nunca hardcodeado)."""
    url = f"{SUPABASE_URL}/rest/v1/etiquetas"
    params = {"estado": "eq.activa", "select": "nombre_etiqueta"}
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return sorted({fila["nombre_etiqueta"] for fila in r.json()})


GLOSARIO_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "glosario_ocr_recibos.md")


def leer_glosario():
    """
    Construye el glosario leyendo secciones conceptuales del markdown +
    tabla de etiquetas dinámicamente desde config/etiquetas.json (caché).
    
    Estrategia:
    1. Lee markdown (referencia de conceptos: principios, fallback, casos ambiguos)
    2. Extrae TODO EXCEPTO la sección "## Glosario por etiqueta"
    3. Carga etiquetas dinámicamente desde caché local (cargar_etiquetas_dinamicas)
    4. Construye tabla dinámica de keywords
    5. Recombina: conceptos + tabla dinámica = glosario completo
    
    Resultado: IDÉNTICO al glosario anterior (markdown llenado), pero tabla
    se actualiza cada 3 días automáticamente desde Supabase.
    """
    try:
        from cargar_etiquetas_dinamicas import cargar_etiquetas, construir_glosario_para_prompt
        
        # Leer markdown completo
        with open(GLOSARIO_PATH, encoding="utf-8") as f:
            contenido_md = f.read()
        
        # Partir en: [conceptos] + [tabla hardcoded que vamos a reemplazar]
        partes = contenido_md.split("## Glosario por etiqueta")
        seccion_conceptos = partes[0]  # Todo antes: principios, fallback, casos ambiguos, etc.
        
        # Cargar etiquetas dinámicamente (caché local OR fallback Supabase)
        etiquetas_dict = cargar_etiquetas()
        
        # Construir tabla dinámica de keywords
        tabla_dinamica = construir_glosario_para_prompt(etiquetas_dict)
        
        # Recombinar: conceptos + tabla dinámica = glosario COMPLETO idéntico
        glosario_completo = (
            seccion_conceptos + 
            "\n## Glosario por etiqueta\n\n" + 
            tabla_dinamica
        )
        
        print(
            f"✅ Glosario cargado dinámicamente ({len(etiquetas_dict)} etiquetas)",
            file=sys.stderr
        )
        
        return glosario_completo
        
    except ImportError as e:
        print(
            f"❌ Error importando cargar_etiquetas_dinamicas: {e}",
            file=sys.stderr
        )
        raise RuntimeError(f"No se pudo cargar el helper de etiquetas dinámicas: {e}")
    except FileNotFoundError as e:
        print(
            f"❌ Error leyendo markdown {GLOSARIO_PATH}: {e}",
            file=sys.stderr
        )
        raise RuntimeError(f"No se encontró el archivo de glosario: {e}")
    except Exception as e:
        print(
            f"❌ Error cargando glosario dinámico: {e}",
            file=sys.stderr
        )
        raise RuntimeError(f"No se pudo cargar el glosario de etiquetas: {e}")


MODELO_GEMINI = "gemini-3.5-flash"  # bajado de 3.8: modelo maduro, menos presion de demanda de lanzamiento reciente


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
