"""The 'prompt para IA': instructions a professor hands to any AI assistant so it produces
the two Excel files (route + landmarks) the way this tool expects, pre-curated."""
from . import excel

STATUS_HELP = "posible | descartado  (curado lo decide el profesor: no lo uses)"


def build():
    headers_route = " | ".join(h for h, *_ in excel.ROUTE_COLUMNS)
    headers_lm = " | ".join(h for h, *_ in excel.LANDMARK_COLUMNS)
    route_block = ("VIAJE: [nombre del viaje]\n"
                   "RUTA APROXIMADA (en orden): [ciudad 1 → ciudad 2 → ciudad 3 … — rellena aquí]\n"
                   "DÍAS DISPONIBLES: [indica aquí el número de días]")
    return f"""# Encargo: lista de hitos arquitectónicos para un viaje de estudiantes de arquitectura

Vas a preparar el material de trabajo de un viaje de estudios de arquitectura (grupo universitario,
profesores de proyectos) para la herramienta archTrip. Te adjunto **dos plantillas Excel**:
`plantilla_ruta.xlsx` y `plantilla_hitos.xlsx`. Devuélvemelas rellenas, con los **mismos nombres de
columna**, sin celdas combinadas, en UTF-8, una fila por hito. La segunda hoja de cada plantilla
("Instrucciones") explica cada columna.

{route_block}

## 1. Qué tienes que hacer

1. **Ruta** (`plantilla_ruta.xlsx`): una fila por ciudad o pueblo donde se hace parada o noche, en
   orden. Columnas: {headers_route}. Si la ruta que te doy es aproximada, propón el orden más
   lógico en tren/coche y explícalo en Notas.

2. **Hitos** (`plantilla_hitos.xlsx`): busca **TODOS los lugares de interés arquitectónico REAL**
   que estén al alcance de esa ruta: obras canónicas históricas, modernas y contemporáneas,
   conjuntos urbanos, arquitectura vernácula, paisaje construido, infraestructuras, casas
   experimentales, campus, museos por su arquitectura (no por sus colecciones). Sé exhaustivo:
   la idea es **ver todo lo posible**; no hay límite de filas. No incluyas monumentos que solo
   son turísticos sin aportación arquitectónica, ni obras inventadas: cada fila debe existir y
   ser localizable.

3. **Precurado**: la herramienta tiene tres estados. Tú solo usas dos:
   - `descartado` → obra **residual o secundaria para el tiempo disponible** (segunda fila de un
     mismo autor, interés solo local, desvío que no compensa, no visitable). **Inclúyela igualmente**
     en el archivo: el profesor quiere verlas y decidir.
   - `posible` → **todo lo demás**.
   No marques nada como `curado`: eso lo decide el profesor en la herramienta.

## 2. Cómo rellenar cada columna de `plantilla_hitos.xlsx`

Columnas: {headers_lm}

- **Edificio**: nombre por el que se conoce la obra, sin paréntesis ni añadidos (los nombres
  alternativos van en Notas). Usa el nombre con el que aparece en mapas y en la bibliografía
  (para Japón, el nombre inglés o romanizado; para Europa, el local o el castellano habitual).
  La herramienta lo usa para localizar el edificio y buscar fotos y planos: cuanto más canónico
  el nombre, mejor.
- **Arquitecto**: autor o estudio. Para obras sin autor conocido: `Tradicional · época` (p. ej.
  `Tradicional · s. XVII`, `Vernácula`, `Meiji`).
- **Ciudad**: el municipio donde está (no la región), en castellano cuando exista forma habitual
  (Kioto, Tokio, Oporto, Múnich…). Es la clave para calcular tiempos de desvío desde la ruta.
- **Dirección**: solo si la sabes con certeza; mejora la localización. Si no, vacío.
- **Año**: año de terminación (solo informativo).
- **Latitud / Longitud**: solo si las conoces con certeza (grados decimales). Si dudas, vacío:
  la herramienta las busca.
- **URL ArchDaily / URL Arquitectura Viva / URL Imagen 1 / URL Imagen 2**: solo enlaces que
  hayas verificado. Si no, vacío: la herramienta busca fichas e imágenes por su cuenta.
- **Notas**: empieza SIEMPRE por la prioridad y los avisos, y sigue con un motivo breve:
  `A [R] · motivo`. Escala: **A** justifica organizar o desviar el viaje; **B** muy recomendable
  si se está en la zona; **C** para completistas. Avisos: **[R]** hace falta reserva o tiene
  calendario especial; **[E]** solo se ve el exterior (viviendas privadas, oficinas); **[X]**
  actualmente no visitable. Añade después lo que un profesor de proyectos querría saber: qué
  mirar, qué relación con la ruta, si supone un desvío y cuánto.
- **Estado**: {STATUS_HELP}.

## 3. Criterios de calidad

- Agrupa mentalmente por ciudad de la ruta; incluye los desvíos razonables (hasta ~1 h) y
  márcalos como tales en Notas; los desvíos largos, solo si son obras de primer orden.
- Piensa en un grupo: horarios, accesos, cierres semanales, edificios que solo se visitan en
  días concretos → [R]; viviendas → [E].
- Verifica que sigue en pie y visitable; si dudas, dilo en Notas.
- Nada de duplicados: un hito por fila (un conjunto —campus, barrio, museo con varios
  pabellones— puede ir en una sola fila si se visita de una vez).
- Cantidad orientativa: entre 20 y 60 hitos por ciudad grande, 3-15 por ciudad pequeña, según
  el patrimonio real. Mejor una lista larga bien precurada que una corta.

## 4. Entrega

Los dos archivos `.xlsx` rellenos (mismos encabezados, hoja "Instrucciones" intacta o eliminada,
da igual). Si no puedes generar Excel, entrega dos tablas CSV con esas mismas columnas y en ese
orden, separadas por punto y coma, para que yo las pegue en las plantillas.
"""
