# -*- coding: utf-8 -*-
"""
Gasto dinamico: NFC o Hub Manual mandan una cuenta compartida, opcionalmente
una fecha, y 1-3 lineas de gasto (importe + categoria + etiqueta elegidos
libremente, no vienen de una plantilla fija). Cada linea se resuelve contra
Supabase igual que la ingesta estandarizada (misma vigencia SCD2 de
categorias, mismo trigger de medio_pago) y se inserta en un solo INSERT
atomico.

Si no llega 'fecha' se usa la fecha de ejecucion del workflow (hora de
Madrid) - pensado para NFC, que se dispara en el instante exacto del pago y
no tiene sentido que pregunte fecha.
"""
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from motor_supabase import (
    notificar_telegram,
    resolver_categoria,
    resolver_id_cuenta_por_nombre,
    resolver_id_etiqueta_por_nombre,
    resolver_signo,
    insertar_filas,
)

CUENTA_SHORTCUT = os.environ.get("CUENTA") or None
FECHA_SHORTCUT = os.environ.get("FECHA") or None
LINEAS_RAW = os.environ.get("LINEAS") or None

MADRID = ZoneInfo("Europe/Madrid")


def error_salir(mensaje):
    notificar_telegram(f"❌ Gasto dinamico fallido: {mensaje}")
    print(f"ERROR: {mensaje}", file=sys.stderr)
    sys.exit(1)


def resolver_fecha():
    if FECHA_SHORTCUT:
        return FECHA_SHORTCUT
    return datetime.now(MADRID).date().isoformat()


def construir_fila(linea, id_cuenta, fecha):
    codigo_categoria = linea.get("categoria")
    nombre_etiqueta = linea.get("etiqueta")
    importe = linea.get("importe")

    if not codigo_categoria or not nombre_etiqueta or importe is None:
        error_salir(f"Linea incompleta, falta categoria/etiqueta/importe: {linea}")

    categoria = resolver_categoria(codigo_categoria, fecha)
    if categoria is None:
        error_salir(f"No hay version vigente de la categoria '{codigo_categoria}' para la fecha {fecha}")

    id_etiqueta = resolver_id_etiqueta_por_nombre(nombre_etiqueta)
    if id_etiqueta is None:
        error_salir(f"No existe ninguna etiqueta llamada '{nombre_etiqueta}'")

    signo = resolver_signo(categoria["tipo"])
    if signo is None:
        error_salir(
            f"La categoria '{codigo_categoria}' es de tipo '{categoria['tipo']}', "
            "que no se puede registrar en una sola linea (ej. un traspaso necesita 2 filas ligadas)"
        )

    nombre_operacion = linea.get("nombre_operacion") or nombre_etiqueta.capitalize()

    return {
        "nombre_operacion": nombre_operacion,
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
    if not LINEAS_RAW:
        error_salir("Falta 'lineas' en el payload")

    try:
        lineas = json.loads(LINEAS_RAW)
    except json.JSONDecodeError:
        error_salir(f"'lineas' no es JSON valido: {LINEAS_RAW}")

    if not isinstance(lineas, list) or not (1 <= len(lineas) <= 3):
        cantidad = len(lineas) if isinstance(lineas, list) else "datos invalidos"
        error_salir(f"'lineas' debe ser una lista de 1 a 3 gastos, llego: {cantidad}")

    id_cuenta = resolver_id_cuenta_por_nombre(CUENTA_SHORTCUT)
    if id_cuenta is None:
        error_salir(f"No existe ninguna cuenta llamada '{CUENTA_SHORTCUT}'")

    fecha = resolver_fecha()

    filas = [construir_fila(linea, id_cuenta, fecha) for linea in lineas]

    resultado = insertar_filas(filas)
    if not resultado.ok:
        error_salir(f"Supabase rechazo el insert ({resultado.status_code}): {resultado.text}")

    filas_insertadas = resultado.json()
    ids = ", ".join(fila["id_operacion"] for fila in filas_insertadas)
    notificar_telegram(f"✅ Gasto registrado ({fecha}): {ids}")
    print(f"OK: {ids}")


if __name__ == "__main__":
    main()
