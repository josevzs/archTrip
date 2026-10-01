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
   (pulsar el activo lo devuelve a pendiente). La pestaña **Sin localizar** reúne los hitos cuyo
   punto no es de fiar —se usó el centro de la ciudad, no se encontró nada o aún no se ha
   buscado— para corregirlos de una sentada con *editar* o *Volver a localizar*. En el mapa,
   filtrar, buscar u ocultar descartados **no cambia el encuadre**: solo se repintan los puntos,
   y si algo queda fuera de pantalla la leyenda lo dice y **ajustar vista** vuelve a encuadrar.
5. **Itinerario** (la quinta pestaña): monta el viaje día a día.
   - **Añadir día** crea el día siguiente al último (el primero toma la fecha de hoy). Cada día
     lleva **fecha** de calendario, un **título** opcional («Llegada», «Nara → Kioto»), la
     **ciudad base** — una parada de la ruta, es decir dónde se duerme — y una **nota del día**
     (hotel, reservas, avisos). Los días se ordenan solos por fecha; los que aún no la tienen
     quedan al final.
   - **＋ Hito** abre un buscador entre los hitos del viaje (los descartados solo salen si los
     buscas por nombre) y avisa si ese hito ya está puesto en otro día. **＋ Nota** añade un
     bloque de texto libre para lo que no es un edificio: un tren, una comida, un aviso.
   - Cada línea puede llevar **hora** (opcional) y se **reordena arrastrando ⠿**, también de un
     día a otro. **Ordenar por hora** recoloca de golpe las que tengan hora.
   - **Asignar en serie, sin abrir el itinerario**: en cuanto hay días, encima de la lista
     aparece una **barra de días** (`D1 · 12 abr (4)`). Eliges el día y luego vas pulsando el
     botón **＋ D1** que llevan todas las fichas —en Fotos, en Arquitectos, en Lista, en el
     popup del mapa y en la ficha grande—; el botón se queda marcado (`D1 ✓`) y volver a
     pulsarlo lo saca. La cuenta de la barra se actualiza sola, así que se puede ir día por día
     repasando el mapa o la parrilla de fotos. Con **▾ ocultar** la barra se pliega a una línea
     (`1 de 5 hitos repartidos en 2 días`) y desaparecen los botones ＋ de las fichas, para no
     estorbar mientras se cura; se recuerda plegada hasta que se vuelva a abrir.
   - En el **mapa**, los hitos que ya están metidos en algún día se pintan **rellenos y con el
     número de su día dentro**; los que faltan quedan **huecos**, con su leyenda. De un vistazo se
     ve cómo va quedando repartido cada día por el territorio y qué queda por colocar.
   - Desde la **ficha** de cualquier hito hay además un desplegable «añadir a un día…», y dice
     en qué días está ya metido.
   - Cada línea enseña **cómo está curado el hito** —punto de color y etiqueta: *fijo* (verde,
     curado), *opcional* (ámbar, posible), *sin curar*— y lleva un botón **⏳** para marcarla como
     **pendiente de permiso o confirmación** (visitas con reserva, permisos de acceso…). La línea
     marcada se resalta y lo dice en claro, también en los PDF.
   - Quitar una línea (✕) no borra el hito del viaje, solo lo saca de ese día; borrar un día
     tampoco borra sus hitos.
   - Las fechas y las horas se guardan solas al terminar de escribirlas: el día no se recoloca
     hasta que se sale del campo, así se puede teclear un año entero sin que la lista salte.
6. **Exportar**:
   - **Itinerario (PDF)**, en dos versiones: **día a día** (compacto, para imprimir o mandar a
     los alumnos) y **con fotos** (el mismo, con la foto de cada hito al lado, todas al mismo
     ancho y sin deformar: las verticales salen más altas). El de fotos
     avisa de lo que está haciendo: primero descarga las que falten, de una en una y con barra
     de progreso —«Descargando fotos para el PDF… 7 de 34», con botón de cancelar—, y después
     genera el documento. Las fotos quedan guardadas en `data/cache/photos`, así que la segunda
     vez sale al momento. Esa carpeta es desechable: se puede borrar y se vuelve a llenar sola. Los dos llevan las horas, el arquitecto y el edificio, ciudad, año,
     tiempo en coche desde la base, las notas, la etiqueta de curado (*fijo* / *opcional*) y el
     aviso de *pendiente de confirmar*, con su leyenda arriba. No llevan la barra de créditos de
     la aplicación: solo un pie discreto de «generado con el sistema archTrip el …» y la paginación.
   - **HTML**: un único archivo `viaje-<nombre>.html` que se abre con doble clic en cualquier
     ordenador, sin servidor. Guarda los cambios en el navegador y con **Guardar copia**
     genera un nuevo HTML con el estado actual para compartir.
   - **Obsidian**: un ZIP con una carpeta lista para soltar en un vault: una nota por hito y
     por parada (con propiedades), un `Itinerario.md` con los días enlazando a cada nota y un
     `Viaje.base` (Obsidian Bases) con vistas *Todos*, *Curados*, *Posibles* y *Fichas*.

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
- El itinerario viaja dentro de la copia HTML autónoma, pero ahí solo se lee: los días se
  editan en la app.
- El mapa carga Leaflet y las teselas de OpenStreetMap por internet (también en la copia HTML).
- Wikimedia pide un contacto en el User-Agent: se puede poner con la variable de entorno
  `ARCHTRIP_CONTACT` (p. ej. una URL o un email).

## Ejemplo real

`ejemplos/japon-2026-ruta.xlsx` y `ejemplos/japon-2026-hitos.xlsx`: un viaje de 10 días
Osaka → Tokio con ~300 hitos. La columna Notas conserva la prioridad del profesor (A/B/C),
los avisos ([R] reserva, [E] solo exterior, [X] no visitable) y sus comentarios. Sirve para
probar la herramienta con volumen real: subirlos a un viaje nuevo y dejar que localice.

## Variantes y viajes solo en local

- **Duplicar** (botón en la lista de viajes) crea una **variante** con las mismas paradas, los
  mismos hitos —con su curado, sus fotos y sus tiempos— y el mismo itinerario, pero
  independiente: tocar la copia no mueve el original ni al revés. Sirve para probar otro
  recorrido sin arriesgar el viaje bueno.
- Un viaje puede quedarse **solo en local**: se ve desde la red de casa pero **no existe** para
  quien entre por el enlace de fuera (el túnel de Tailscale) — ni en la lista, ni abriéndolo por
  su dirección, ni en las exportaciones. Las variantes nacen así, y se publican con un clic
  («Publicar» en la lista). La etiqueta amarilla *solo en local* lo recuerda.
- La página de administración tampoco responde desde fuera, ni con la contraseña correcta.
- Se distingue por el nombre con el que se pide la página: lo que acabe en `.ts.net` cuenta como
  «desde fuera». Se puede cambiar con la variable de entorno `ARCHTRIP_PUBLIC_HOSTS`
  (lista separada por comas).

## Copias de seguridad

`scripts/backup.sh` guarda en `data/backups/` una instantánea consistente de la base de datos
(`auto-<fecha>.db`), las fotos subidas y una copia HTML autónoma de cada viaje — **solo si el
contenido ha cambiado** desde la última copia automática (se compara un hash del volcado, no la
fecha) — y conserva las 7 últimas distintas (`KEEP=n` para cambiarlo). Programado dos veces al día
con cron: `0 3,15 * * * /srv/apps/archtrip/scripts/backup.sh >> …/data/backups/backup.log`.
Los archivos sin el prefijo `auto-` (copias manuales) no se tocan. Restaurar: desde la página de
administración (abajo), o a mano: parar el contenedor, copiar el `.db` elegido sobre
`data/archtrip.db` (y descomprimir el `.tgz` en `data/` si hace falta) y arrancar.

## Sesiones de edición y página de administración

Cada cambio que alguien hace (curar, editar, borrar, subir plantillas, añadir o quitar fotos,
montar el itinerario, renombrar o borrar viajes…) queda anotado en un diario con lo necesario
para deshacerlo. Mirar
no deja rastro: la **sesión** de un navegador se abre con su primer cambio (se identifica con
una cookie, la IP y el navegador) y se da por cerrada tras **una hora sin cambios**. Los
archivos de fotos borradas se conservan en `data/uploads/` por ese mismo motivo.

La página de administración no tiene enlace: **triple clic sobre «archTrip» en el pie de
página**, o `#/admin` en la dirección. Pide contraseña (`admin` por defecto; cámbiala con la
variable de entorno `ARCHTRIP_ADMIN_PASSWORD`, por ejemplo en `docker-compose.yml`). Desde ahí:

- ver las sesiones (quién, cuándo, qué viajes, resumen de acciones) y ponerles nombre;
- abrir una sesión y **deshacer** un cambio suelto o **toda la sesión** de golpe (del último al
  primero). Cada deshacer queda también registrado y se puede volver a deshacer;
- buscar el **historial de un hito** por nombre o número: cuándo y en qué sesión se tocó;
- **copias de seguridad**: crear una ahora, descargar cualquiera (`.db` o HTML autónomo),
  descargar un volcado SQL de la base actual, o **restaurar** una copia `.db` (antes se guarda el
  estado actual como `pre-restore-<fecha>.db`, así que también es reversible).

El diario vive en la misma base de datos (tablas `sessions` y `changes`), así que entra en las
copias de seguridad.

## Desarrollo

```
pip install -r requirements-dev.txt
pytest
```

- `archtrip/` — Flask: `db.py` (SQLite), `excel.py` (plantillas y parseo), `geo.py`
  (Nominatim/OSRM), `images.py` (Wikidata/Commons), `enrich.py` (un paso de trabajo por
  llamada), `export.py` (HTML autónomo y ZIP Obsidian), `routes.py` (API `/api/*`),
  `audit.py` (sesiones y diario de cambios), `admin.py` (API `/api/admin/*`: deshacer, copias).
  El itinerario son dos tablas (`trip_days`, `day_items`) y su API `/api/days/*` y `/api/items/*`.
- `static/index.html` — todo el frontend (CSS y JS inline, sin build). El mismo archivo es
  la base del export HTML: el servidor solo le inyecta los datos en `<!--ARCHTRIP_DATA-->`.
- La base de datos se crea sola al arrancar; `ARCHTRIP_DB` cambia su ruta.

## Licencia

Uso libre y gratuito, sin fines comerciales, conservando la barra de créditos del autor que
muestra la interfaz (también en las copias HTML). Se puede modificar añadiendo coautoría y un
enlace de patrocinio propio después del original. Texto completo en [LICENSE](LICENSE).
