# Abera SigNoz 0.2.0 · servidores compartidos bajo demanda en DEV

Automations asigna **hasta cuatro clientes por servidor**. Cuando Billing solicita crear una suscripción pagada, se reutiliza un cupo compatible o se crea otro servidor con su disco. Al archivar o eliminar el último cliente, el controlador verifica que no quedan bases ni volúmenes de aplicaciones y Automations retira el servidor y su disco. Cada cliente conserva su aplicación Community, SQLite, colector, gateway, credenciales y bases de telemetría; ClickHouse y Keeper se comparten dentro de su grupo.

**Estado de 0.2.0:** cambios y pruebas locales; publicación y aceptación AWS pendientes. Esta versión conserva los precios aprobados y Pro aplazado. La foundation de 0.1.0 y su disco recuperado se preservan durante la transición. Un servidor permite recuperación, pero no ofrece alta disponibilidad ante una caída de la máquina o la zona.

## Planes propuestos

Los valores están en [plans.json](plans.json). Los precios son antes de impuestos; la clasificación fiscal sigue pendiente en Billing.

| Límite | Lite | Esencial |
| --- | ---: | ---: |
| Precio / 31 días | COP 89.900 | COP 129.900 |
| Logs + trazas / período pagado | 10 GB | 30 GB |
| Puntos de métricas / período pagado | 25 millones | 75 millones |
| Series activas durante la última hora | 1.000 | 3.000 |
| Ingestión sostenida | 64 KiB/s | 128 KiB/s |
| Ráfaga y tamaño máximo de petición | 4 MiB | 4 MiB |
| Telemetría almacenada | 10 GiB | 25 GiB |
| Retención | 7 días | 15 días |
| Intervalo de backup | 24 horas | 6 horas |

Los límites de volumen y capacidad son **propuestas pendientes de la prueba ARM**, no capacidad ya demostrada. GB significa 1.000 millones de bytes; GiB significa 1.073.741.824 bytes. Logs y trazas se miden como OTLP protobuf sin comprimir. Las métricas se cobran por puntos y tienen un límite adicional de series, porque muchas etiquetas distintas consumen recursos aunque haya pocos GB.

No se agrega otro calendario de cobro. El `cycleId` es el ID del período que Billing ya registró como pagado. La renovación anticipada agrega un período futuro. El cambio de plan conserva el período y lo consumido. Al alcanzar el cupo se rechaza nueva ingestión; no se generan cobros por excedentes. El panel `/abera` muestra consumo y avisos al 80 %, 95 % y 100 %.

## Ejecutar localmente

Requisitos: Docker con contenedores Linux, Compose v2 y Python 3.11 o posterior. La prueba se realizó en Docker Desktop x86_64; para AWS se construyen imágenes ARM64. Las compilaciones descargan dependencias fijadas en los manifiestos y necesitan Internet.

Desde la raíz de `abera-signoz`:

```powershell
docker build -f abera/docker/Dockerfile.community --target runtime -t abera/signoz-community:dev --build-arg VERSION=0.2.0 .
docker build -f abera/docker/Dockerfile.community --target collector -t abera/signoz-collector:dev .
docker build -f abera/docker/Dockerfile.clickhouse -t abera/signoz-clickhouse:dev .
python abera/tools/runtime.py init-dev --customers 4
python abera/tools/runtime.py up
python abera/tools/runtime.py observe --watch
```

Mantener el observador abierto en una terminal. La ingestión se cierra si la observación del disco tiene más de 90 segundos. En AWS lo hace el controlador automáticamente. `init-dev` preserva un estado existente y nunca reemplaza sus claves.

Abrir `http://localhost:24801` hasta `http://localhost:24804`. Las credenciales están en `abera/.runtime/dev/credentials/`, fuera de Git. El cliente recuperado conserva su contraseña y token originales. Los puertos privados `25801–25804` son para el operador local y no se publican en AWS.

Configurar el exportador OpenTelemetry con el endpoint del cliente, **OTLP por HTTP** y `Authorization: Bearer <otlpToken>`. Se aceptan protobuf, JSON y gzip. Esta versión no expone OTLP gRPC.

```powershell
python -m pip install -r abera/docker/controller-requirements.txt pytest
python -m pytest abera/tests -q
go test ./pkg/abera/... ./abera/tools/namespace-go
python abera/tools/smoke.py
python abera/tools/benchmark.py --seconds 180
python abera/tools/lifecycle_smoke.py
```

`lifecycle_smoke.py` archiva, elimina y recupera únicamente `development-4`: comprueba tablas, cuentas y consumo. Usa un sustituto local de S3; la prueba de S3/KMS real sigue pendiente. No ejecutar operaciones de lifecycle simultáneamente en el laboratorio. Los reportes quedan en `abera/results/`, ignorado por Git.

También existen `runtime.py backup development-1`, `runtime.py restore development-1 --snapshot <directorio>`, `suspend` y `reactivate`. Estos comandos aceptan solo clientes sintéticos locales. Las suscripciones administradas se operan por la API de Automations.

## Documentación

- [Asignación de hosts por suscripción, retirada y aceptación de 0.2.0](ON_DEMAND_HOSTS.md).
- [Arquitectura, aislamiento, consumo y lifecycle](ARCHITECTURE.md).
- [Despliegue y recuperación en DEV; requisitos para PROD](OPERATIONS.md).
- [Escenario económico reproducible](ECONOMICS.md).
- [Licencias y compilación del código correspondiente](LICENSES.md).
- [Evidencia local](evidence/README.md).

## Ubicación del código

| Área | Implementación |
| --- | --- |
| Cuotas, cola durable y proxy | `pkg/abera/gateway`, `cmd/abera-gateway` |
| Bases separadas y consultas acotadas | `pkg/abera/namespace`, `pkg/abera/querylimits` |
| Parche del colector fijado | `abera/tools/namespace-go`, `abera/collector` |
| Contenedores y operación del servidor | `abera/docker`, `abera/runtime` |
| Producto en Automations | `products/abera-signoz/` en `abera-automations` |
| Cupos y períodos pagados | `control-plane/src/abera_core/capacity.py`, `usage.py` |
| Catálogo y períodos de Billing | `services/billing/config/catalog_manifest.json`, `application/services/usage_entitlement.py` en `abera-payments` |
