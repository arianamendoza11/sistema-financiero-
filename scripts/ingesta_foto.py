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
)

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CUENTA_SHORTCUT = os.environ.get("CUENTA") or None
FOTO_PATH = os.environ.get("FOTO_PATH") or None
CATEGORIAS_RAW = os.environ.get("CATEGORIAS_PERMITIDAS") or None

MADRID = ZoneInfo("Europe/Madrid")
MAX_LINEAS = 60  # un ticket real no supera esto, es solo un limite de cordura


def error_salir(mensaje):
    notificar_telegram(f"❌ Ingesta de foto fallida: {mensaje}")
    print(f"ERROR: {mensaje}", file=sys.stderr)
    sys.exit(1)


def descargar_foto(path):
    url = f"{SUPABASE_URL}/storage/v1/object/{path}"
    r = requests.get(url, headers=REST_HEADERS, timeout=30)
    if not r.ok:
        error_salir(f"No se pudo descargar la foto '{path}' de Supabase Storage ({r.status_code}): {r.text}")
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
            error_salir(f"La categoria preseleccionada '{codigo}' no tiene version vigente para la fecha {fecha}")
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
    try:
        return llamar_gemini_json(parts, GEMINI_API_KEY)
    except RuntimeError as e:
        error_salir(str(e))


def construir_fila(linea, id_cuenta, fecha, categorias_dict):
    codigo_categoria = linea.get("categoria")
    nombre_etiqueta = linea.get("etiqueta")
    importe = linea.get("importe")

    if not codigo_categoria or not nombre_etiqueta or importe is None:
        error_salir(f"Linea incompleta devuelta por la IA: {linea}")

    categoria = categorias_dict.get(codigo_categoria)
    if categoria is None:
        error_salir(
            f"La IA devolvio categoria '{codigo_categoria}' que no esta en la lista preseleccionada "
            f"({', '.join(categorias_dict)})"
        )

    id_etiqueta = resolver_id_etiqueta_por_nombre(nombre_etiqueta)
    if id_etiqueta is None:
        error_salir(f"La IA devolvio etiqueta '{nombre_etiqueta}' que no existe")

    signo = resolver_signo(categoria["tipo"])
    if signo is None:
        error_salir(
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


def main():
    if not CUENTA_SHORTCUT:
        error_salir("Falta 'cuenta' en el payload")
    if not FOTO_PATH:
        error_salir("Falta 'foto_bucket_path' en el payload")
    if not CATEGORIAS_RAW:
        error_salir("Falta 'categorias' (preseleccionadas en el Shortcut) en el payload")

    try:
        codigos_categoria = json.loads(CATEGORIAS_RAW)
    except json.JSONDecodeError:
        error_salir(f"'categorias' no es JSON valido: {CATEGORIAS_RAW}")

    if not isinstance(codigos_categoria, list) or not codigos_categoria:
        error_salir(f"'categorias' debe ser una lista no vacia, llego: {codigos_categoria}")

    id_cuenta = resolver_id_cuenta_por_nombre(CUENTA_SHORTCUT)
    if id_cuenta is None:
        error_salir(f"No existe ninguna cuenta llamada '{CUENTA_SHORTCUT}'")

    hoy = datetime.now(MADRID).date().isoformat()

    categorias_dict = resolver_categorias_permitidas(codigos_categoria, hoy)
    etiquetas = listar_etiquetas_activas()
    glosario_texto = leer_glosario()

    imagen_bytes = descargar_foto(FOTO_PATH)
    prompt = construir_prompt(list(categorias_dict), etiquetas, glosario_texto)
    resultado_ia = llamar_gemini(imagen_bytes, prompt)

    lineas_ia = resultado_ia.get("lineas") or []

    if not (1 <= len(lineas_ia) <= MAX_LINEAS):
        error_salir(f"La IA devolvio {len(lineas_ia)} lineas, fuera del rango valido 1-{MAX_LINEAS}")

    filas = [construir_fila(linea, id_cuenta, hoy, categorias_dict) for linea in lineas_ia]

    resultado = insertar_filas(filas)
    if not resultado.ok:
        error_salir(f"Supabase rechazo el insert ({resultado.status_code}): {resultado.text}")

    filas_insertadas = resultado.json()
    ids = ", ".join(fila["id_operacion"] for fila in filas_insertadas)
    comercio = resultado_ia.get("comercio") or "comercio no identificado"
    total = sum(abs(float(fila["importe"])) for fila in filas_insertadas)
    resumen = "\n".join(f"  - {fila['nombre_operacion']}: {fila['importe']}€" for fila in filas_insertadas)

    # La foto NO se borra aqui: se queda en el bucket al menos 1 dia para
    # dejar ventana de correccion, y la borra limpieza_recibos.py (cron diario).
    notificar_telegram(
        f"✅ Recibo de {comercio} ({hoy}) — {len(filas_insertadas)} línea(s), {total:.2f}€ total:\n"
        f"{resumen}\nIDs: {ids}"
    )
    print(f"OK: {ids}")


if __name__ == "__main__":
    main()
