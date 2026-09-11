# -*- coding: utf-8 -*-
"""
Ingesta estandarizada: recibe un payload minimo (tipo_ingesta, fecha, y solo
lo que la plantilla marque como variable) via repository_dispatch, resuelve
todo lo demas contra config/plantillas_estandarizadas.json + Supabase, e
inserta en seguimiento_efectivo. Notifica el resultado por Telegram.

Nunca asume un dato que se pueda resolver por otra via: id_categoria siempre
se resuelve por codigo_categoria + vigencia en Supabase (SCD2), medio_pago lo
pone el trigger de la base a partir de la cuenta, tipo siempre es el de la
categoria resuelta.
"""
import json
import os
import sys

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

TIPO_INGESTA = os.environ.get("TIPO_INGESTA") or None
FECHA = os.environ.get("FECHA") or None
MONTO = os.environ.get("MONTO") or None
CUENTA_SHORTCUT = os.environ.get("CUENTA") or None
COMENTARIO_SHORTCUT = os.environ.get("COMENTARIO") or None

PLANTILLAS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "plantillas_estandarizadas.json"
)

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


def error_salir(mensaje):
    notificar_telegram(f"❌ Ingesta '{TIPO_INGESTA}' fallida: {mensaje}")
    print(f"ERROR: {mensaje}", file=sys.stderr)
    sys.exit(1)


def cargar_version_vigente(tipo_ingesta, fecha):
    with open(PLANTILLAS_PATH, encoding="utf-8") as f:
        plantillas = json.load(f)

    versiones = plantillas.get(tipo_ingesta)
    if not versiones:
        error_salir(f"tipo_ingesta '{tipo_ingesta}' no existe en plantillas_estandarizadas.json")

    for version in versiones:
        inicio = version["fecha_inicio"]
        fin = version.get("fecha_fin")
        if inicio <= fecha and (fin is None or fecha <= fin):
            return version

    error_salir(f"No hay version vigente de la plantilla '{tipo_ingesta}' para la fecha {fecha}")


def resolver_categoria(codigo_categoria, fecha):
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
    if not filas:
        error_salir(f"No hay version vigente de la categoria '{codigo_categoria}' para la fecha {fecha}")
    return filas[0]


def calcular_comentario(version, categoria):
    if "comentario_fijo" in version:
        return version["comentario_fijo"]

    if "comentario_regla" in version:
        regla = version["comentario_regla"]
        if regla == "condicional_umbral_8":
            if MONTO is None:
                error_salir("La regla de comentario 'condicional_umbral_8' necesita 'monto' y no llego")
            return "Pago con adicionales" if abs(float(MONTO)) > 8 else "Pago exacto"
        error_salir(f"Regla de comentario desconocida: {regla}")

    if "comentario_fuente" in version:
        fuente = version["comentario_fuente"]
        if fuente == "categorias.glosa":
            return categoria.get("glosa")
        error_salir(f"Fuente de comentario desconocida: {fuente}")

    if version.get("comentario_desde_shortcut"):
        if not COMENTARIO_SHORTCUT:
            error_salir("Este tipo de ingesta requiere 'comentario' desde el Shortcut y no llego")
        return COMENTARIO_SHORTCUT

    return None


def resolver_monto(version):
    if version["importe_fijo"] is not None:
        return version["importe_fijo"]
    if MONTO is None:
        error_salir("Este tipo de ingesta requiere 'monto' desde el Shortcut y no llego")
    return abs(float(MONTO)) * version["signo"]


def resolver_id_cuenta_por_nombre(nombre_cuenta):
    url = f"{SUPABASE_URL}/rest/v1/cuentas"
    params = {"nombre_cuenta": f"eq.{nombre_cuenta}", "select": "id_cuenta"}
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    filas = r.json()
    if not filas:
        error_salir(f"No existe ninguna cuenta llamada '{nombre_cuenta}'")
    return filas[0]["id_cuenta"]


def resolver_cuenta(id_cuenta_plantilla):
    if id_cuenta_plantilla is not None:
        return id_cuenta_plantilla
    if not CUENTA_SHORTCUT:
        error_salir("Este tipo de ingesta requiere 'cuenta' (nombre, ej. 'BBVA Esp') desde el Shortcut y no llego")
    return resolver_id_cuenta_por_nombre(CUENTA_SHORTCUT)


def construir_fila_simple(version, fecha):
    categoria = resolver_categoria(version["codigo_categoria"], fecha)
    return {
        "nombre_operacion": version["nombre_operacion"],
        "importe": resolver_monto(version),
        "fecha_operacion": fecha,
        "id_cuenta": resolver_cuenta(version["id_cuenta"]),
        "id_categoria": categoria["id_categoria"],
        "id_etiqueta": version["id_etiqueta"],
        "comentario": calcular_comentario(version, categoria),
        "tipo": categoria["tipo"],
    }


def construir_filas_ahorro(version, fecha):
    if MONTO is None:
        error_salir("El ahorro requiere 'monto' desde el Shortcut y no llego")
    monto = abs(float(MONTO))

    categoria = resolver_categoria(version["codigo_categoria"], fecha)
    comentario = calcular_comentario(version, categoria)

    filas = []
    for fila_def in version["filas"]:
        filas.append({
            "nombre_operacion": fila_def["nombre_operacion"],
            "importe": monto * fila_def["signo"],
            "fecha_operacion": fecha,
            "id_cuenta": resolver_cuenta(fila_def["id_cuenta"]),
            "id_categoria": categoria["id_categoria"],
            "id_etiqueta": version["id_etiqueta"],
            "comentario": comentario,
            "tipo": categoria["tipo"],
        })
    return filas


def insertar_filas(filas):
    url = f"{SUPABASE_URL}/rest/v1/seguimiento_efectivo"
    r = requests.post(url, headers=REST_HEADERS, json=filas, timeout=15)
    if not r.ok:
        error_salir(f"Supabase rechazo el insert ({r.status_code}): {r.text}")
    return r.json()


def main():
    if not TIPO_INGESTA:
        error_salir("Falta 'tipo_ingesta' en el payload")
    if not FECHA:
        error_salir("Falta 'fecha' en el payload (nunca hay fecha por defecto)")

    version = cargar_version_vigente(TIPO_INGESTA, FECHA)

    if "filas" in version:
        filas = construir_filas_ahorro(version, FECHA)
    else:
        filas = [construir_fila_simple(version, FECHA)]

    resultado = insertar_filas(filas)
    ids = ", ".join(fila["id_operacion"] for fila in resultado)
    notificar_telegram(f"✅ Ingesta '{TIPO_INGESTA}' registrada: {ids}")
    print(f"OK: {ids}")


if __name__ == "__main__":
    main()
