# Arquitectura y contratos

## Flujo comercial

```mermaid
sequenceDiagram
    participant VPS as Plataforma en VPS
    participant B as Abera Payments / Billing
    participant C as Automations
    participant D as Driver SigNoz
    participant H as Controlador del servidor
    VPS->>B: Crear orden, plan y AdminEmail
    B->>C: Validar contrato y reservar hostname/cupo
    Note over C: Reserva atómica; máximo cuatro cupos; servidor saludable
    B->>B: Confirmar pago y registrar período
    B->>C: Crear suscripción con revisión y períodos pagados
    C->>D: prepare/create (protocolo v4)
    D->>H: Job durable en DynamoDB
    H->>H: Crear bases, migrar, crear cuentas y verificar login
    H->>C: ARN de credenciales + puerto asignado
    C->>C: Crear ruta ALB, verificar y activar
    VPS->>B: Reclamar credenciales mediante flujo existente
```

`usagePolicy` y `capacityPolicy` son extensiones opcionales del contrato. Los productos que no las declaran conservan su contrato anterior. La reserva del cupo ocurre en la misma transacción que la reserva del hostname. CREATE exige esa reserva. ARCHIVE/DELETE liberan el cupo tras eliminar los recursos del cliente; una limpieza fallida conserva la asignación. RECOVER vuelve a reclamar un cupo mediante una transacción y un registro de propietario idempotente.

El driver Lambda solo encola y consulta trabajos. El controlador ejecuta uno por vez, mantiene un lease y comprueba el propietario y el plazo de la operación. Las renovaciones de una suscripción activa actualizan el contexto de Billing sin desplegar infraestructura; el controlador lee ese contexto y reconcilia los límites. DynamoDB conserva las revisiones para rechazar eventos viejos.

## Aislamiento

- Una aplicación Community y SQLite por cliente; no se depende de la multitenencia Enterprise.
- Siete bases posibles con prefijo aleatorio derivado de suscripción y generación. El código resuelve nombres conocidos al iniciar; no reescribe SQL introducido por el usuario.
- Usuarios separados para lectura, exportación y consultas distribuidas. Los engines Distributed guardan el clúster del cliente, nunca credenciales de migración.
- El administrador de ClickHouse solo se usa desde el operador y el contenedor temporal de migración. Aplicaciones y colectores carecen de DDL y de permisos sobre las bases de otros clientes.
- Redes privadas por cliente. ClickHouse y Keeper no publican puertos. AWS permite llegar a los cuatro gateways solo desde el ALB compartido. IMDSv2 tiene hop limit 1; el controlador usa la red del host.
- SQL libre, registro público inicial y cambios de TTL se rechazan en el gateway. Se conserva el constructor de consultas de SigNoz y el cliente recibe una cuenta administradora no raíz.

Las pruebas comprueban SELECT propio y cruzado, tokens ajenos, DDL, lectura de usuarios, Keeper y archivos. El proceso ClickHouse y el sistema operativo siguen siendo componentes compartidos: todavía se necesita una prueba adversarial de consumo de recursos antes de aceptar clientes.

## Entrega y consumo

El gateway valida la identidad y el período pagado, decodifica OTLP y registra consumo, recibo y carga útil en **una transacción SQLite durable** antes de responder. Los límites de bytes, puntos, series, velocidad y cola se verifican de forma atómica. Reintentos idénticos dentro del mismo período se deduplican durante 24 horas. El cambio de plan conserva los contadores.

La cola tiene 32 MiB y el archivo SQLite un máximo de 256 MiB. El colector escribe sin cola ni batching volátiles. Un ACK del exportador sigue a la escritura en ClickHouse. Un fallo 5xx provoca reintento; un rechazo permanente o éxito parcial queda en cuarentena y aparece en el panel para revisión. **La entrega al almacén puede duplicarse si se pierde la respuesta después de insertar**; el consumo del gateway no se vuelve a cobrar. No se afirma entrega exactamente una vez.

El disco se observa cada 25 segundos en AWS; una lectura vieja o menos de 15 %/5 GiB libres detiene ingestión. El límite por cliente mide las partes activas de ClickHouse; también existe reserva global para SQLite, imágenes, merges y backups. El tope es un control preventivo con observación periódica, no una partición de disco físicamente reservada.

## Expiración y recuperación

| Evento | Comportamiento |
| --- | --- |
| Expira el período pagado | Se detiene inmediatamente la ingestión; las consultas conservan cuatro días de gracia. |
| Suspensión | Se detienen los contenedores del cliente. Se conservan sus bases y volúmenes. |
| Día siete sin pago | Automations inicia ARCHIVE; solo elimina recursos después de registrar el backup final verificado. |
| Pago durante la ventana de recuperación | RECOVER reclama un cupo, restaura su identidad y aplica la revisión comercial vigente. |
| Fin de los 30 días desde archivo | PURGE elimina las versiones exactas indicadas por el recibo, mediante el lifecycle existente. |

Los backups detienen brevemente **al cliente respaldado**. Incluyen ClickHouse nativo, aplicación, ledger y secretos de aplicación. Antes de publicarlos se restauran temporalmente las tablas, se verifican integridad y cantidades, y se elimina la copia de verificación. Los tres artefactos se suben con KMS y se vuelven a leer para verificar SHA-256. Los recibos fijan versiones de S3; un reintento reutiliza los mismos objetos.

La restauración normal conserva los contadores, series y lotes pendientes más recientes. Una recuperación de un archivo final sellado conserva exactamente el cupo registrado. Si se pierde el servidor y solo existe un backup periódico, se bloquea conservadoramente el cupo del período activo hasta conciliación; podría existir consumo aceptado después del backup. La retención usa la fecha original de la telemetría y se vuelve a materializar al restaurar.

Se conservan dos backups periódicos por cliente; los backups operativos adicionales expiran a los 30 días. Los archivos finales tienen el plazo administrado por Automations. Hay que mantener disponibles controlador, driver, KMS e imágenes durante ese plazo. La copia previa a una restauración queda local para permitir investigación y requiere limpieza controlada del operador.

## Actualizaciones y crecimiento

App y colector tienen digests por cliente. UPDATE toma backup, despliega y migra ese cliente durante `verify`, comprueba salud y luego permite activar la ruta. Los digests de ClickHouse/Keeper compartidos no pueden cambiar mediante UPDATE de una suscripción. Un fallo de recuperación o migración deja el cliente detenido para inspección.

Esta versión no crea nuevos hosts ni redistribuye shards. Al cuarto cliente se cierran las reservas; el quinto no cobra mediante un checkout válido. Para crecer se necesita ampliar el asignador a varios hosts, probar migración entre ellos y presupuestar redundancia. La actualización del controlador o del motor compartido requiere una ventana de mantenimiento con los cuatro clientes considerados.
