# Glosario de etiquetas (referencia)

El glosario vive en Supabase: tabla `etiquetas`, columnas `keyword` (ejemplos de
productos), `criterio` (una frase que define la etiqueta y la distingue de sus
hermanas, p. ej. despensa = "para comer en casa" vs cafeteria = "servido en un
bar") y `posibles_categorias`. Es la única fuente: los scripts lo leen en
vivo en cada ingesta, sin caché. Este archivo es solo documentación; ningún
script lo lee.

## Cómo lo usa cada ingesta

- **Gasto dinámico (sin foto):** keyword matching determinista acotado a la
  categoría elegida en el Shortcut. Sin coincidencia → `registros_pendientes`.
- **Gasto por foto:** el prompt se construye en `scripts/ingesta_foto.py` con un
  MENÚ numerado de pares categoría / etiqueta, limitado a las categorías que
  llegan del Shortcut, con su criterio y las keywords como ejemplos. El modelo
  elige un número por producto. Si las keywords reconocen el producto (primero
  en el texto literal del ticket, luego en el nombre que puso el modelo), el
  glosario se impone a la elección del modelo. Los importes se verifican contra
  el TOTAL del ticket.

## Cómo enseñar un producto nuevo

Añade el elemento (no la frase entera: "parodontax", no "compré parodontax en
Carrefour") al array `keyword` de su etiqueta en Supabase. Vale para la
siguiente ingesta, sin despliegue.

```sql
SELECT nombre_etiqueta, posibles_categorias, keyword
FROM etiquetas WHERE estado = 'activa' ORDER BY nombre_etiqueta;
```

## Principios

- Cada línea se clasifica por lo que ES el producto, no por el comercio.
- Descuentos y promociones se netean en la línea del producto al que se aplican.
- Las categorías las fija Ariana en el Shortcut (presupuesto); la ingesta solo
  reparte productos entre esas categorías.
