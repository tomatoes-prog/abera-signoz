# Distribución Community y código correspondiente

El [LICENSE raíz](../LICENSE) aplica MIT Expat al código fuera de `ee/` y `cmd/enterprise/`. El build excluye esos directorios, compila `cmd/community` y falla si el grafo Go incorpora paquetes de Enterprise. Se conservan los avisos originales. La edición Community mantiene sus controles de características; no se desactivan comprobaciones para habilitar funciones restringidas.

El colector está fijado a `b75a6256ec153f6e095f29860f53f88c873dd268` (v0.144.6). Su LICENSE raíz es GNU AGPLv3; también contiene componentes con sus propios avisos. La imagen de aplicación incluye el código modificado completo de aplicación y colector, licencias y scripts bajo `/usr/share/licenses/signoz/abera-signoz-source.tar.gz`.

Cada gateway ofrece `/abera/licenses` y `/abera/source.tar.gz` públicamente. Un enlace visible en la pantalla de SigNoz abre el panel de Abera, desde donde se accede a esa descarga. El build no incluye `.runtime`, resultados privados, `.aws`, `.env`, Git ni credenciales.

## Reconstruir la versión descargada

Descomprimir el archivo: contiene `src/` (esta aplicación) y `collector/` (el colector parcheado). Desde `src/`, usar los comandos Docker del [README](README.md). El Dockerfile vuelve a obtener el commit exacto del colector y aplica `tools/namespace-go` y `collector/components.go.txt`; la copia incluida permite inspeccionar el resultado correspondiente. `go.mod`, `go.sum` y `frontend/pnpm-lock.yaml` fijan dependencias. Los builds conservan sus módulos en `build-modules.txt`.

Para ARM: agregar `--platform linux/arm64` a cada build y probarlo en ARM real. El frontend se compila en la arquitectura del constructor; Go compila el binario para `TARGETARCH`. ClickHouse incorpora la función `histogramQuantile` del repositorio.

Antes de distribución comercial, revisar el inventario completo de dependencias e imágenes, vulnerabilidades, avisos de terceros y uso de marcas. La exclusión de Enterprise y la oferta de fuentes son mecanismos implementados; no constituyen una certificación de cumplimiento legal.
