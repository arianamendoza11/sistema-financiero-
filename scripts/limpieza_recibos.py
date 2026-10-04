# -*- coding: utf-8 -*-
"""
Limpieza del bucket 'recibos': corre cada 2 dias (cron) y borra solo los
objetos con mas de RETENCION_DIAS de antiguedad. 7 dias de ventana le dan
tiempo de sobra a la rutina nocturna de Claude (corre cada noche) para
procesar un pendiente de foto antes de perder la foto.

Doble condicional antes de borrar: ademas de la edad, se excluye cualquier
foto que todavia este referenciada por un pendiente sin procesar
(origen gasto_foto, estado='pendiente') - si sigue ahi es porque el sistema
todavia la necesita, sin importar cuantos dias hayan pasado. Las de pendientes
ya procesados si caducan con normalidad.
"""
from datetime import datetime, timedelta, timezone

from motor_supabase import (
    listar_objetos_storage,
    borrar_objetos_storage,
    listar_fotos_pendientes_de_borrado,
    notificar_telegram,
)

BUCKET = "recibos"
RETENCION_DIAS = 7


def main():
    objetos = listar_objetos_storage(BUCKET)
    limite = datetime.now(timezone.utc) - timedelta(days=RETENCION_DIAS)
    referenciadas = listar_fotos_pendientes_de_borrado()

    a_borrar = [
        obj["name"] for obj in objetos
        if datetime.fromisoformat(obj["created_at"].replace("Z", "+00:00")) < limite
        and obj["name"] not in referenciadas
    ]

    if not a_borrar:
        print("Nada que borrar.")
        return

    resultado = borrar_objetos_storage(BUCKET, a_borrar)
    if not resultado.ok:
        notificar_telegram(f"⚠️ Limpieza de recibos fallo ({resultado.status_code}): {resultado.text}")
        print(f"ERROR: {resultado.status_code} {resultado.text}")
        return

    print(f"OK: {len(a_borrar)} objeto(s) borrado(s): {', '.join(a_borrar)}")


if __name__ == "__main__":
    main()
