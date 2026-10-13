# SigNoz 0.2.0: capacidad creada por la suscripción

## Recorrido

1. La plataforma en el VPS crea la orden en Billing con el plan y `AdminEmail`.
2. Billing reserva el hostname mediante Automations, confirma el pago y envía la suscripción con su revisión y períodos pagados.
3. El driver SigNoz de Automations reserva un slot de un host compatible. Sus cuatro slots incluyen clientes activos, suspendidos y operaciones de provisión.
4. Si hace falta capacidad, el driver crea `abera-dev-abera-signoz-host-<grupo>` usando el template auxiliar de la release. Otra compra simultánea reutiliza esa asignación y otro slot del mismo grupo.
5. El driver espera el EBS montado, el controlador, ClickHouse/Keeper y el heartbeat saludable. Envía el trabajo a `READY#<grupo>`.
6. El controlador verifica cliente, grupo, slot y operación. Crea las bases, aplicación y credenciales aisladas. Automations crea la ruta del cliente; Billing entrega el acceso mediante su flujo existente.

Los primeros cuatro clientes comparten un host. El quinto abre otro de hasta cuatro. Una compra posterior ocupa un slot libre compatible antes de crear más capacidad. Un reintento conserva la asignación. No hay traslado automático de clientes activos entre hosts.

## Retirada

SUSPEND conserva el slot y los datos. ARCHIVE exige el backup final verificado y el compromiso de eliminación registrado por el core. DELETE usa el recorrido de backup y eliminación vigente. Solo una confirmación del controlador permite liberar el slot.

Cuando el grupo queda vacío, Automations lo marca `RETIRING` de forma condicional. Ese estado bloquea nuevas asignaciones. El controlador comprueba que no quedan clientes, bases desconocidas, tablas en `default`, volúmenes Docker de aplicaciones ni restauraciones locales incompletas. También comprueba que el inventario de backups operativos existe fuera del host.

La prueba identifica grupo, instancia y operación de retirada. Automations conserva el ID del EBS antes de eliminar el stack. CloudFormation retira la instancia, su root efímero y el attachment; el EBS de datos conserva `Retain`. El driver elimina ese disco solo si corresponde a la prueba, tiene tags del mismo grupo/producto/ambiente y está desadjuntado. La siguiente comprobación confirma que desapareció antes de registrar `RETIRED`.

El último ARCHIVE/DELETE inicia este proceso. Un evento de mantenimiento cada cinco minutos continúa los pasos y reintentos aunque ya no haya clientes en el host. PURGE y el inventario operativo de backups se ejecutan desde Lambda con versiones exactas de S3. Los backups recuperables, imágenes, KMS y los recursos de control de la foundation se mantienen durante su ventana de retención.

Una creación con resultado AWS desconocido conserva su identidad como pendiente. Un fallo de montaje, una base desconocida o una retirada fallida conserva el stack/disco para inspección. El estado pendiente no equivale a una limpieza confirmada.

## Separación de permisos

No se agregan escrituras EC2 al seed-installer humano. Su `DescribeVolumes` sigue sirviendo para inspección. La foundation define el coordinador Lambda y un rol de ejecución de CloudFormation del producto bajo el boundary existente. El controlador conserva su boundary sin permisos EC2, IAM ni CloudFormation. Las rutas por suscripción conservan su rol más estrecho.

El rol del coordinador puede asignar su estado, gestionar únicamente stacks `abera-dev-abera-signoz-host-*` y borrar discos etiquetados de sus grupos después de la prueba. El stack auxiliar crea el host sin IAM propio. DEV continúa siendo el único ambiente permitido; este cambio no modifica el security-seed común.

## Entrega y transición

La release publicada 0.1.0 permanece inmutable. La declaración técnica 0.2.0 permite provisión y sustituye el `capacityPolicy` global del piloto. Las admisiones están cerradas por defecto; publicación, selección de versión y pruebas pagadas de DEV requieren su entrega revisada. Se conservan `usagePolicy`, precios, impuestos pendientes y contratos estándar de Billing. No se declara una migración automática de instalaciones 0.1.0: necesita una transición revisada si se encuentran clientes o datos de esa versión.

El disco legado importado en la foundation conserva su identidad y `Retain`. No tiene el tag de grupo nuevo y queda fuera de la limpieza del asignador. Inspeccionar su contenido y decidir su transferencia o eliminación exige una revisión aparte. La revisión anterior que activaba un host con `EnablePilot=true` queda sustituida por este recorrido; no debe aplicarse para 0.2.0.

Seguir [el procedimiento de entrega de Automations](https://github.com/tomatoes-prog/abera-automation/blob/develop/docs/product-delivery.md): fuente revisada por SHA, imágenes por digest, `review-foundation`, revisión concreta, `apply-reviewed-foundation` y `publish-reviewed --no-default`. Mantener `EnablePilot=false`. No crear compras ni suscripciones de aceptación sin autorización explícita. La promoción del default y la apertura comercial requieren su aceptación.

## Aceptación DEV pendiente

- Primera orden pagada por Billing crea el host, disco, slot, ruta y credenciales; una orden sin pagar no crea infraestructura.
- Cuatro clientes comparten el primer host, el quinto crea el segundo y varios intentos simultáneos conservan slots únicos.
- Una suspensión conserva su slot; la reactivación reutiliza sus datos.
- ARCHIVE con backup fallido conserva datos y asignación. El último archivo exitoso retira host y EBS; se verifica el inventario físico después de los reintentos.
- RECOVER puede crear o reutilizar otro host y restaura las credenciales, datos y términos comerciales vigentes.
- PURGE y expiración operativa funcionan con todos los hosts retirados.
- Repetir la prueba ARM de aislamiento, carga, backups y recuperación. [acceptance.example.json](acceptance.example.json) exige estas evidencias además de las pruebas anteriores.

## Costo

El escenario económico de [ECONOMICS.md](ECONOMICS.md) corresponde a un host con hasta cuatro clientes. La base de cada host nuevo se suma al gasto. Un host con solo un cliente todavía necesita financiar sus slots vacíos; este cambio elimina servidores ociosos sin suscripciones y el límite global de cuatro, pero no demuestra rentabilidad con ocupación baja.

El template usa la recuperación automática simplificada de EC2, disponible para la familia R7g, mediante `MaintenanceOptions.AutoRecovery=default`. [Documentación AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/instance-configuration-recovery.html). No sustituye una prueba de restauración ni ofrece tolerancia a una caída de zona.
