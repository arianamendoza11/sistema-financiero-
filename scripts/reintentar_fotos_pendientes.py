# -*- coding: utf-8 -*-
"""
Cron nocturno: reintenta las fotos que quedaron en fotos_pendientes (fallos
de Gemini u otro error transitorio en gasto_foto). Reusa procesar_foto() de
ingesta_foto.py - no duplica logica de negocio.

Para no spamear Telegram cada noche que algo siga fallando por lo mismo, se
queda callado en reintentos fallidos y solo avisa en dos casos: exito, o
cuando la foto ya se perdio (se cumplio la retencion de Storage antes de
poder procesarla) - ahi se da por vencido y se lo dice a Diego.
"""
from ingesta_foto import procesar_foto, notificar_exito, FotoNoEncontrada
from motor_supabase import (
    listar_fotos_pendientes,
    guardar_foto_pendiente,
    borrar_foto_pendiente,
    notificar_telegram,
)


def main():
    pendientes = listar_fotos_pendientes()
    if not pendientes:
        print("Nada pendiente.")
        return

    for foto in pendientes:
        foto_path = foto["foto_bucket_path"]

        try:
            filas_insertadas, comercio, fecha = procesar_foto(foto["cuenta"], foto["categorias"], foto_path)
        except FotoNoEncontrada:
            borrar_foto_pendiente(foto_path)
            notificar_telegram(
                f"⚠️ Se perdio un recibo pendiente: la foto '{foto_path}' (cuenta {foto['cuenta']}) "
                f"ya no existe en Storage - se cumplio la retencion antes de poder procesarla. "
                f"Nunca se registro en seguimiento_efectivo, no hay forma de recuperarla."
            )
            print(f"PERDIDA: {foto_path}")
            continue
        except Exception as e:
            guardar_foto_pendiente(foto_path, foto["cuenta"], foto["categorias"])
            print(f"Sigue fallando (sin avisar de nuevo): {foto_path} - {e}")
            continue

        borrar_foto_pendiente(foto_path)
        notificar_exito(filas_insertadas, comercio, fecha)


if __name__ == "__main__":
    main()
