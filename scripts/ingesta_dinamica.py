# -*- coding: utf-8 -*-
"""
Gasto dinamico (NFC / Hub Manual): una cuenta compartida, opcionalmente una
fecha, y 1-3 lineas de gasto (importe + categoria + un texto corto que describe
la compra). La categoria la elige Ariana en el Shortcut (afecta presupuesto).

La ETIQUETA ya NO la decide una IA. Se resuelve por KEYWORD MATCHING
determinista contra el vocabulario vigente de `etiquetas` (consultado en vivo a
Supabase, sin cache), acotado por la categoria elegida (posibles_categorias).
Si ninguna keyword coincide, la operacion se deriva a registros_pendientes para
que la rutina nocturna (Claude) la clasifique. Instantaneo, gratis y sin red
para la etiqueta.

Si no llega 'fecha' se usa la fecha de ejecucion del workflow (hora de Madrid)
- pensado para NFC, que se dispara en el instante exacto del pago.

La logica real vive en procesar_gasto_dinamico() - no llama a Telegram ni hace
sys.exit, solo lanza excepciones. Esto permite reusarla desde main() (disparo
normal); si falla, el pendiente lo procesa la rutina nocturna de Claude.
"""
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
    normalizar,
    resolver_categoria,
    resolver_id_cuenta_por_nombre,
    resolver_signo,
    insertar_filas,
    guardar_registro_pendiente,
    NOTA_PENDIENTE,
)

CUENTA_SHORTCUT = os.environ.get("CUENTA") or None
FECHA_SHORTCUT = os.environ.get("FECHA") or None
LINEAS_RAW = os.environ.get("LINEAS") or None

MADRID = ZoneInfo("Europe/Madrid")


def normalizar_importe(importe):
    """El Shortcut puede mandar el decimal con coma o punto segun el formato
    regional del iPhone - se normaliza aqui."""
    return float(str(importe).replace(",", "."))


def resolver_fecha(fecha_shortcut):
    if fecha_shortcut:
        return fecha_shortcut
    return datetime.now(MADRID).date().isoformat()


def listar_etiquetas_para_matcher():
    """Vocabulario vigente en vivo desde Supabase: id, nombre, keyword[],
    posibles_categorias[]. Sin cache (opcion A: un dato, un lugar)."""
    url = f"{SUPABASE_URL}/rest/v1/etiquetas"
    params = {
        "estado": "eq.activa",
        "select": "id_etiqueta,nombre_etiqueta,keyword,posibles_categorias",
    }
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def resolver_etiqueta_por_keyword(texto, codigo_categoria, etiquetas):
    """Devuelve (nombre_etiqueta, id_etiqueta) de la etiqueta cuyas keywords
    tienen mas coincidencias dentro del texto, acotada a las etiquetas cuyo
    posibles_categorias incluye la categoria elegida. (None, None) si nada
    coincide. El score pondera por longitud de keyword para que un match
    especifico ("pechuga de pollo") gane a uno generico ("pollo")."""
    texto_norm = normalizar(texto)
    if not texto_norm:
        return None, None

    mejor = None
    mejor_score = 0
    for e in etiquetas:
        if codigo_categoria not in (e.get("posibles_categorias") or []):
            continue
        score = 0
        for kw in (e.get("keyword") or []):
            kw_norm = normalizar(kw)
            if kw_norm and kw_norm in texto_norm:
                score += len(kw_norm)
        if score > mejor_score:
            mejor_score = score
            mejor = e

    if mejor is None:
        return None, None
    return mejor["nombre_etiqueta"], mejor["id_etiqueta"]


def construir_fila(linea, id_cuenta, fecha, etiquetas):
    codigo_categoria = linea.get("categoria")
    importe = linea.get("importe")

    if not codigo_categoria or importe is None:
        raise ValueError(f"Linea incompleta, falta categoria/importe: {linea}")

    categoria = resolver_categoria(codigo_categoria, fecha)
    if categoria is None:
        raise ValueError(f"No hay version vigente de la categoria '{codigo_categoria}' para la fecha {fecha}")

    signo = resolver_signo(categoria["tipo"])
    if signo is None:
        raise ValueError(
            f"La categoria '{codigo_categoria}' es de tipo '{categoria['tipo']}', "
            "que no se puede registrar en una sola linea (ej. un traspaso necesita 2 filas ligadas)"
        )

    texto = linea.get("nombre_operacion") or ""
    nombre_etiqueta, id_etiqueta = resolver_etiqueta_por_keyword(texto, codigo_categoria, etiquetas)
    if id_etiqueta is None:
        raise RuntimeError(
            f"Sin match de keyword para '{texto}' en la categoria '{codigo_categoria}' "
            "- se deriva a pendientes para clasificacion nocturna"
        )

    return {
        "nombre_operacion": texto or nombre_etiqueta.capitalize(),
        "importe": abs(normalizar_importe(importe)) * signo,
        "fecha_operacion": fecha,
        "id_cuenta": id_cuenta,
        "id_categoria": categoria["id_categoria"],
        "id_etiqueta": id_etiqueta,
        "comentario": linea.get("comentario"),
        "tipo": categoria["tipo"],
    }, nombre_etiqueta


def procesar_gasto_dinamico(cuenta_nombre, lineas_raw, fecha_shortcut):
    """Hace todo el trabajo real a partir del payload crudo del Shortcut.
    Lanza excepciones en cualquier fallo (ValueError datos invalidos,
    RuntimeError sin-match/Supabase) - nunca Telegram ni sys.exit."""
    if not cuenta_nombre:
        raise ValueError("Falta 'cuenta' en el payload")
    if not lineas_raw:
        raise ValueError("Falta 'lineas' en el payload")

    try:
        lineas = json.loads(lineas_raw) if isinstance(lineas_raw, str) else lineas_raw
    except json.JSONDecodeError:
        raise ValueError(f"'lineas' no es JSON valido: {lineas_raw}")

    if not isinstance(lineas, list) or not (1 <= len(lineas) <= 3):
        cantidad = len(lineas) if isinstance(lineas, list) else "datos invalidos"
        raise ValueError(f"'lineas' debe ser una lista de 1 a 3 gastos, llego: {cantidad}")

    id_cuenta = resolver_id_cuenta_por_nombre(cuenta_nombre)
    if id_cuenta is None:
        raise ValueError(f"No existe ninguna cuenta llamada '{cuenta_nombre}'")

    fecha = resolver_fecha(fecha_shortcut)
    etiquetas = listar_etiquetas_para_matcher()

    filas = []
    etiquetas_asignadas = []
    for linea in lineas:
        fila, nombre_etiqueta = construir_fila(linea, id_cuenta, fecha, etiquetas)
        filas.append(fila)
        etiquetas_asignadas.append(nombre_etiqueta)

    resultado = insertar_filas(filas)
    if not resultado.ok:
        raise RuntimeError(f"Supabase rechazo el insert ({resultado.status_code}): {resultado.text}")

    return resultado.json(), etiquetas_asignadas, fecha


def notificar_exito(filas_insertadas, etiquetas_asignadas, fecha):
    resumen = "\n".join(
        "  - {tipo}: {nombre} — {importe}€ — etiqueta: {etiqueta}".format(
            tipo="Ingreso" if float(fila["importe"]) > 0 else "Gasto",
            nombre=fila["nombre_operacion"],
            importe=fila["importe"],
            etiqueta=etiqueta,
        )
        for fila, etiqueta in zip(filas_insertadas, etiquetas_asignadas)
    )
    ids = ", ".join(fila["id_operacion"] for fila in filas_insertadas)
    notificar_telegram(f"✅ Registrado ({fecha}):\n{resumen}\nIDs: {ids}")
    print(f"OK: {ids}")


def main():
    payload = {"cuenta": CUENTA_SHORTCUT, "fecha": FECHA_SHORTCUT, "lineas": LINEAS_RAW}

    try:
        filas_insertadas, etiquetas_asignadas, fecha = procesar_gasto_dinamico(
            CUENTA_SHORTCUT, LINEAS_RAW, FECHA_SHORTCUT
        )
    except Exception as e:
        id_pendiente = guardar_registro_pendiente("gasto_dinamico", payload, error_detalle=str(e))
        notificar_telegram(f"❌ Gasto dinamico fallido ({id_pendiente}): {e}\n{NOTA_PENDIENTE}")
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    notificar_exito(filas_insertadas, etiquetas_asignadas, fecha)


if __name__ == "__main__":
    main()
