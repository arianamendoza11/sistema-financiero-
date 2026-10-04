# -*- coding: utf-8 -*-
"""
Funciones compartidas por todos los scripts de ingesta: resolver datos contra
Supabase, notificar por Telegram y llamar a la IA de vision (Qwen/DashScope).
No contiene logica de negocio de ningun tipo de ingesta en particular - eso
vive en cada script (ingesta_estandarizada, ingesta_dinamica, ingesta_foto).
"""
import base64
import json
import os
import sys
import time
import unicodedata
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


def normalizar(texto):
    """minuscula, sin acentos, espacios colapsados - lo hace el codigo, no Ariana.
    Tolera como se escribe de verdad ("Pechuga de POLLO" matchea keyword "pollo").
    Lo comparten el matcher del gasto dinamico y el del gasto por foto."""
    if not texto:
        return ""
    t = unicodedata.normalize("NFKD", str(texto))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return " ".join(t.lower().split())


SIGNO_POR_TIPO_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "signo_por_tipo.json")
with open(SIGNO_POR_TIPO_PATH, encoding="utf-8") as _f:
    _SIGNO_POR_TIPO = json.load(_f)


def resolver_signo(tipo):
    """Devuelve +1/-1 segun config/signo_por_tipo.json para el 'tipo' de una
    categoria (ingreso, gasto, etc.), o None si ese tipo no esta permitido en
    un registro de una sola linea (ej. 'traspaso' necesita 2 filas ligadas)."""
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
    "no tiene etiquetas asociadas",
    "no cuadran con el total",
)


def es_error_reintentable(excepcion):
    """Decide si vale la pena que el reproceso nocturno reintente este fallo.
    Transitorio (IA/Supabase saturado o caido, timeout de red) -> True.
    Estructural (dato invalido, config faltante, payload malformado) -> False.
    Ante la duda, True - reintentar de mas sale barato, de menos pierde datos."""
    if isinstance(excepcion, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True

    mensaje = str(excepcion)
    if any(codigo in mensaje for codigo in ("503", "504", "502", "500", "429")):
        return True
    if any(patron in mensaje for patron in _PATRONES_NO_REINTENTABLES):
        return False

    return True


def guardar_registro_pendiente(origen, payload, foto_bucket_path=None, error_detalle=None, reintentable=True):
    """Inserta un registro pendiente nuevo a partir de un fallo recien ocurrido.
    'Estar en esta tabla' ES el estado de 'pendiente'. Devuelve el id_pendiente."""
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
    (origen gasto_foto). Lo usa limpieza_recibos.py para nunca borrar una foto
    que el sistema todavia necesita."""
    url = f"{SUPABASE_URL}/rest/v1/registros_pendientes"
    params = {"origen": "eq.gasto_foto", "select": "foto_bucket_path"}
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return {fila["foto_bucket_path"] for fila in r.json() if fila["foto_bucket_path"]}


def borrar_registro_pendiente(id_pendiente):
    url = f"{SUPABASE_URL}/rest/v1/registros_pendientes"
    requests.delete(url, headers=REST_HEADERS, params={"id_pendiente": f"eq.{id_pendiente}"}, timeout=15)


def borrar_registros_pendientes_de_foto(foto_bucket_path):
    """Borra cualquier pendiente de origen gasto_foto asociado a esa ruta."""
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
    """Vocabulario vigente de etiquetas (nombres), consultado en vivo."""
    url = f"{SUPABASE_URL}/rest/v1/etiquetas"
    params = {"estado": "eq.activa", "select": "nombre_etiqueta"}
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return sorted({fila["nombre_etiqueta"] for fila in r.json()})


# --- IA de vision: Qwen via DashScope (endpoint OpenAI-compatible) ---
# QWEN_BASE_URL (workspace compatible-mode) va como secret en GitHub Actions.
# El MODELO va aqui en el script como parametro (no como secret - no aporta
# tenerlo fuera). Opciones: qwen3-vl-flash ($0.05/$0.40 por millon, el mas
# barato) o qwen3-vl-plus ($0.20/$1.60). El prompt de foto esta disenado para
# que flash rinda: menu numerado acotado a las categorias del Shortcut, glosario
# que se impone en codigo y verificacion de importes contra el TOTAL.
QWEN_BASE_URL = (os.environ.get("QWEN_BASE_URL") or "").rstrip("/")
MODELO_QWEN = "qwen3-vl-flash"

MAX_INTENTOS_QWEN = 3    # intentos totales (1 inicial + 2 reintentos)
ESPERA_QWEN_5XX_SEG = 5  # 5xx = servidores saturados
ESPERA_QWEN_429_SEG = 20  # 429 = rate limit


def _parsear_json_qwen(texto):
    """Parsea el JSON que devuelve el modelo, tolerando que lo envuelva en
    fences markdown (```json ... ```) o texto alrededor."""
    t = (texto or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.strip()
    candidatos = [t]
    if "{" in t and "}" in t:
        candidatos.append(t[t.find("{"): t.rfind("}") + 1])
    for candidato in candidatos:
        try:
            return json.loads(candidato)
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f"Qwen no devolvio JSON valido: {texto}")


def llamar_qwen_json(prompt, imagen_bytes=None, api_key=None, sistema=None):
    """Llama a Qwen (DashScope, endpoint OpenAI-compatible /chat/completions).
    'sistema' (opcional) va como mensaje de sistema con las reglas; 'prompt' es
    la instruccion del usuario y la imagen (opcional) va antes que el texto, como
    recomienda Qwen-VL. temperature=0: lectura de tickets, no creatividad.
    Devuelve el JSON parseado. Lanza RuntimeError con mensaje claro si algo falla.

    Reintenta ante codigos transitorios (5xx y 429); cualquier otro falla directo.
    El reintento inmediato es SOLO para la IA - Supabase no se reintenta (si cae,
    la operacion deriva a pendientes, por decision de diseno)."""
    if not QWEN_BASE_URL:
        raise RuntimeError("Falta QWEN_BASE_URL en el entorno (endpoint compatible-mode de DashScope)")
    if not api_key:
        raise RuntimeError("Falta DASHSCOPE_API_KEY en el entorno")

    url = f"{QWEN_BASE_URL}/chat/completions"
    content = []
    if imagen_bytes is not None:
        b64 = base64.b64encode(imagen_bytes).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })
    content.append({"type": "text", "text": prompt})

    messages = [{"role": "system", "content": sistema}] if sistema else []
    messages.append({"role": "user", "content": content})

    body = {"model": MODELO_QWEN, "messages": messages, "temperature": 0}
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    r = None
    for intento in range(1, MAX_INTENTOS_QWEN + 1):
        r = requests.post(url, headers=headers, json=body, timeout=90)
        if r.ok or intento == MAX_INTENTOS_QWEN:
            break
        if r.status_code in (500, 502, 503, 504):
            time.sleep(ESPERA_QWEN_5XX_SEG)
            continue
        if r.status_code == 429:
            time.sleep(ESPERA_QWEN_429_SEG)
            continue
        break  # cualquier otro codigo: reintentar no lo arregla

    if not r.ok:
        raise RuntimeError(f"Qwen rechazo la peticion ({r.status_code}): {r.text}")

    data = r.json()
    try:
        texto = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"Respuesta de Qwen con forma inesperada: {data}")

    return _parsear_json_qwen(texto)
