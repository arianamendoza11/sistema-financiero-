# -*- coding: utf-8 -*-
"""
Limpieza del bucket 'recibos': corre una vez al dia (cron) y borra solo los
objetos con mas de RETENCION_DIAS de antiguedad. Existe una ventana minima de
retencion (no se borra nada el mismo dia que se sube) para poder corregir un
insert erroneo antes de perder la foto de referencia.
"""
from datetime import datetime, timedelta, timezone

from motor_supabase import (
    listar_objetos_storage,
    borrar_objetos_storage,
    notificar_telegram,
)

BUCKET = "recibos"
RETENCION_DIAS = 0  # TEMPORAL: limpieza manual de fotos de prueba, revertir a 1


def main():
    objetos = listar_objetos_storage(BUCKET)
    limite = datetime.now(timezone.utc) - timedelta(days=RETENCION_DIAS)

    a_borrar = [
        obj["name"] for obj in objetos
        if datetime.fromisoformat(obj["created_at"].replace("Z", "+00:00")) < limite
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
