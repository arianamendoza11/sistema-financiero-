# -*- coding: utf-8 -*-
"""
Gasto por foto (NFC/Manual, rama "con foto"): el Shortcut sube la imagen del
recibo a Supabase Storage (bucket 'recibos') y manda cuenta + la ruta del objeto
que el propio Shortcut genero.

Diseno de responsabilidades (para que la IA nunca pueda romper presupuesto):
- CATEGORIAS las preselecciona Ariana en el Shortcut. El prompt se construye EN
  VIVO solo con esas categorias: un MENU numerado de pares categoria/etiqueta
  (con ejemplos sacados de etiquetas.keyword). La IA solo elige un numero del
  menu por producto - nunca escribe nombres de categoria/etiqueta a mano.
- El GLOSARIO manda sobre la IA: si las keywords de una etiqueta reconocen el
  producto, esa opcion se impone a la que eligio el modelo (determinista).
- IMPORTES se verifican contra el TOTAL impreso en el ticket. Si no cuadran se
  pide una relectura; si siguen sin cuadrar, se deriva a pendientes.
- FECHA siempre es la fecha de ejecucion del workflow (Europe/Madrid).

La IA de vision es Qwen via DashScope (endpoint OpenAI-compatible).
La logica real vive en procesar_foto() - no llama a error_salir ni sys.exit, solo
lanza excepciones; main() decide Telegram y pendiente.

Sin RUN2, sin botones, sin webhook: se inserta directo y Telegram notifica. Si
algo sale mal se corrige a mano despues (via Supabase MCP).
"""
import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from motor_supabase import (
    SUPABASE_URL,
    REST_HEADERS,
    notificar_telegram,
    resolver_categoria,
    resolver_id_cuenta_por_nombre,
    resolver_signo,
    insertar_filas,
    llamar_qwen_json,
    normalizar,
    guardar_registro_pendiente,
    borrar_registros_pendientes_de_foto,
    NOTA_PENDIENTE,
)

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY")
CUENTA_SHORTCUT = os.environ.get("CUENTA") or None
FOTO_PATH = os.environ.get("FOTO_PATH") or None
CATEGORIAS_RAW = os.environ.get("CATEGORIAS_PERMITIDAS") or None

MADRID = ZoneInfo("Europe/Madrid")
MAX_LINEAS = 60  # un ticket real no supera esto, es solo un limite de cordura
MAX_KW_POR_OPCION = 15  # ejemplos por opcion del menu: orientan sin inflar el prompt
TOLERANCIA_TOTAL = 0.05  # EUR de margen entre la suma de lineas y el TOTAL del ticket
IMPORTES_EJEMPLO = (1.25, 2.40, 3.10, 0.95, 4.60)

INSTRUCCION_USUARIO = "Lee este ticket y devuelve el JSON."


class FotoNoEncontrada(RuntimeError):
    """La foto ya no existe en Storage (se cumplio la retencion antes de poder
    procesarla) - no tiene sentido seguir reintentando esta."""


def parsear_categorias(raw):
    """Normaliza 'categorias' venga como venga del Shortcut a una lista de
    codigos limpia, sin tener que tocar el Shortcut (son fragiles de mantener).

    Casos reales observados:
    - array JSON correcto: ["alimentacion", "hogar"]
    - array de UN elemento con los codigos pegados por comillas-coma:
      ["alimentacion','salud_belleza"]  (lo que manda hoy el Shortcut)
    - string plano con separadores (coma, salto de linea, ; | etc.)

    Estrategia: parsear JSON si se puede, aplanar a piezas, y separar cada
    pieza por comillas / comas / corchetes / ; / | / espacios.
    """
    if raw is None:
        return []

    valor = raw
    try:
        valor = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        pass

    piezas = [str(x) for x in valor] if isinstance(valor, list) else [str(valor)]

    codigos = []
    for pieza in piezas:
        for parte in re.split(r"[\s,;|'\"\[\]]+", pieza):
            limpio = parte.strip()
            if limpio:
                codigos.append(limpio)
    return codigos


def listar_etiquetas_completo():
    """Etiquetas activas con id, nombre, posibles_categorias, criterio y keywords.
    Con esto se construye el menu del prompt y se resuelve el id sin mas
    consultas. En vivo, sin cache."""
    url = f"{SUPABASE_URL}/rest/v1/etiquetas"
    params = {"estado": "eq.activa", "select": "id_etiqueta,nombre_etiqueta,posibles_categorias,criterio,keyword"}
    r = requests.get(url, headers=REST_HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def descargar_foto(path):
    url = f"{SUPABASE_URL}/storage/v1/object/{path}"
    r = requests.get(url, headers=REST_HEADERS, timeout=30)
    if r.status_code == 404:
        raise FotoNoEncontrada(f"La foto '{path}' ya no existe en Supabase Storage")
    if not r.ok:
        raise RuntimeError(f"No se pudo descargar la foto '{path}' de Supabase Storage ({r.status_code}): {r.text}")
    return r.content


def resolver_categorias_permitidas(codigos, fecha):
    """Valida en vivo contra Supabase cada codigo_categoria que Ariana
    preselecciono en el Shortcut. Devuelve {codigo: categoria_info}."""
    resueltas = {}
    for codigo in codigos:
        categoria = resolver_categoria(codigo, fecha)
        if categoria is None:
            raise RuntimeError(f"La categoria preseleccionada '{codigo}' no tiene version vigente para la fecha {fecha}")
        resueltas[codigo] = categoria
    return resueltas


# --- Menu dinamico: SOLO las categorias que llegaron del Shortcut ---

def construir_menu(categorias_permitidas, etiquetas_full):
    """Opciones numeradas (categoria, etiqueta) de las categorias preseleccionadas,
    segun etiquetas.posibles_categorias. Una etiqueta que vive en dos categorias
    permitidas aparece como dos opciones (una por categoria)."""
    menu = []
    for codigo in categorias_permitidas:
        propias = sorted(
            (e for e in etiquetas_full if codigo in (e.get("posibles_categorias") or [])),
            key=lambda e: e["nombre_etiqueta"],
        )
        if not propias:
            raise RuntimeError(f"La categoria '{codigo}' no tiene etiquetas asociadas (posibles_categorias)")
        for e in propias:
            menu.append({
                "opcion": len(menu) + 1,
                "categoria": codigo,
                "etiqueta": e["nombre_etiqueta"],
                "id_etiqueta": e["id_etiqueta"],
                "criterio": e.get("criterio") or "",
                "keywords": e.get("keyword") or [],
            })
    return menu


def texto_menu(menu):
    """Cada opcion: categoria / etiqueta, su criterio (lo que la distingue de sus
    hermanas) y ejemplos de productos."""
    return "\n".join(
        f"{o['opcion']} = {o['categoria']} / {o['etiqueta']}"
        + (f" | {o['criterio']}" if o["criterio"] else "")
        + f" | ej: {', '.join(o['keywords'][:MAX_KW_POR_OPCION]) or '-'}"
        for o in menu
    )


def construir_ejemplo(menu, categorias_permitidas):
    """Ejemplo de salida generado con el propio menu: un producto por cada
    categoria recibida, tomado de los ejemplos de su primera etiqueta. Asi el
    ejemplo siempre muestra el reparto con LAS categorias de este ticket."""
    elegidas = []
    for codigo in categorias_permitidas:
        opciones = [o for o in menu if o["categoria"] == codigo and o["keywords"]]
        if opciones:
            elegidas.append(opciones[0])
    if len(elegidas) == 1:  # una sola categoria: dos productos de etiquetas distintas
        elegidas += [o for o in menu if o["keywords"] and o["etiqueta"] != elegidas[0]["etiqueta"]][:1]

    lineas = []
    for i, o in enumerate(elegidas):
        kw = o["keywords"][0]
        lineas.append({
            "texto_ticket": kw.upper(),
            "importe": IMPORTES_EJEMPLO[i % len(IMPORTES_EJEMPLO)],
            "producto": kw.capitalize(),
            "opcion": o["opcion"],
        })
    total = round(sum(linea["importe"] for linea in lineas), 2)
    return json.dumps({"comercio": "Nombre del comercio", "total": total, "lineas": lineas}, ensure_ascii=False)


def construir_prompt_sistema(menu, categorias_permitidas):
    return f"""Eres el lector de tickets de compra de un sistema financiero personal. Tu trabajo tiene dos pasos: transcribir cada producto del ticket con su importe, y asignar a cada producto la opcion del menu que describe lo que ES ese producto.

PASO 1 - TRANSCRIBIR
- Recorre el ticket de arriba abajo. Cada producto del ticket es un objeto propio en "lineas", en el mismo orden del ticket.
- "texto_ticket": el texto del producto copiado tal cual aparece.
- "importe": el precio impreso en ESE MISMO renglon, a la derecha del producto. Numero positivo con punto decimal.
- Productos a peso o con varias unidades: el renglon de detalle ("1,268 kg x 1,49 EUR/kg", "2 x 0,85") forma parte del producto de arriba; su importe es el total de ese producto.
- Descuentos, promociones y cupones: restalos del importe del producto al que se aplican.
- "total": el TOTAL a pagar impreso en el ticket.
- Comprobacion final: la suma de los importes de "lineas" es igual a "total".

PASO 2 - CLASIFICAR
- Cada producto recibe el numero de la opcion del menu cuya definicion describe lo que es el producto. Se juzga el producto, no la tienda.
- Cada producto se clasifica por si solo: un mismo ticket reparte sus productos entre las categorias del menu que les correspondan.
- Entre opciones parecidas decide la definicion; los ejemplos orientan. Un producto que no aparezca en los ejemplos va a la opcion cuya definicion le encaje mejor.
- "producto": nombre corto y legible del producto en espanol.

MENU (numero = categoria / etiqueta | definicion | ejemplos de productos)
{texto_menu(menu)}

FORMATO DE SALIDA: unicamente este JSON.
Ejemplo de forma (productos ilustrativos, cada uno en la opcion de su naturaleza):
{construir_ejemplo(menu, categorias_permitidas)}
"""


# --- Lectura con verificacion contra el TOTAL del ticket ---

def _numero(valor):
    if valor is None or isinstance(valor, bool):
        return None
    try:
        return float(str(valor).replace(",", "."))
    except ValueError:
        return None


def verificar_total(resultado_ia):
    """(cuadra, suma, total). Si el ticket no trae total legible, no se bloquea."""
    lineas = resultado_ia.get("lineas") or []
    suma = round(sum(_numero(linea.get("importe")) or 0 for linea in lineas), 2)
    total = _numero(resultado_ia.get("total"))
    if total is None:
        return True, suma, None
    return abs(suma - total) <= TOLERANCIA_TOTAL, suma, total


def llamar_qwen(imagen_bytes, sistema, instruccion):
    return llamar_qwen_json(instruccion, imagen_bytes, DASHSCOPE_API_KEY, sistema=sistema)  # deja propagar RuntimeError


def leer_ticket(imagen_bytes, sistema):
    """Primera lectura; si la suma no cuadra con el TOTAL, UNA relectura con el
    descuadre como pista. Si sigue sin cuadrar, error -> pendiente (mejor revisar
    a mano que meter importes mal en el presupuesto)."""
    resultado = llamar_qwen(imagen_bytes, sistema, INSTRUCCION_USUARIO)
    cuadra, suma, total = verificar_total(resultado)
    if cuadra:
        return resultado

    relectura = (
        f"{INSTRUCCION_USUARIO}\n\nREVISION: en una lectura anterior la suma de importes dio {suma:.2f} "
        f"y el TOTAL impreso es {total:.2f}. Lee el ticket renglon por renglon y toma para cada "
        "producto el importe de su mismo renglon."
    )
    resultado = llamar_qwen(imagen_bytes, sistema, relectura)
    cuadra, suma, total = verificar_total(resultado)
    if not cuadra:
        raise RuntimeError(
            f"Los importes leidos no cuadran con el total del ticket (suma {suma:.2f} vs total {total:.2f}) "
            "- revisar a mano"
        )
    return resultado


# --- Clasificacion: el glosario manda, la IA cubre lo que el glosario no conoce ---

def opciones_por_keyword(texto, menu):
    """Opciones del menu con mejor puntaje de keywords sobre el texto (palabra
    completa, sin acentos). Puntaje = longitud de las keywords que coinciden, para
    que lo especifico gane a lo generico. [] si nada coincide."""
    texto_norm = normalizar(texto)
    if not texto_norm:
        return []
    puntajes = []
    for o in menu:
        score = 0
        for kw in o["keywords"]:
            kw_norm = normalizar(kw)
            if kw_norm and re.search(rf"\b{re.escape(kw_norm)}\b", texto_norm):
                score += len(kw_norm)
        if score:
            puntajes.append((score, o))
    if not puntajes:
        return []
    mejor = max(score for score, _ in puntajes)
    return [o for score, o in puntajes if score == mejor]


def resolver_opcion(linea, menu, menu_por_id):
    """Devuelve (opcion, por_glosario). El glosario se impone a la IA cuando
    reconoce el producto sin ambiguedad; si no, vale la opcion de la IA."""
    numero = _numero(linea.get("opcion"))
    opcion_ia = menu_por_id.get(int(numero)) if numero is not None else None

    # Manda el texto literal del ticket; el nombre que pone la IA solo cuenta si
    # el ticket no coincide con nada (ej. abreviaturas: "YOG GRIEGO" -> "Yogur").
    candidatas = (opciones_por_keyword(linea.get("texto_ticket"), menu)
                  or opciones_por_keyword(linea.get("producto"), menu))
    if candidatas and opcion_ia not in candidatas:
        if len(candidatas) == 1:
            return candidatas[0], True
        misma_categoria = [o for o in candidatas if opcion_ia and o["categoria"] == opcion_ia["categoria"]]
        if len(misma_categoria) == 1:
            return misma_categoria[0], True

    if opcion_ia is None:
        if candidatas:
            return candidatas[0], True
        raise RuntimeError(f"Linea incompleta devuelta por la IA (opcion fuera del menu): {linea}")
    return opcion_ia, False


def construir_fila(linea, opcion, id_cuenta, fecha, categorias_dict):
    importe = _numero(linea.get("importe"))
    if importe is None:
        raise RuntimeError(f"Linea incompleta devuelta por la IA (sin importe): {linea}")

    categoria = categorias_dict[opcion["categoria"]]
    signo = resolver_signo(categoria["tipo"])
    if signo is None:
        raise RuntimeError(
            f"La categoria '{opcion['categoria']}' es de tipo '{categoria['tipo']}', "
            "que no se puede registrar en una sola linea (ej. un traspaso necesita 2 filas ligadas)"
        )

    return {
        "nombre_operacion": linea.get("producto") or opcion["etiqueta"].capitalize(),
        "importe": abs(importe) * signo,
        "fecha_operacion": fecha,
        "id_cuenta": id_cuenta,
        "id_categoria": categoria["id_categoria"],
        "id_etiqueta": opcion["id_etiqueta"],
        "comentario": linea.get("texto_ticket"),
        "tipo": categoria["tipo"],
    }


def procesar_foto(cuenta_nombre, codigos_categoria, foto_path):
    """Hace todo el trabajo real. Devuelve (filas_insertadas, etiquetas, comercio,
    fecha) en exito. Lanza FotoNoEncontrada o RuntimeError en fallo - nunca
    Telegram ni sys.exit, eso lo decide el llamador."""
    id_cuenta = resolver_id_cuenta_por_nombre(cuenta_nombre)
    if id_cuenta is None:
        raise RuntimeError(f"No existe ninguna cuenta llamada '{cuenta_nombre}'")

    hoy = datetime.now(MADRID).date().isoformat()

    categorias_dict = resolver_categorias_permitidas(codigos_categoria, hoy)
    menu = construir_menu(list(categorias_dict), listar_etiquetas_completo())
    menu_por_id = {o["opcion"]: o for o in menu}
    sistema = construir_prompt_sistema(menu, list(categorias_dict))

    imagen_bytes = descargar_foto(foto_path)
    resultado_ia = leer_ticket(imagen_bytes, sistema)

    lineas_ia = resultado_ia.get("lineas") or []
    if not (1 <= len(lineas_ia) <= MAX_LINEAS):
        raise RuntimeError(f"La IA devolvio {len(lineas_ia)} lineas, fuera del rango valido 1-{MAX_LINEAS}")

    filas, etiquetas = [], []
    for linea in lineas_ia:
        opcion, por_glosario = resolver_opcion(linea, menu, menu_por_id)
        filas.append(construir_fila(linea, opcion, id_cuenta, hoy, categorias_dict))
        etiquetas.append(f"{opcion['categoria']} / {opcion['etiqueta']}" + (" (glosario)" if por_glosario else ""))

    resultado = insertar_filas(filas)
    if not resultado.ok:
        raise RuntimeError(f"Supabase rechazo el insert ({resultado.status_code}): {resultado.text}")

    comercio = resultado_ia.get("comercio") or "comercio no identificado"
    return resultado.json(), etiquetas, comercio, hoy


def notificar_exito(filas_insertadas, etiquetas, comercio, fecha):
    ids = ", ".join(fila["id_operacion"] for fila in filas_insertadas)
    total = sum(abs(float(fila["importe"])) for fila in filas_insertadas)
    resumen = "\n".join(
        "  - {tipo}: {nombre} — {importe}€ — {etiqueta}".format(
            tipo="Ingreso" if float(fila["importe"]) > 0 else "Gasto",
            nombre=fila["nombre_operacion"],
            importe=fila["importe"],
            etiqueta=etiqueta,
        )
        for fila, etiqueta in zip(filas_insertadas, etiquetas)
    )
    notificar_telegram(
        f"✅ Recibo de {comercio} ({fecha}) — {len(filas_insertadas)} línea(s), {total:.2f}€ total:\n"
        f"{resumen}\nIDs: {ids}"
    )
    print(f"OK: {ids}")


def main():
    if not CUENTA_SHORTCUT:
        notificar_telegram("❌ Ingesta de foto fallida: Falta 'cuenta' en el payload")
        sys.exit(1)
    if not FOTO_PATH:
        notificar_telegram("❌ Ingesta de foto fallida: Falta 'foto_bucket_path' en el payload")
        sys.exit(1)
    if not CATEGORIAS_RAW:
        notificar_telegram("❌ Ingesta de foto fallida: Falta 'categorias' (preseleccionadas en el Shortcut) en el payload")
        sys.exit(1)

    codigos_categoria = parsear_categorias(CATEGORIAS_RAW)
    if not codigos_categoria:
        notificar_telegram(f"❌ Ingesta de foto fallida: no pude extraer categorias de: {CATEGORIAS_RAW!r}")
        sys.exit(1)

    payload = {"cuenta": CUENTA_SHORTCUT, "categorias": codigos_categoria, "foto_bucket_path": FOTO_PATH}

    try:
        filas_insertadas, etiquetas, comercio, fecha = procesar_foto(CUENTA_SHORTCUT, codigos_categoria, FOTO_PATH)
    except Exception as e:
        id_pendiente = guardar_registro_pendiente(
            "gasto_foto", payload, foto_bucket_path=FOTO_PATH, error_detalle=str(e)
        )
        notificar_telegram(f"❌ Ingesta de foto fallida ({id_pendiente}): {e}\n{NOTA_PENDIENTE}")
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    borrar_registros_pendientes_de_foto(FOTO_PATH)
    notificar_exito(filas_insertadas, etiquetas, comercio, fecha)


if __name__ == "__main__":
    main()
