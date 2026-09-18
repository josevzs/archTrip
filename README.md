# archTrip

Código: https://github.com/josevzs/archTrip · Autor: José Vargas-Zúñiga Soldevila, 2026 · [Ko-fi](https://ko-fi.com/josevzs)

Herramienta sencilla para preparar viajes de arquitectura con alumnos: se suben dos Excel
(la ruta de ciudades y el listado de hitos arquitectónicos) y la app permite **curar** los
hitos —ver de un vistazo arquitecto y edificio, enlaces a ArchDaily / Arquitectura Viva /
imágenes, y el tiempo en coche desde la ciudad de la ruta más cercana— marcando cada uno
como *curado*, *posible* o *descartado*.

## Arrancar

### En local (sin Docker)

```
python -m venv .venv
.venv\Scripts\activate          # Windows   ·   source .venv/bin/activate en Linux/Mac
pip install -r requirements.txt
python app.py
```

Abre http://localhost:8000. Los datos se guardan en `data/archtrip.db` (SQLite).

### Con Docker

```
docker compose up -d --build
```

Escucha en `127.0.0.1:8000`; la base de datos queda en `./data/` fuera del contenedor.

## Uso

1. **Nuevo viaje**, con dos caminos:
   - **Asistido por IA**: descargas las dos plantillas, copias el texto del encargo (también
     descargable como `.md`) y se lo pegas a ChatGPT/Claude/Gemini junto con los dos archivos
     y tu ruta aproximada. El encargo le explica que busque todos los hitos de interés real
     para un grupo de arquitectura, que marque en la columna Estado los residuales como
     `descartado` sin quitarlos y el resto como `posible`, y cómo escribir las notas
     (prioridad A/B/C, avisos [R]/[E]/[X]). Te devuelve las plantillas rellenas listas para subir.
   - **Creación manual**: viaje vacío; rellenas las plantillas tú.
2. **Las dos plantillas** Excel (también descargables desde el viaje):
   - `plantilla_ruta.xlsx` — `Orden | Ciudad | País | Notas`. Una fila por ciudad o pueblo
     donde se hace parada, en orden.
   - `plantilla_hitos.xlsx` — `Edificio | Arquitecto | Ciudad | Dirección | Año | Latitud |
     Longitud | URL ArchDaily | URL Arquitectura Viva | URL Imagen 1 | URL Imagen 2 | Notas`.
     Solo **Edificio, Arquitecto y Ciudad** son obligatorios; la columna opcional **Estado**
     (`posible` / `descartado`) se aplica solo a hitos nuevos o todavía pendientes — nunca pisa
     el curado hecho en la herramienta. Cada plantilla lleva una hoja "Instrucciones".
3. **Subir** ambos archivos. La app localiza automáticamente cada parada y cada hito en el
   mapa (OpenStreetMap/Nominatim), calcula el tiempo en coche desde la parada más cercana
   (OSRM) y **busca fotos y planos** de cada edificio (Wikidata + Wikimedia Commons). Se ve
   una barra "Localizando…" / "Buscando fotos…"; si se cierra la pestaña, continúa la próxima vez.
4. **Curar** en la vista que convenga (los **descartados se ocultan por defecto** en todas; la casilla
   "ocultar descartados" junto a las pestañas los vuelve a mostrar, y en la ficha "saltar descartados"
   hace que las flechas y las teclas no pasen por ellos):
   - **Fotos** (por defecto): rejilla de tarjetas con la foto principal, agrupadas por parada. En las
     descartadas aparece una papelera 🗑 para eliminarlas del todo.
   - **Arquitectos**: las mismas tarjetas agrupadas por estudio/arquitecto, los más repetidos primero.
   - **Mapa**: paradas numeradas, ruta y un punto por hito coloreado según estado; el popup
     tiene los botones de curado. Los hitos que se solapan se agrupan en un círculo con el
     número y un anillo de colores por estado: un grupo pequeño abre una lista con los botones
     de curado de todos, uno grande acerca el zoom, y los que caen en el mismo punto se
     despliegan en abanico al máximo zoom. El control de capas (arriba a la derecha) cambia el mapa
     base: gris claro/oscuro, topográfico, National Geographic, satélite y calles (Esri),
     OpenStreetMap y su versión humanitaria, relieve (OpenTopoMap) y los mapas del Instituto
     Geográfico de Japón (GSI estándar, pálido y ortofoto); la elección se recuerda. Ninguno
     necesita clave.
   - **Lista**: la ficha completa en texto, con `editar` para corregir datos, poner URLs o
     coordenadas a mano, "Volver a localizar" o "Buscar fotos otra vez".
   - **Ficha** (clic en cualquier foto): la imagen grande, todas las fotos y los planos o
     secciones encontrados, botones grandes "Buscar en Google" / "Buscar en Google Imágenes",
     y curado con teclado: `←` `→` hito anterior/siguiente, `↑` `↓` otra imagen, `C` curar,
     `P` posible, `X` descartar (y pasa al siguiente), `Esc` cerrar. Las miniaturas se
     **reordenan arrastrándolas** (la primera es la foto principal) y "es un plano" / "es una
     foto" cambia una imagen de grupo.
     Cada ficha tiene enlace propio (`#/viaje/<id>/hito/<id>`, botón "enlace" para copiarlo)
     que abre la app directamente en ella. "Quitar esta imagen" descarta una foto equivocada; "añadir foto o plano" acepta una URL
     (clic derecho en cualquier imagen de la web → copiar dirección) o un archivo del ordenador
     (se guarda reducido en `data/uploads/` y viaja dentro de la copia HTML). "Eliminar hito del
     todo" (o la tecla `Supr`) lo quita del viaje definitivamente — descartar solo lo aparta.
   En todas: pestañas por estado, buscador, y los botones ✓ Curar / ? Posible / ✗ Descartar
   (pulsar el activo lo devuelve a pendiente).
5. **Exportar**:
   - **HTML**: un único archivo `viaje-<nombre>.html` que se abre con doble clic en cualquier
     ordenador, sin servidor. Guarda los cambios en el navegador y con **Guardar copia**
     genera un nuevo HTML con el estado actual para compartir.
   - **Obsidian**: un ZIP con una carpeta lista para soltar en un vault: una nota por hito y
     por parada (con propiedades) y un `Viaje.base` (Obsidian Bases) con vistas *Todos*,
     *Curados*, *Posibles* y *Fichas*.

### Detalles que conviene saber

- Volver a subir el Excel de hitos **no borra** nada ni pierde el curado: los hitos se
  emparejan por edificio + arquitecto y se actualizan; los nuevos se añaden. Para empezar de
  cero está el borrado por hito (desde *Descartados* o *editar*).
- Volver a subir la ruta reemplaza las paradas y recalcula todos los tiempos.
- Si el hito no se encuentra en el mapa se usa el centro de la ciudad y el tiempo se marca
  con `≈`. Si tampoco se encuentra la ciudad, aparece ⚠ y se puede corregir en *editar*.
- Si el servicio de rutas no responde, el tiempo se estima por distancia en línea recta
  (×1,3 a 70 km/h) y se marca con `≈`.
- **Enlaces a ArchDaily y Arquitectura Viva**: si las URLs van vacías, la app consulta el
  buscador de cada web (ArchDaily por nombre + arquitecto; AV por la etiqueta del arquitecto y
  título) y guarda el enlace directo a la ficha solo cuando el título coincide sin ambigüedad
  (mismo nombre, arquitecto o lugar). Mientras no se ha comprobado se ofrece la búsqueda; si no
  hay ficha, no aparece botón. Los títulos en español de AV ("Museo del Siglo XXI") no casan con
  nombres en inglés: ahí conviene pegar la URL a mano en *editar*.
- Cada hito enlaza a **Google Maps** (por coordenadas si está localizado) y, si tiene parada,
  a "cómo llegar" con la ruta en coche desde ella.
- Coordenadas y tiempos vienen de servicios públicos gratuitos (Nominatim tiene un límite de
  1 petición/segundo: 100 hitos tardan ~2 minutos en localizarse).
- Las fotos y planos salen de Wikidata (imagen principal, "plan view image", categoría de
  Commons, coordenadas) y de Wikimedia Commons (fotos de la categoría y una búsqueda de
  planos/secciones/alzados dentro de ella). Funciona bien con obras conocidas; para las
  oscuras no encuentra nada o se equivoca de edificio — "buscar fotos otra vez" tras corregir
  el nombre, o "quitar esta imagen". Unas 4 peticiones por hito a 1/s: 100 hitos ≈ 8 min.
  Si el hito no se había localizado con precisión y Wikidata sí lo conoce, se usan sus
  coordenadas y se recalcula el tiempo en coche.
- El mapa carga Leaflet y las teselas de OpenStreetMap por internet (también en la copia HTML).
- Wikimedia pide un contacto en el User-Agent: se puede poner con la variable de entorno
  `ARCHTRIP_CONTACT` (p. ej. una URL o un email).

## Ejemplo real

`ejemplos/japon-2026-ruta.xlsx` y `ejemplos/japon-2026-hitos.xlsx`: un viaje de 10 días
Osaka → Tokio con ~300 hitos. La columna Notas conserva la prioridad del profesor (A/B/C),
los avisos ([R] reserva, [E] solo exterior, [X] no visitable) y sus comentarios. Sirve para
probar la herramienta con volumen real: subirlos a un viaje nuevo y dejar que localice.

## Desarrollo

```
pip install -r requirements-dev.txt
pytest
```

- `archtrip/` — Flask: `db.py` (SQLite), `excel.py` (plantillas y parseo), `geo.py`
  (Nominatim/OSRM), `images.py` (Wikidata/Commons), `enrich.py` (un paso de trabajo por
  llamada), `export.py` (HTML autónomo y ZIP Obsidian), `routes.py` (API `/api/*`).
- `static/index.html` — todo el frontend (CSS y JS inline, sin build). El mismo archivo es
  la base del export HTML: el servidor solo le inyecta los datos en `<!--ARCHTRIP_DATA-->`.
- La base de datos se crea sola al arrancar; `ARCHTRIP_DB` cambia su ruta.

## Licencia

Uso libre y gratuito, sin fines comerciales, conservando la barra de créditos del autor que
muestra la interfaz (también en las copias HTML). Se puede modificar añadiendo coautoría y un
enlace de patrocinio propio después del original. Texto completo en [LICENSE](LICENSE).
