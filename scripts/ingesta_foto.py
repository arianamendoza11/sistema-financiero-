# -*- coding: utf-8 -*-
"""
Gasto por foto (NFC/Manual, rama "con foto"): el Shortcut sube la imagen del
recibo a Supabase Storage (bucket 'recibos', con la key publica, solo puede
insertar) y manda cuenta + la ruta del objeto que el propio Shortcut genero
(no se descubre despues del upload, se decide antes y se reusa en las dos
llamadas HTTP).

Diseño de responsabilidades (para que la IA nunca pueda romper presupuesto):
- CATEGORIA la preselecciona Diego en el Shortcut (Choose from List, lista
  fija mantenida a mano, no consultada en vivo desde el Shortcut - un solo
  punto de escritura hacia Supabase: los scripts de Python via GitHub
  Actions, nunca el Shortcut directo). La IA solo puede asignar cada linea a
  UNA de esas categorias ya aprobadas por Diego, nunca elige libremente -
  categoria consume presupuesto, un error ahi es costoso.
- ETIQUETA la decide la IA libremente contra el vocabulario activo vigente
  (consultado en vivo a Supabase) - es solo clusterizacion para analitica,
  un error ahi es barato y facil de corregir despues a mano.
- FECHA siempre es la fecha de ejecucion del workflow (Europe/Madrid), nunca
  se le pide a la IA que la lea del ticket: un NFC se dispara en el instante
  exacto del pago, no hay nada que inferir ni que preguntar.

La logica real vive en procesar_foto() - no llama a error_salir ni hace
sys.exit, solo lanza excepciones. Esto permite que la reuse tanto main()
(disparo normal via Shortcut/GitHub) como reintentar_fotos_pendientes.py
(cron nocturno que reprocesa fallos de Gemini) sin duplicar nada.

Sin RUN2, sin botones, sin webhook: se inserta directo y Telegram notifica
el resultado. Si algo sale mal se corrige a mano despues (via Supabase MCP),
igual que el resto del sistema.
"""
import base64
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from motor_supabase import (
    SUPABASE_URL,
    REST_HEADERS,
    notificar_telegram,
    resolver_categoria,
    resolver_id_cuenta_por_nombre,
    resolver_id_etiqueta_por_nombre,
    resolver_signo,
    insertar_filas,
    listar_etiquetas_activas,
    leer_glosario,
    llamar_gemini_json,
    guardar_foto_pendiente,
    borrar_foto_pendiente,
)

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CUENTA_SHORTCUT = os.environ.get("CUENTA") or None
FOTO_PATH = os.environ.get("FOTO_PATH") or None
CATEGORIAS_RAW = os.environ.get("CATEGORIAS_PERMITIDAS") or None

MADRID = ZoneInfo("Europe/Madrid")
MAX_LINEAS = 60  # un ticket real no supera esto, es solo un limite de cordura


class FotoNoEncontrada(RuntimeError):
    """La foto ya no existe en Storage (se cumplio la retencion antes de
    poder procesarla) - no tiene sentido seguir reintentando esta."""


def descargar_foto(path):
    url = f"{SUPABASE_URL}/storage/v1/object/{path}"
    r = requests.get(url, headers=REST_HEADERS, timeout=30)
    if r.status_code == 404:
        raise FotoNoEncontrada(f"La foto '{path}' ya no existe en Supabase Storage")
    if not r.ok:
        raise RuntimeError(f"No se pudo descargar la foto '{path}' de Supabase Storage ({r.status_code}): {r.text}")
    return r.content


def resolver_categorias_permitidas(codigos, fecha):
    """Valida en vivo contra Supabase cada codigo_categoria que Diego
    preselecciono en el Shortcut. Devuelve {codigo: categoria_info}. Si un
    codigo ya no esta vigente (ej. la lista fija del Shortcut quedo
    desactualizada), falla explicito antes de gastar una llamada a Gemini."""
    resueltas = {}
    for codigo in codigos:
        categoria = resolver_categoria(codigo, fecha)
        if categoria is None:
            raise RuntimeError(f"La categoria preseleccionada '{codigo}' no tiene version vigente para la fecha {fecha}")
        resueltas[codigo] = categoria
    return resueltas


def construir_prompt(categorias_permitidas, etiquetas, glosario_texto):
    return f"""Eres el motor de OCR y categorizacion de tickets de compra de un sistema financiero personal.

Analiza la foto del ticket adjunto y devuelve EXCLUSIVAMENTE un JSON (sin markdown, sin explicacion, sin texto fuera del JSON) con esta forma exacta:

{{
  "comercio": "nombre del comercio tal como aparece en el ticket",
  "lineas": [
    {{
      "importe": 1.15,
      "categoria": "codigo_categoria exacto de la lista permitida",
      "etiqueta": "nombre_etiqueta exacto de la lista permitida",
      "nombre_operacion": "nombre corto y claro del producto",
      "comentario": "texto original de la linea tal como aparece en el ticket"
    }}
  ]
}}

Ejemplo de una respuesta valida (un ticket de supermercado con 2 productos):

{{
  "comercio": "Lidl",
  "lineas": [
    {{"importe": 3.45, "categoria": "alimentacion", "etiqueta": "despensa", "nombre_operacion": "Pan integral", "comentario": "PAN INTEGRAL 500G"}},
    {{"importe": 5.90, "categoria": "alimentacion", "etiqueta": "cerveza o copa", "nombre_operacion": "Ron", "comentario": "RON CARTA 70CL"}}
  ]
}}

Reglas obligatorias:
- "categoria" DEBE ser EXACTAMENTE uno de los codigos permitidos abajo. Diego ya preselecciono estas categorias a mano antes de mandar la foto porque afectan su presupuesto - nunca uses una categoria fuera de esta lista, ni siquiera si crees que existe otra mas precisa en el sistema. Si de verdad ninguna de las permitidas encaja con una linea, usa la que mas se acerque semanticamente, nunca inventes una nueva.
- "etiqueta" SI la eliges libremente (para eso tienes la lista completa de etiquetas activas abajo) - debe ser EXACTAMENTE uno de esos valores.
- Se clasifica cada linea por lo que ES el producto, nunca por el comercio de origen. Un supermercado puede vender alcohol, limpieza, tecnologia o comida en el mismo ticket - cada linea se juzga sola.
- Neta los descuentos/promos/cupones/devoluciones en el precio final de la linea del producto al que corresponden. No generes una linea aparte solo para un descuento.
- "importe" siempre en positivo (numero).
- Si un producto no aparece en el glosario de referencia, usa tu propio criterio semantico para elegir la etiqueta que mejor encaje - nunca dejes una linea sin clasificar.

Categorias permitidas (preseleccionadas por Diego, cerradas): {', '.join(categorias_permitidas)}

Etiquetas permitidas (elige libremente): {', '.join(etiquetas)}

Glosario de referencia (sugestivo, no exhaustivo):
{glosario_texto}
"""


def llamar_gemini(imagen_bytes, prompt):
    parts = [
        {"text": prompt},
        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(imagen_bytes).decode()}},
    ]
    return llamar_gemini_json(parts, GEMINI_API_KEY)  # deja propagar RuntimeError


def construir_fila(linea, id_cuenta, fecha, categorias_dict):
    codigo_categoria = linea.get("categoria")
    nombre_etiqueta = linea.get("etiqueta")
    importe = linea.get("importe")

    if not codigo_categoria or not nombre_etiqueta or importe is None:
        raise RuntimeError(f"Linea incompleta devuelta por la IA: {linea}")

    categoria = categorias_dict.get(codigo_categoria)
    if categoria is None:
        raise RuntimeError(
            f"La IA devolvio categoria '{codigo_categoria}' que no esta en la lista preseleccionada "
            f"({', '.join(categorias_dict)})"
        )

    id_etiqueta = resolver_id_etiqueta_por_nombre(nombre_etiqueta)
    if id_etiqueta is None:
        raise RuntimeError(f"La IA devolvio etiqueta '{nombre_etiqueta}' que no existe")

    signo = resolver_signo(categoria["tipo"])
    if signo is None:
        raise RuntimeError(
            f"La categoria '{codigo_categoria}' es de tipo '{categoria['tipo']}', "
            "que no se puede registrar en una sola linea (ej. un traspaso necesita 2 filas ligadas)"
        )

    return {
        "nombre_operacion": linea.get("nombre_operacion") or nombre_etiqueta.capitalize(),
        "importe": abs(float(importe)) * signo,
        "fecha_operacion": fecha,
        "id_cuenta": id_cuenta,
        "id_categoria": categoria["id_categoria"],
        "id_etiqueta": id_etiqueta,
        "comentario": linea.get("comentario"),
        "tipo": categoria["tipo"],
    }


def procesar_foto(cuenta_nombre, codigos_categoria, foto_path):
    """Hace todo el trabajo real. Devuelve (filas_insertadas, comercio,
    fecha) en exito. Lanza FotoNoEncontrada o RuntimeError en fallo - nunca
    llama a Telegram ni hace sys.exit, eso lo decide el llamador."""
    id_cuenta = resolver_id_cuenta_por_nombre(cuenta_nombre)
    if id_cuenta is None:
        raise RuntimeError(f"No existe ninguna cuenta llamada '{cuenta_nombre}'")

    hoy = datetime.now(MADRID).date().isoformat()

    categorias_dict = resolver_categorias_permitidas(codigos_categoria, hoy)
    etiquetas = listar_etiquetas_activas()
    glosario_texto = leer_glosario()

    imagen_bytes = descargar_foto(foto_path)
    prompt = construir_prompt(list(categorias_dict), etiquetas, glosario_texto)
    resultado_ia = llamar_gemini(imagen_bytes, prompt)

    lineas_ia = resultado_ia.get("lineas") or []
    if not (1 <= len(lineas_ia) <= MAX_LINEAS):
        raise RuntimeError(f"La IA devolvio {len(lineas_ia)} lineas, fuera del rango valido 1-{MAX_LINEAS}")

    filas = [construir_fila(linea, id_cuenta, hoy, categorias_dict) for linea in lineas_ia]

    resultado = insertar_filas(filas)
    if not resultado.ok:
        raise RuntimeError(f"Supabase rechazo el insert ({resultado.status_code}): {resultado.text}")

    comercio = resultado_ia.get("comercio") or "comercio no identificado"
    return resultado.json(), comercio, hoy


def notificar_exito(filas_insertadas, comercio, fecha):
    ids = ", ".join(fila["id_operacion"] for fila in filas_insertadas)
    total = sum(abs(float(fila["importe"])) for fila in filas_insertadas)
    resumen = "\n".join(f"  - {fila['nombre_operacion']}: {fila['importe']}€" for fila in filas_insertadas)
    notificar_telegram(
        f"✅ Recibo de {comercio} ({fecha}) — {len(filas_insertadas)} línea(s), {total:.2f}€ total:\n"
        f"{resumen}\nIDs: {ids}"
    )
    print(f"OK: {ids}")


def main():
    if not CUENTA_SHORTCUT:
        notificar_telegram("❌ Ingesta de foto fallida: Falta 'cuenta' en el payload")
        sys.exit(1)
    if not FOTO_PATH:
        notificar_telegram("❌ Ingesta de foto fallida: Falta 'foto_bucket_path' en el payload")
        sys.exit(1)
    if not CATEGORIAS_RAW:
        notificar_telegram("❌ Ingesta de foto fallida: Falta 'categorias' (preseleccionadas en el Shortcut) en el payload")
        sys.exit(1)

    try:
        codigos_categoria = json.loads(CATEGORIAS_RAW)
    except json.JSONDecodeError:
        notificar_telegram(f"❌ Ingesta de foto fallida: 'categorias' no es JSON valido: {CATEGORIAS_RAW}")
        sys.exit(1)

    if not isinstance(codigos_categoria, list) or not codigos_categoria:
        notificar_telegram(f"❌ Ingesta de foto fallida: 'categorias' debe ser una lista no vacia, llego: {codigos_categoria}")
        sys.exit(1)

    try:
        filas_insertadas, comercio, fecha = procesar_foto(CUENTA_SHORTCUT, codigos_categoria, FOTO_PATH)
    except Exception as e:
        # La foto NO se borra: se queda en el bucket (7 dias) para que el
        # cron nocturno de reintentos la vuelva a intentar solo.
        intentos = guardar_foto_pendiente(FOTO_PATH, CUENTA_SHORTCUT, codigos_categoria)
        notificar_telegram(
            f"❌ Ingesta de foto fallida (intento {intentos}): {e}\n"
            f"------------------------------------------\n"
            f"Se guardo como pendiente - se reintenta solo cada noche mientras la foto siga "
            f"disponible. No hace falta que hagas nada."
        )
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    borrar_foto_pendiente(FOTO_PATH)  # por si esta ejecucion resuelve un pendiente previo
    notificar_exito(filas_insertadas, comercio, fecha)


if __name__ == "__main__":
    main()
