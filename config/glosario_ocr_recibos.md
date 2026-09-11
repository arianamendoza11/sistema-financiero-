# Glosario sugestivo de tageo para OCR de recibos (rama "con foto")

Referencia/contexto para el prompt de Gemini el día que se construya esa rama. No está conectado a ningún workflow todavía — es un glosario sugestivo de primera pasada, no una tabla de reglas rígidas.

## Principio no negociable: se tagea por elemento de línea, nunca por origen de compra

Cada línea del ticket se clasifica por **lo que ES el producto**, sin importar en qué comercio se compró. Un supermercado puede vender comida (`despensa`), alcohol (`cerveza o copa`), limpieza (`limpieza del hogar`) y tecnología (`tecnologia`) en el mismo ticket — el comercio no predice nada sobre la línea. (Ejemplo real que desmontó la versión anterior de este documento: un ron comprado en Lidl es `cerveza o copa`, no `despensa`, aunque el ticket entero sea de supermercado.)

## Cómo se construyó este glosario

Tres fuentes, en este orden de confianza:
1. **Antecedentes reales del sistema** (`seguimiento_efectivo`, ~485 filas ya tageadas con nuestro vocabulario real de Supabase) — la fuente más confiable, es literalmente cómo Diego ya tagea.
2. **Nombres de producto de `recibos procesados.xlsx`** (401 líneas de tickets reales Lidl/Carrefour) — se usan solo como **vocabulario de productos reales que se compran**, ignorando por completo las categorías que trae ese Excel (están fuera del maestro de tags de este sistema, no son fiables).
3. **Inferencia razonada** para cubrir escenarios que no han pasado todavía pero son plausibles (ej. ropa, regalos, farmacia — poco representados en el histórico pero con etiqueta ya definida en Supabase).

## Mecanismo de fallback (importante para el prompt de Gemini)

Este glosario es **sugestivo, no exhaustivo**. Cuando una línea de un recibo no tenga una coincidencia clara aquí, Gemini debe usar su propio razonamiento semántico para elegir la etiqueta que mejor encaje **dentro del vocabulario real de Supabase** (`etiquetas.nombre_etiqueta` vigentes) — nunca inventar una etiqueta nueva, y nunca dejar de asignar una por no encontrarla en esta lista.

## Nota sobre categoría: no siempre es 1:1 con la etiqueta

El histórico real muestra que una misma etiqueta puede vivir bajo más de una categoría según el contexto (ej. `cafeteria` aparece tanto en `alimentacion` como en `entretenimiento`; `cerveza o copa` igual; `transportes varios` aparece en `transporte`, `entretenimiento`, `regularizacion_papeles` y `salud_belleza`). No hay que forzar una categoría única — el criterio real es la intención del gasto, no una tabla fija.

## Glosario por etiqueta

| Etiqueta | Categoría(s) vista(s) en histórico | Keywords / ejemplos reales y plausibles |
|---|---|---|
| `despensa` | alimentacion | pan, carne, pescado, filete, pollo, fruta, verdura, huevos, lácteos, leche, yogur, queso, arroz, pasta, aceite, atún, condimentos, snacks, agua, refrescos, café molido, cacao, cereales, congelados, embutidos, mariscos, especias, harina, azúcar, sal, vinagre, salsas, comida para mascotas, cerveza sin alcohol — compra de supermercado para casa |
| `comida fuera` | alimentacion, entretenimiento | menú del día, kebab, pizza, hamburguesa, mcdonalds, bocadillo, tacos, pollo a la brasa, comida para llevar, restaurante, comida en el trabajo, sushi, poke bowl, delivery (Glovo/Uber Eats), catering, brunch, food truck |
| `cafeteria` | alimentacion, entretenimiento | café, capuchino, desayuno de cafetería, cruasán, mollete, infusión, cápsulas de café de oficina, té, chocolate caliente, bollería, sándwich de máquina, snack de vending |
| `cerveza o copa` | alimentacion, entretenimiento | cerveza, caña, vino, ron, whisky, vodka, ginebra, cubata, copa, sidra, cava, botella de licor, cóctel, champán, cerveza artesanal, mezcal, tequila, absenta — **incluye alcohol comprado en supermercado**, no solo en bares |
| `articulos del hogar` | hogar | ambientador, bolsas de basura/plástico, papel higiénico, papel de cocina, fundas de almohada, sábanas, ganchos, enchufes, saco de dormir, vajilla, utensilios de cocina, menaje, decoración, velas, plantas de interior, macetas, organizadores, muebles pequeños, electrodomésticos de cocina (tostadora, batidora) |
| `limpieza del hogar` | hogar | detergente, lejía, friegasuelos, quitagrasas, estropajo, bayetas, lavavajillas, suavizante, desinfectante, limpiacristales, quitamanchas |
| `lavanderia` | hogar | lavandería, lavado y secado, tinte de ropa, tintorería, arreglo de ropa/sastre, planchado |
| `bricolaje/mantenimiento` | deporte, hogar | herramientas, tornillos, material de reparación, pintura, bombillas, taladro, brocas, silicona, cinta aislante, electricista/fontanero a domicilio, repuestos de bicicleta |
| `cuidado personal basico` | salud_belleza | champú, gel de baño, jabón, desodorante, cepillo de dientes, pasta dental, toallitas, colonia/perfume, maquinillas de afeitar, protector labial, crema hidratante, crema solar, hilo dental, algodón, bastoncillos |
| `farmacia` | salud_belleza | medicamentos, ibuprofeno, paracetamol, test de embarazo, preservativos, insecticida, gotas, jarabe, vitaminas/suplementos, mascarilla FFP2, termómetro, tiritas, alcohol en gel, test de antígenos, anticonceptivos |
| `parafarmacia` | salud_belleza | cremas dermatológicas, mascarillas faciales, productos aclaradores, tratamientos de piel, sérum facial, protector solar facial premium, suplemento de colágeno, tratamiento capilar |
| `salud de los pies` | salud_belleza | crema de pies, cortaúñas, tratamiento de hongos, plantillas ortopédicas, calcetines de compresión, callicida |
| `barberia` | salud_belleza | corte de pelo, barbería, arreglo de barba, tinte de barba, manicura, pedicura |
| `ropa varios` | ropa_calzado | ropa interior, calcetines, guantes, zapatillas, pasadores, paraguas, camisetas, abrigos, chaqueta, vestido, traje, ropa deportiva, calzado de vestir, cinturón, bufanda, ropa de segunda mano |
| `articulos de deporte` | deporte | material deportivo, mancuernas, magnesio, alquiler de equipo (esquí), ropa técnica, cantimplora, bicicleta, patines, balón, raqueta, proteína en polvo/suplemento deportivo |
| `entrada` | deporte, entretenimiento | entrada de cine, concierto, museo, piscina, forfait de pista, evento, **festival**, teatro, parque de atracciones, partido/estadio, exposición |
| `hobbies` | entretenimiento | instrumento musical, juego de mesa, material de manualidades, videojuego, material de pintura, equipo de fotografía, drone |
| `tecnologia` | gastos_varios | altavoz, cargador, cable, auriculares, funda de móvil/tablet, power bank, alfombrilla, accesorios electrónicos, móvil, portátil, tablet, gafas de realidad aumentada/virtual (ej. Vision Pro), monitor, teclado, ratón, disco duro, router, smartwatch, impresora |
| `transportes varios` | transporte, entretenimiento, regularizacion_papeles, salud_belleza | taxi, uber, cercanías, autobús suelto, gasolina, peaje, billete de tren, bicimad, parking, alquiler de coche, patinete compartido, ferry, billete de avión puntual — viajes puntuales, no el abono mensual |
| `regalos` | regalos | papel de regalo, bolsa de regalo, detalle para alguien, tarjeta de felicitación, flores, tarjeta regalo/gift card |
| `donaciones` | gastos_varios | donativo, colecta benéfica, ONG, crowdfunding |
| `drogas` | entretenimiento, gastos_varios | Lsd (histórico), marihuana, hachís, cocaína, MDMA/éxtasis, ketamina, hongos alucinógenos, popper |
| `otros` | entretenimiento, gastos_varios, ropa_calzado | guardarropa, servicios de compañía/acompañante (escort), masajes, apuestas/lotería, propinas sin categoría clara — catch-all cuando de verdad no encaja en ninguna otra |

## Casos ambiguos conocidos (sin regla dura a propósito)

Algunos productos no tienen una única etiqueta correcta — forzar una sería inventar certeza donde no la hay. Se documentan para que Gemini use el contexto completo del ticket (comercio, resto de líneas) en vez de decidir por la palabra suelta:

- **Impresora 3D**: podría ser `tecnologia` (es electrónica) o `hobbies` (uso típico creativo/maker). Sin regla fija — depende del contexto del ticket (tienda de electrónica vs. tienda de manualidades/maker).
- **"Lentes de visión" tipo Vision Pro**: si es el dispositivo (gafas de realidad aumentada) → `tecnologia`. Si en realidad es una compra de óptica (lentes graduados/gafas de vista) → **no existe etiqueta de óptica/salud visual en el sistema hoy**; lo más cercano sería `farmacia`, pero habría que decidir si conviene crear una etiqueta nueva el día que esto pase de verdad.

## Líneas de descuento/promo/devolución

Pendiente de decisión con Diego: netear el descuento en el precio final de la línea que descuenta (evita filas de céntimos sueltos) vs. registrar cada descuento como su propia línea negativa con la misma etiqueta que el producto descontado.
