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

La logica real vive en procesar_ingesta_estandarizada() - no llama a
Telegram ni hace sys.exit, solo lanza excepciones. Esto permite que la reuse
tanto main() como reintentar_registros_pendientes.py, mismo patron que
procesar_foto() en ingesta_foto.py.
"""
import json
import os
import sys

from motor_supabase import (
    notificar_telegram,
    resolver_categoria,
    resolver_id_cuenta_por_nombre,
    resolver_nombre_etiqueta_por_id,
    insertar_filas,
    guardar_registro_pendiente,
    es_error_reintentable,
)

TIPO_INGESTA = os.environ.get("TIPO_INGESTA") or None
FECHA = os.environ.get("FECHA") or None
MONTO = os.environ.get("MONTO") or None
CUENTA_SHORTCUT = os.environ.get("CUENTA") or None
COMENTARIO_SHORTCUT = os.environ.get("COMENTARIO") or None

PLANTILLAS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "config", "plantillas_estandarizadas.json"
)


def cargar_version_vigente(tipo_ingesta, fecha):
    with open(PLANTILLAS_PATH, encoding="utf-8") as f:
        plantillas = json.load(f)

    versiones = plantillas.get(tipo_ingesta)
    if not versiones:
        raise ValueError(f"tipo_ingesta '{tipo_ingesta}' no existe en plantillas_estandarizadas.json")

    for version in versiones:
        inicio = version["fecha_inicio"]
        fin = version.get("fecha_fin")
        if inicio <= fecha and (fin is None or fecha <= fin):
            return version

    raise ValueError(f"No hay version vigente de la plantilla '{tipo_ingesta}' para la fecha {fecha}")


def resolver_categoria_o_falla(codigo_categoria, fecha):
    categoria = resolver_categoria(codigo_categoria, fecha)
    if categoria is None:
        raise ValueError(f"No hay version vigente de la categoria '{codigo_categoria}' para la fecha {fecha}")
    return categoria


def calcular_comentario(version, categoria, monto, comentario_shortcut):
    if "comentario_fijo" in version:
        return version["comentario_fijo"]

    if "comentario_regla" in version:
        regla = version["comentario_regla"]
        if regla == "condicional_umbral_8":
            if monto is None:
                raise ValueError("La regla de comentario 'condicional_umbral_8' necesita 'monto' y no llego")
            return "Pago con adicionales" if abs(float(monto)) > 8 else "Pago exacto"
        raise ValueError(f"Regla de comentario desconocida: {regla}")

    if "comentario_fuente" in version:
        fuente = version["comentario_fuente"]
        if fuente == "categorias.glosa":
            return categoria.get("glosa")
        raise ValueError(f"Fuente de comentario desconocida: {fuente}")

    if version.get("comentario_desde_shortcut"):
        if not comentario_shortcut:
            raise ValueError("Este tipo de ingesta requiere 'comentario' desde el Shortcut y no llego")
        return comentario_shortcut

    return None


def resolver_monto(version, monto):
    if version["importe_fijo"] is not None:
        return version["importe_fijo"]
    if monto is None:
        raise ValueError("Este tipo de ingesta requiere 'monto' desde el Shortcut y no llego")
    return abs(float(monto)) * version["signo"]


def resolver_cuenta(id_cuenta_plantilla, cuenta_shortcut):
    if id_cuenta_plantilla is not None:
        return id_cuenta_plantilla
    if not cuenta_shortcut:
        raise ValueError("Este tipo de ingesta requiere 'cuenta' (nombre, ej. 'BBVA Esp') desde el Shortcut y no llego")
    id_cuenta = resolver_id_cuenta_por_nombre(cuenta_shortcut)
    if id_cuenta is None:
        raise ValueError(f"No existe ninguna cuenta llamada '{cuenta_shortcut}'")
    return id_cuenta


def construir_fila_simple(version, fecha, monto, cuenta_shortcut, comentario_shortcut):
    categoria = resolver_categoria_o_falla(version["codigo_categoria"], fecha)
    return {
        "nombre_operacion": version["nombre_operacion"],
        "importe": resolver_monto(version, monto),
        "fecha_operacion": fecha,
        "id_cuenta": resolver_cuenta(version["id_cuenta"], cuenta_shortcut),
        "id_categoria": categoria["id_categoria"],
        "id_etiqueta": version["id_etiqueta"],
        "comentario": calcular_comentario(version, categoria, monto, comentario_shortcut),
        "tipo": categoria["tipo"],
    }


def construir_filas_ahorro(version, fecha, monto, cuenta_shortcut, comentario_shortcut):
    if monto is None:
        raise ValueError("El ahorro requiere 'monto' desde el Shortcut y no llego")
    monto_abs = abs(float(monto))

    categoria = resolver_categoria_o_falla(version["codigo_categoria"], fecha)
    comentario = calcular_comentario(version, categoria, monto, comentario_shortcut)

    filas = []
    for fila_def in version["filas"]:
        filas.append({
            "nombre_operacion": fila_def["nombre_operacion"],
            "importe": monto_abs * fila_def["signo"],
            "fecha_operacion": fecha,
            "id_cuenta": resolver_cuenta(fila_def["id_cuenta"], cuenta_shortcut),
            "id_categoria": categoria["id_categoria"],
            "id_etiqueta": version["id_etiqueta"],
            "comentario": comentario,
            "tipo": categoria["tipo"],
        })
    return filas


def procesar_ingesta_estandarizada(tipo_ingesta, fecha, monto, cuenta_shortcut, comentario_shortcut):
    """Hace todo el trabajo real a partir del payload crudo del Shortcut.
    Lanza ValueError/RuntimeError en cualquier fallo - nunca llama a
    Telegram ni hace sys.exit, eso lo decide el llamador."""
    if not tipo_ingesta:
        raise ValueError("Falta 'tipo_ingesta' en el payload")
    if not fecha:
        raise ValueError("Falta 'fecha' en el payload (nunca hay fecha por defecto)")

    version = cargar_version_vigente(tipo_ingesta, fecha)

    if "filas" in version:
        filas = construir_filas_ahorro(version, fecha, monto, cuenta_shortcut, comentario_shortcut)
    else:
        filas = [construir_fila_simple(version, fecha, monto, cuenta_shortcut, comentario_shortcut)]

    resultado = insertar_filas(filas)
    if not resultado.ok:
        raise RuntimeError(f"Supabase rechazo el insert ({resultado.status_code}): {resultado.text}")

    return resultado.json(), filas[0]["id_etiqueta"]


def notificar_exito(tipo_ingesta, filas_insertadas, id_etiqueta):
    etiqueta = resolver_nombre_etiqueta_por_id(id_etiqueta) or id_etiqueta
    resumen = "\n".join(
        "  - {tipo}: {nombre} — {importe}€ — etiqueta: {etiqueta}".format(
            tipo="Ingreso" if float(fila["importe"]) > 0 else "Gasto",
            nombre=fila["nombre_operacion"],
            importe=fila["importe"],
            etiqueta=etiqueta,
        )
        for fila in filas_insertadas
    )
    ids = ", ".join(fila["id_operacion"] for fila in filas_insertadas)
    notificar_telegram(f"✅ Ingesta '{tipo_ingesta}' registrada:\n{resumen}\nIDs: {ids}")
    print(f"OK: {ids}")


def main():
    payload = {
        "tipo_ingesta": TIPO_INGESTA,
        "fecha": FECHA,
        "monto": MONTO,
        "cuenta": CUENTA_SHORTCUT,
        "comentario": COMENTARIO_SHORTCUT,
    }

    try:
        filas_insertadas, id_etiqueta = procesar_ingesta_estandarizada(
            TIPO_INGESTA, FECHA, MONTO, CUENTA_SHORTCUT, COMENTARIO_SHORTCUT
        )
    except Exception as e:
        reintentable = es_error_reintentable(e)
        id_pendiente = guardar_registro_pendiente(
            "ingesta_estandarizada", payload, error_detalle=str(e), reintentable=reintentable
        )
        nota = (
            "Se guardo como pendiente, se reintenta solo cada noche."
            if reintentable
            else "NO se va a reintentar solo (parece un error de datos/configuracion, no transitorio) - hace falta arreglarlo a mano."
        )
        notificar_telegram(f"❌ Ingesta '{TIPO_INGESTA}' fallida ({id_pendiente}): {e}\n{nota}")
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    notificar_exito(TIPO_INGESTA, filas_insertadas, id_etiqueta)


if __name__ == "__main__":
    main()
