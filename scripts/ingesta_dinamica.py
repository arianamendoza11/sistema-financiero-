# -*- coding: utf-8 -*-
"""
Gasto dinamico: NFC o Hub Manual mandan una cuenta compartida, opcionalmente
una fecha, y 1-3 lineas de gasto (importe + categoria + un texto corto que
describe la compra). La categoria la elige Diego en el Shortcut (afecta
presupuesto). La etiqueta NO se pide en el Shortcut - una lista de 41
etiquetas activas es inmostrable en pantalla de celular - la resuelve Gemini
(solo texto, sin imagen) a partir de la categoria + el texto, igual que en
la rama "con foto" pero sin OCR de por medio. Es barato de corregir si se
equivoca, por eso se delega.

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
    listar_etiquetas_activas,
    leer_glosario,
    llamar_gemini_json,
)

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
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


def resolver_etiquetas_ia(lineas, etiquetas_activas, glosario_texto):
    """Una sola llamada a Gemini (solo texto) que devuelve una etiqueta por
    cada linea, en el mismo orden. La categoria ya la eligio Diego - esto
    solo cubre lo barato de corregir (la etiqueta)."""
    items_texto = "\n".join(
        f'{i + 1}. categoria="{l.get("categoria")}" texto="{l.get("nombre_operacion")}"'
        for i, l in enumerate(lineas)
    )
    prompt = f"""Eres el motor de clasificacion de etiquetas de un sistema financiero personal.

Para cada linea de gasto (categoria ya elegida por el usuario + un texto corto que describe la compra), elige la etiqueta que mejor encaje.

Devuelve EXCLUSIVAMENTE un JSON (sin markdown, sin texto fuera del JSON) con esta forma:
{{"etiquetas": ["etiqueta linea 1", "etiqueta linea 2"]}}

El array debe tener EXACTAMENTE {len(lineas)} elemento(s), en el mismo orden que las lineas de entrada.

Reglas:
- Cada etiqueta debe ser EXACTAMENTE uno de los valores permitidos abajo. Nunca inventes una que no este en la lista.
- Usa la categoria como contexto, pero clasifica por lo que ES la compra descrita en el texto.
- Si el texto no aparece en el glosario de referencia, usa tu propio criterio semantico para elegir la que mejor encaje - nunca dejes una linea sin clasificar.

Etiquetas permitidas: {', '.join(etiquetas_activas)}

Glosario de referencia (sugestivo, no exhaustivo):
{glosario_texto}

Lineas a clasificar:
{items_texto}
"""
    resultado = llamar_gemini_json([{"text": prompt}], GEMINI_API_KEY)  # deja propagar RuntimeError - main() decide el fallback

    etiquetas = resultado.get("etiquetas")
    if not isinstance(etiquetas, list) or len(etiquetas) != len(lineas):
        error_salir(f"Gemini devolvio {etiquetas!r}, se esperaban {len(lineas)} etiqueta(s)")

    return etiquetas


def construir_sql_fallback(lineas, id_cuenta, fecha, etiquetas_activas):
    """Cuando Gemini esta caido, arma un INSERT manual con todo ya resuelto
    (cuenta, categoria, signo) excepto la etiqueta - eso queda como
    instruccion + la lista real de etiquetas activas, para completarlo a
    mano (Supabase MCP, o pegado en cualquier chat con esa capacidad)."""
    filas_sql = []
    for linea in lineas:
        codigo_categoria = linea.get("categoria")
        importe = linea.get("importe")
        if not codigo_categoria or importe is None:
            continue

        categoria = resolver_categoria(codigo_categoria, fecha)
        if categoria is None:
            continue

        signo = resolver_signo(categoria["tipo"])
        if signo is None:
            continue

        nombre_operacion = (linea.get("nombre_operacion") or codigo_categoria).replace("'", "''")
        comentario = linea.get("comentario")
        comentario_sql = "'{}'".format(comentario.replace("'", "''")) if comentario else "NULL"
        importe_final = abs(float(importe)) * signo

        filas_sql.append(
            "  ('{nombre}', {importe}, '{fecha}', '{cuenta}', '{categoria}', "
            "(SELECT id_etiqueta FROM etiquetas WHERE nombre_etiqueta = '<<ELEGIR>>'), "
            "{comentario}, '{tipo}')".format(
                nombre=nombre_operacion,
                importe=importe_final,
                fecha=fecha,
                cuenta=id_cuenta,
                categoria=categoria["id_categoria"],
                comentario=comentario_sql,
                tipo=categoria["tipo"],
            )
        )

    if not filas_sql:
        return None

    return (
        "-- Reemplaza cada <<ELEGIR>> por el nombre EXACTO de la etiqueta que mejor\n"
        "-- encaje, elegida de esta lista de etiquetas activas:\n"
        "-- " + ", ".join(etiquetas_activas) + "\n"
        "INSERT INTO seguimiento_efectivo\n"
        "  (nombre_operacion, importe, fecha_operacion, id_cuenta, id_categoria, id_etiqueta, comentario, tipo)\n"
        "VALUES\n" + ",\n".join(filas_sql) + ";"
    )


def construir_fila(linea, id_cuenta, fecha, nombre_etiqueta):
    codigo_categoria = linea.get("categoria")
    importe = linea.get("importe")

    if not codigo_categoria or importe is None:
        error_salir(f"Linea incompleta, falta categoria/importe: {linea}")

    categoria = resolver_categoria(codigo_categoria, fecha)
    if categoria is None:
        error_salir(f"No hay version vigente de la categoria '{codigo_categoria}' para la fecha {fecha}")

    id_etiqueta = resolver_id_etiqueta_por_nombre(nombre_etiqueta)
    if id_etiqueta is None:
        error_salir(f"La IA devolvio etiqueta '{nombre_etiqueta}' que no existe")

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

    etiquetas_activas = listar_etiquetas_activas()
    glosario_texto = leer_glosario()

    try:
        etiquetas_asignadas = resolver_etiquetas_ia(lineas, etiquetas_activas, glosario_texto)
    except RuntimeError as e:
        sql_fallback = construir_sql_fallback(lineas, id_cuenta, fecha, etiquetas_activas)
        mensaje = f"❌ Gasto dinamico fallido: {e}"
        if sql_fallback:
            mensaje += "\n------------------------------------------\n" + sql_fallback
        notificar_telegram(mensaje)
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    filas = [
        construir_fila(linea, id_cuenta, fecha, etiqueta)
        for linea, etiqueta in zip(lineas, etiquetas_asignadas)
    ]

    resultado = insertar_filas(filas)
    if not resultado.ok:
        error_salir(f"Supabase rechazo el insert ({resultado.status_code}): {resultado.text}")

    filas_insertadas = resultado.json()
    ids = ", ".join(fila["id_operacion"] for fila in filas_insertadas)
    notificar_telegram(f"✅ Gasto registrado ({fecha}): {ids}")
    print(f"OK: {ids}")


if __name__ == "__main__":
    main()
