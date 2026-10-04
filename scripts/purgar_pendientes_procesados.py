# -*- coding: utf-8 -*-
"""
Purga diaria de registros_pendientes ya procesados.

La rutina nocturna de Claude (WS4) no puede borrar filas: el conector MCP de
Supabase pide aprobacion humana para cualquier SQL destructivo y la rutina es
desatendida. Por eso la rutina solo MARCA cada pendiente que resuelve
(estado='procesado') y este job, que usa la service key por REST y no pasa por
el MCP, los borra fisicamente una vez al dia.

Corre 1 h 30 min despues de la rutina (ambos crons en UTC, sin horario de
verano). Los pendientes en estado 'pendiente' nunca se tocan.
"""
import sys

import requests

from motor_supabase import SUPABASE_URL, REST_HEADERS, notificar_telegram


def purgar_procesados():
    """Borra los pendientes con estado='procesado'. Devuelve las filas borradas."""
    url = f"{SUPABASE_URL}/rest/v1/registros_pendientes"
    r = requests.delete(url, headers=REST_HEADERS, params={"estado": "eq.procesado"}, timeout=15)
    r.raise_for_status()
    return r.json()


def main():
    try:
        borrados = purgar_procesados()
    except requests.RequestException as e:
        notificar_telegram(f"❌ Purga diaria de pendientes procesados fallida: {e}")
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if not borrados:
        print("Nada que purgar.")
        return

    ids = ", ".join(fila["id_pendiente"] for fila in borrados)
    notificar_telegram(f"🧹 Purga diaria: {len(borrados)} pendiente(s) procesado(s) eliminado(s): {ids}")
    print(f"Purgados: {ids}")


if __name__ == "__main__":
    main()
