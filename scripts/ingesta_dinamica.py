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

La logica real vive en procesar_gasto_dinamico() - no llama a Telegram ni
hace sys.exit, solo lanza excepciones. Esto permite que la reuse tanto
main() (disparo normal via Shortcut/GitHub) como
reintentar_registros_pendientes.py (cron nocturno) sin duplicar nada, mismo
patron que procesar_foto() en ingesta_foto.py.
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
    guardar_registro_pendiente,
    es_error_reintentable,
)

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
CUENTA_SHORTCUT = os.environ.get("CUENTA") or None
FECHA_SHORTCUT = os.environ.get("FECHA") or None
LINEAS_RAW = os.environ.get("LINEAS") or None

MADRID = ZoneInfo("Europe/Madrid")


def normalizar_importe(importe):
    """El Shortcut puede mandar el decimal con coma o con punto segun el
    formato regional del iPhone en ese momento - se normaliza aqui en vez de
    depender de que el Shortcut siempre mande el mismo caracter."""
    return float(str(importe).replace(",", "."))


def resolver_fecha(fecha_shortcut):
    if fecha_shortcut:
        return fecha_shortcut
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
    resultado = llamar_gemini_json([{"text": prompt}], GEMINI_API_KEY)  # deja propagar RuntimeError

    etiquetas = resultado.get("etiquetas")
    if not isinstance(etiquetas, list) or len(etiquetas) != len(lineas):
        raise RuntimeError(f"Gemini devolvio {etiquetas!r}, se esperaban {len(lineas)} etiqueta(s)")

    return etiquetas


def construir_fila(linea, id_cuenta, fecha, nombre_etiqueta):
    codigo_categoria = linea.get("categoria")
    importe = linea.get("importe")

    if not codigo_categoria or importe is None:
        raise ValueError(f"Linea incompleta, falta categoria/importe: {linea}")

    categoria = resolver_categoria(codigo_categoria, fecha)
    if categoria is None:
        raise ValueError(f"No hay version vigente de la categoria '{codigo_categoria}' para la fecha {fecha}")

    id_etiqueta = resolver_id_etiqueta_por_nombre(nombre_etiqueta)
    if id_etiqueta is None:
        raise RuntimeError(f"La IA devolvio etiqueta '{nombre_etiqueta}' que no existe")

    signo = resolver_signo(categoria["tipo"])
    if signo is None:
        raise ValueError(
            f"La categoria '{codigo_categoria}' es de tipo '{categoria['tipo']}', "
            "que no se puede registrar en una sola linea (ej. un traspaso necesita 2 filas ligadas)"
        )

    nombre_operacion = linea.get("nombre_operacion") or nombre_etiqueta.capitalize()

    return {
        "nombre_operacion": nombre_operacion,
        "importe": abs(normalizar_importe(importe)) * signo,
        "fecha_operacion": fecha,
        "id_cuenta": id_cuenta,
        "id_categoria": categoria["id_categoria"],
        "id_etiqueta": id_etiqueta,
        "comentario": linea.get("comentario"),
        "tipo": categoria["tipo"],
    }


def procesar_gasto_dinamico(cuenta_nombre, lineas_raw, fecha_shortcut):
    """Hace todo el trabajo real a partir del payload crudo del Shortcut.
    Lanza excepciones en cualquier fallo (ValueError para datos invalidos,
    RuntimeError para fallos de IA/Supabase) - nunca llama a Telegram ni
    hace sys.exit, eso lo decide el llamador."""
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

    etiquetas_activas = listar_etiquetas_activas()
    glosario_texto = leer_glosario()
    etiquetas_asignadas = resolver_etiquetas_ia(lineas, etiquetas_activas, glosario_texto)

    filas = [
        construir_fila(linea, id_cuenta, fecha, etiqueta)
        for linea, etiqueta in zip(lineas, etiquetas_asignadas)
    ]

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
        reintentable = es_error_reintentable(e)
        id_pendiente = guardar_registro_pendiente(
            "gasto_dinamico", payload, error_detalle=str(e), reintentable=reintentable
        )
        nota = (
            "Se guardo como pendiente, se reintenta solo cada noche."
            if reintentable
            else "NO se va a reintentar solo (parece un error de datos/configuracion, no transitorio) - hace falta arreglarlo a mano."
        )
        notificar_telegram(f"❌ Gasto dinamico fallido ({id_pendiente}): {e}\n{nota}")
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    notificar_exito(filas_insertadas, etiquetas_asignadas, fecha)


if __name__ == "__main__":
    main()
