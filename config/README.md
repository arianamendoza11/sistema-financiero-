# Plantillas de ingesta estandarizada

`plantillas_estandarizadas.json` define, por tipo de ingesta fija, todo lo que la ingesta dinámica NO necesita preguntar en el Shortcut. Se edita a mano cuando algo cambia (proveedor, cuenta de pago, etc.) — nunca se toca el código del workflow para eso.

## Versionado (igual que `categorias` en Supabase)

Cada tipo de ingesta es un **array** de versiones con `fecha_inicio`/`fecha_fin`. El workflow resuelve la versión vigente así:

```
fecha_inicio <= fecha_operacion AND (fecha_fin IS NULL OR fecha_operacion <= fecha_fin)
```

Si mañana cambias de proveedor de móvil o de cuenta de pago: **no edites la versión existente**, ciérrala poniendo `fecha_fin` y añade una nueva versión al array. Así el histórico sigue resolviendo con la plantilla que estaba vigente en cada fecha.

## Qué NO lleva el JSON (y por qué)

- **`tipo`**: no existe en ninguna plantilla. Se deriva siempre por lookup de `categorias.tipo` según el `codigo_categoria` de la fila. Es un invariante de la base (verificado: `tipo` de toda fila en `seguimiento_efectivo` = `tipo` de su categoría, sin excepciones).
- **`medio_pago`**: tampoco existe aquí. Lo pone un trigger en Supabase (`trg_set_medio_pago`) a partir de la cuenta usada (`credito` solo si es Interbank Credito, `debito` en el resto). Nunca lo decide un script.
- **`id_categoria` como ID fijo**: nunca. Solo se guarda `codigo_categoria` (el slug). El `id_categoria` real se resuelve en el momento del insert consultando `categorias` por `codigo_categoria` + vigencia en `fecha_operacion` (las categorías tienen SCD2: mismo código, distinto id según el rango de fechas — ver caso `renta`, que cambió de CT000001 a CT000002 en julio).

## Convención de campos por plantilla

| Campo | Significado |
|---|---|
| `id_cuenta` | ID fijo si la cuenta nunca cambia para ese tipo. **`null`** = el Shortcut manda el **nombre** de la cuenta como texto (ej. `"BBVA Esp"`, el usuario nunca ve ni escribe un ID), y el script lo resuelve contra `cuentas.nombre_cuenta` en el momento del insert. |
| `codigo_categoria` | Slug de la categoría; el id real se resuelve en Supabase por fecha vigente. |
| `id_etiqueta` | Las etiquetas no tienen SCD2, así que aquí sí va el ID directo. |
| `importe_fijo` | Número si el monto nunca cambia. **`null`** = lo aporta el Shortcut (monto variable). |
| `signo` | Solo presente cuando `importe_fijo` es `null` (movil, renta, pago_tarjeta). El Shortcut siempre manda el monto en positivo; `signo` (`1`/`-1`) le dice al script si esa plantilla es una salida o una entrada de dinero. Evita que el script asuma "esto siempre es un gasto" a lo bruto. |
| `comentario_fijo` | Texto literal fijo para el comentario. |
| `comentario_regla` | Nombre de una regla que vive en el código Python del workflow (hoy solo `condicional_umbral_8`: si `abs(importe) > 8` → `"Pago con adicionales"`, si no → `"Pago exacto"`). |
| `comentario_fuente` | Ruta a un dato ya existente en Supabase que hay que leer en el momento del insert (hoy solo `categorias.glosa`, la dirección vigente de la renta). |
| `comentario_desde_shortcut` | `true` = el comentario lo escribe el usuario a mano en el Shortcut (hoy solo ahorro: justificación del aporte/retiro). |

Si un campo (`id_cuenta`, `importe_fijo`) es `null` y no hay ningún `comentario_*`, el dato correspondiente lo pide el Shortcut. Nunca se pide algo que ya se puede resolver por otra vía — es la regla de "mínimo dato posible" del diseño.

## Caso especial: `ahorro_aporte` / `ahorro_retiro`

Son los únicos tipos que generan **2 filas** en `seguimiento_efectivo` (traspaso entre bolsillos). Cada versión tiene un array `filas` con rol `origen`/`destino`. `signo` indica si el monto recibido del Shortcut se aplica en negativo o positivo en esa fila. La cuenta con `id_cuenta: null` es la que el usuario elige en el Shortcut (el "bolsillo disponible"); la cuenta fija `CU000005` es siempre la cuenta Ahorros real.

## Caso especial: `pago_tarjeta`

`id_cuenta: null` porque la cuenta de origen del pago varía (hoy BBVA, pero puede ser cualquier disponible). `importe_fijo: null` porque el monto del pago varía cada vez. El comentario quedó fijo como en el histórico (`"Pago de tarjeta de credito | via Remitly"`) aunque Remitly ya no exista como cuenta — es texto descriptivo, no una referencia funcional.
