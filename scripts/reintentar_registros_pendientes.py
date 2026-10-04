# -*- coding: utf-8 -*-
"""
Reproceso de registros_pendientes (gasto_dinamico, gasto_foto,
ingesta_estandarizada). Reusa las funciones procesar_* de cada script - no
duplica logica de negocio.

NOTA (WS3): el cron automatico esta DESACTIVADO. El reproceso nocturno pasa a
la rutina de Claude (WS4). Este script se conserva solo para disparo manual
(workflow_dispatch) hasta validar la rutina; despues se depura o elimina.

Para no spamear Telegram cada reproceso que algo siga fallando por lo mismo, se
queda callado en reintentos fallidos y solo avisa en tres casos:
- exito (siempre)
- una foto que ya se perdio en Storage (FotoNoEncontrada) - fallo terminal
- un registro con 3 intentos fallidos sin resolverse - un aviso unico
  (columna 'alertado' evita repetirlo)

Los registros marcados reintentable=False (errores estructurales de datos o
config) se ignoran a proposito: quedan en la tabla para que Ariana los revise a
mano.

Entre un registro y el siguiente se espera PAUSA_ENTRE_PENDIENTES_SEG como
cortesia para no saturar la IA de vision en rafagas.
"""
import time

from ingesta_dinamica import procesar_gasto_dinamico, notificar_exito as notificar_exito_dinamico
from ingesta_estandarizada import procesar_ingesta_estandarizada, notificar_exito as notificar_exito_estandarizada
from ingesta_foto import procesar_foto, notificar_exito as notificar_exito_foto, FotoNoEncontrada
from motor_supabase import (
    listar_registros_pendientes,
    borrar_registro_pendiente,
    actualizar_intento_fallido,
    marcar_alertado,
    es_error_reintentable,
    notificar_telegram,
)

INTENTOS_ANTES_DE_ALERTAR = 3
PAUSA_ENTRE_PENDIENTES_SEG = 15  # cortesia para no saturar la IA de vision en rafagas


def reintentar_gasto_dinamico(payload):
    filas_insertadas, etiquetas_asignadas, fecha = procesar_gasto_dinamico(
        payload.get("cuenta"), payload.get("lineas"), payload.get("fecha")
    )
    notificar_exito_dinamico(filas_insertadas, etiquetas_asignadas, fecha)


def reintentar_ingesta_estandarizada(payload):
    filas_insertadas, id_etiqueta = procesar_ingesta_estandarizada(
        payload.get("tipo_ingesta"), payload.get("fecha"), payload.get("monto"),
        payload.get("cuenta"), payload.get("comentario"), payload.get("monto_eur"),
    )
    notificar_exito_estandarizada(payload.get("tipo_ingesta"), filas_insertadas, id_etiqueta)


def reintentar_gasto_foto(payload, foto_bucket_path):
    filas_insertadas, etiquetas, comercio, fecha = procesar_foto(
        payload.get("cuenta"), payload.get("categorias"), foto_bucket_path
    )
    notificar_exito_foto(filas_insertadas, etiquetas, comercio, fecha)


REINTENTOS_POR_ORIGEN = {
    "gasto_dinamico": lambda registro: reintentar_gasto_dinamico(registro["payload"]),
    "ingesta_estandarizada": lambda registro: reintentar_ingesta_estandarizada(registro["payload"]),
    "gasto_foto": lambda registro: reintentar_gasto_foto(registro["payload"], registro["foto_bucket_path"]),
}


def main():
    pendientes = listar_registros_pendientes(solo_reintentables=True)
    if not pendientes:
        print("Nada pendiente reintentable.")
        return

    for i, registro in enumerate(pendientes):
        # Espaciar las llamadas a la IA para no provocar 429 en rafagas.
        if i > 0:
            time.sleep(PAUSA_ENTRE_PENDIENTES_SEG)

        id_pendiente = registro["id_pendiente"]
        origen = registro["origen"]
        reintentar = REINTENTOS_POR_ORIGEN.get(origen)

        if reintentar is None:
            print(f"Origen desconocido, se ignora: {id_pendiente} ({origen})")
            continue

        try:
            reintentar(registro)
        except FotoNoEncontrada:
            borrar_registro_pendiente(id_pendiente)
            notificar_telegram(
                f"⚠️ Se perdio un recibo pendiente ({id_pendiente}): la foto "
                f"'{registro['foto_bucket_path']}' ya no existe en Storage - se cumplio "
                f"la retencion antes de poder procesarla. Nunca se registro, no hay forma de recuperarla."
            )
            print(f"PERDIDA: {id_pendiente}")
            continue
        except Exception as e:
            intentos = registro["intentos"] + 1
            reintentable = es_error_reintentable(e)
            actualizar_intento_fallido(id_pendiente, intentos, str(e), reintentable)

            if intentos >= INTENTOS_ANTES_DE_ALERTAR and not registro["alertado"]:
                notificar_telegram(
                    f"⚠️ {id_pendiente} ({origen}) lleva {intentos} intentos sin poder registrarse: {e}\n"
                    f"Payload: {registro['payload']}"
                )
                marcar_alertado(id_pendiente)

            print(f"Sigue fallando (intento {intentos}): {id_pendiente} - {e}")
            continue

        borrar_registro_pendiente(id_pendiente)
        print(f"OK: {id_pendiente}")


if __name__ == "__main__":
    main()
