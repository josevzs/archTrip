\# Roadmap



Esto debe ser una herramienta sencilla y ligera, pensada para:

Facilitar a mis profes de arquitectura, que son bastante booomers, el diseño de los viajes que organizan para sus alumnos. La herramienta debe:



\## Casos de uso

1. El sistema debe tener una plantilla de Excel fácilmente descargable para dos cosas:

   1. Ruta de ciudades y pueblos
   2. Listado completo de landmarks arquitectónicos de interés, con la información asociada necesaria para el sistema
2. Una vez introducidos \*\*rellenos\*\* esas dos plantillas, el sistema debe permitir:

   1. Hacer un "curado" de landmarks de interés

      1. Debe poder verse MUY fácilmente arquitecto y nombre del edificio
      2. Debe tener enlazadas página de archdaily, arquitecturaviva, y debe tener links a imágenes que de un vistazo permitan ver cómo es un proyecto
      3. Debe tener una estimación de tiempo en coche desde el centro de la ciudad de ruta más cercana al hito. 
      4. Debe tener botones de curado, eliminación directa, posible opción, etc



\## Estructura técnica

0\. El sistema debe ser muy fácil de usar, intuitivo, y minimalista, sin nada sofisticado

1. El tema será claro, el diseño muy minimalista y limpio, en Courier
2. El sistema debe poder alojarse fácilmente como un webserver local, con opción de hacerlo vía Docker
3. Idealmente, el sistema debe poder exportarse en un estado completo en un archivo fácilmente ejecutable por cualquiera, que además debe recordar las modificaciones que haga el usuario
4. Idealmente, el sistema debe poder exportar toda la información de ruta y estados como archivos compatibles con Obsidian Bases

