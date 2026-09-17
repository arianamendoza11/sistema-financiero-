# -*- coding: utf-8 -*-
"""
Cron nocturno unico: reintenta todo lo que quedo en registros_pendientes,
sin importar el origen (gasto_dinamico, gasto_foto, ingesta_estandarizada).
Reusa las funciones procesar_* de cada script - no duplica logica de
negocio, mismo patron que ya usaba reintentar_fotos_pendientes.py.

Para no spamear Telegram cada noche que algo siga fallando por lo mismo, se
queda callado en reintentos fallidos y solo avisa en tres casos:
- exito (siempre)
- una foto que ya se perdio en Storage (FotoNoEncontrada) - fallo terminal,
  no tiene sentido seguir reintentando
- un registro que lleva 3 intentos fallidos sin resolverse - un aviso unico
  (columna 'alertado' evita repetirlo cada noche)

Los registros marcados reintentable=False (errores estructurales de datos o
configuracion, no transitorios) se ignoran aqui a proposito: reintentarlos
no cambia nada y solo gasta minutos de Actions. Quedan en la tabla para que
Diego los revise a mano.
"""
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


def reintentar_gasto_dinamico(payload):
    filas_insertadas, etiquetas_asignadas, fecha = procesar_gasto_dinamico(
        payload.get("cuenta"), payload.get("lineas"), payload.get("fecha")
    )
    notificar_exito_dinamico(filas_insertadas, etiquetas_asignadas, fecha)


def reintentar_ingesta_estandarizada(payload):
    filas_insertadas, id_etiqueta = procesar_ingesta_estandarizada(
        payload.get("tipo_ingesta"), payload.get("fecha"), payload.get("monto"),
        payload.get("cuenta"), payload.get("comentario"),
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

    for registro in pendientes:
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
