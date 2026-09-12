# Glosario de tageo de etiquetas (dinámico desde Supabase)

**IMPORTANTE:** A partir de la refactorización del 12 de septiembre de 2026, el glosario de etiquetas se carga **dinámicamente en runtime** desde la tabla `etiquetas` en Supabase (columna `keyword`), en lugar de estar hardcodeado en este markdown.

---

## Flujo actual (dinámico)

1. **Cada 3 días** (lunes y jueves a las 02:00 UTC): Workflow `sync_etiquetas.yml` ejecuta
2. `sync_etiquetas.py` consulta tabla `etiquetas` (solo activas, columna `keyword`) desde Supabase
3. Guarda caché local en `config/etiquetas.json`
4. **En cada ingesta** (gasto_dinamico.yml, gasto_foto.yml):
   - `motor_supabase.leer_glosario()` carga el glosario recombinado:
     - Secciones conceptuales (este markdown, TODO EXCEPTO la tabla)
     - Tabla de etiquetas generada dinámicamente desde `config/etiquetas.json`
   - Inyecta el glosario completo en el prompt de Gemini

### ¿Cómo agregar una etiqueta nueva?

1. Accede a Supabase → tabla `etiquetas`
2. Inserta una nueva fila con:
   - `nombre_etiqueta`: nombre exacto de la etiqueta (ej: "nueva_etiqueta")
   - `keyword`: array JSON con keywords (ej: `["palabra1", "palabra2", "palabra3"]`)
   - `estado`: "activa"
3. **No necesitas editar este markdown**
4. El workflow de sync lo tomará en la próxima ejecución (máximo 3 días)
5. La próxima ejecución de un workflow de ingesta usará automáticamente la nueva etiqueta

### Estructura de `keyword` en Supabase

Columna tipo JSONB. Formato:
```json
["keyword1", "keyword2", "keyword3", "...]
```

Ejemplo real:
```json
["pan", "carne", "pescado", "filete", "pollo", "fruta", "verdura", "huevos", "lácteos"]
```

---

## Principio no negociable: se tagea por elemento de línea, nunca por origen de compra

Cada línea del ticket se clasifica por **lo que ES el producto**, sin importar en qué comercio se compró. Un supermercado puede vender comida (`despensa`), alcohol (`cerveza o copa`), limpieza (`limpieza del hogar`) o tecnología (`tecnologia`) en el mismo ticket — cada línea se juzga independientemente.

---

## Cómo se construyó el glosario original

Tres fuentes, en este orden de confianza:

1. **Antecedentes reales del sistema** (`seguimiento_efectivo`, ~485 filas ya tageadas con nuestro vocabulario real de Supabase) — la fuente más confiable, es literalmente cómo Diego ya tagea.
2. **Nombres de producto de `recibos procesados.xlsx`** (401 líneas de tickets reales Lidl/Carrefour) — se usan solo como **vocabulario de productos reales que se compran**, ignorando comercios.
3. **Inferencia razonada** para cubrir escenarios que no han pasado todavía pero son plausibles (ej. ropa, regalos, farmacia — poco representados en el histórico pero con etiqueta ya definida en Supabase).

---

## Mecanismo de fallback (importante para el prompt de Gemini)

Este glosario es **sugestivo, no exhaustivo**. Cuando una línea de un recibo no tenga una coincidencia clara aquí, Gemini debe usar su propio razonamiento semántico para elegir la etiqueta que mejor encaje.

**Ejemplo:** Si en un ticket aparece "LENTES PROGRESIVAS" (compra en óptica), ese producto específico puede no estar en los keywords, pero Gemini entiende que es óptica (prescripción) y NO debería tagearla como `tecnologia` (que es para dispositivos electrónicos). Usará el contexto del ticket (tienda de óptica) y los principios del glosario para elegir una categoría más aproppiada (en este caso, probablemente descarte la línea o la tag como `otros`).

---

## Nota sobre categoría: no siempre es 1:1 con la etiqueta

El histórico real muestra que una misma etiqueta puede vivir bajo más de una categoría según el contexto (ej. `cafeteria` aparece tanto en `alimentacion` como en `entretenimiento`; `cerveza o copa` aparece en `alimentacion` y en `entretenimiento`).

Esto es normal y por eso Gemini recibe AMBOS datos:
- La **categoría** ya elegida por Diego en el Shortcut (presupuesto fijo)
- El **glosario completo** con todas las etiquetas y sus keywords

Gemini usa ambos como contexto para elegir la mejor etiqueta, pero no está forzado a seguir la categoría si el producto es claramente otra cosa.

---

## Casos ambiguos conocidos (sin regla dura a propósito)

Algunos productos no tienen una única etiqueta correcta — forzar una sería inventar certeza donde no la hay. Se documentan para que Gemini use el contexto completo del ticket (comercio, resto de líneas, fecha, etc.) antes de decidir:

- **Impresora 3D**: podría ser `tecnologia` (es electrónica) o `hobbies` (uso típico creativo/maker). Sin regla fija — depende del contexto del ticket (tienda de electrónica vs. tienda de manualidades).

- **"Lentes de visión" tipo Vision Pro**: si es el dispositivo (gafas de realidad aumentada) → `tecnologia`. Si en realidad es una compra de óptica (lentes graduados/gafas de vista) → probablemente no debería estar en sistema financiero (es prescripción médica), o taggearla como `otros` con aclaración.

- **Café en barra vs. café en máquina expendedora vs. café en supermercado**: todos se tagean igual (`cafeteria` si es consumo en el momento, `despensa` si es producto empaquetado para llevar), pero el contexto del ticket y el nombre de la tienda ayudan a Gemini.

---

## Líneas de descuento/promo/devolución

**Regla actual:** Netear el descuento en el precio final de la línea que descuenta (evita filas de céntimos sueltos). No registrar cada descuento como su propia línea negativa.

**Ejemplo:**
- Línea 1: "JAMÓN SERRANO 500G" → importe: 12.50
- Línea 2: "PROMOCIÓN -2.50" → se resta del precio anterior
- **Resultado en base de datos:** 1 línea de jamón con importe 10.00 (12.50 - 2.50)

---

## Glosario por etiqueta

⚠️ **Este glosario se genera dinámicamente en runtime desde Supabase y no se muestra aquí en markdown**.

Para ver la lista completa de etiquetas activas y sus keywords, consulta directamente en Supabase:
```sql
SELECT nombre_etiqueta, keyword 
FROM etiquetas 
WHERE estado = 'activa' 
ORDER BY nombre_etiqueta;
```

O espera a que el workflow de sync (`sync_etiquetas.yml`) genere `config/etiquetas.json`, que contiene la versión en caché:
```bash
cat config/etiquetas.json | jq '.etiquetas'
```

---

## Historial de cambios

- **12 de septiembre 2026**: Refactorización a etiquetas dinámicas. Tabla migrada a Supabase `etiquetas.keyword`. Workflow `sync_etiquetas.yml` ejecuta cada 3 días. Este markdown pasa a ser documentación de referencia (read-only).
