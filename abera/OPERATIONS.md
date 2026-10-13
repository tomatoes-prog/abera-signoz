# Operación de DEV y paso a producción

## Estado inicial seguro

**0.2.0 cambia el ciclo de capacidad.** Seguir [ON_DEMAND_HOSTS.md](ON_DEMAND_HOSTS.md): mantener `EnablePilot=false`, publicar la release revisada y crear los hosts desde las suscripciones de Billing. Los pasos de activación de host único y rechazo del quinto cliente que se conservan más abajo documentan la entrega de 0.1.0; no deben ejecutarse para 0.2.0. El disco legado recuperado permanece protegido. La aceptación de AWS del recorrido nuevo está pendiente.

Las plantillas solo admiten `Environment=dev`. `EnablePilot=false` evita crear la máquina; `EnableAdmissions=false` cierra nuevas reservas. El manifiesto técnico tiene `provisioningEnabled: false`. Producto, planes y ofertas de Billing están retirados y no permiten checkout. Pro no tiene oferta ni configuración técnica desplegable.

La cuenta DEV comprobada es `374786578563`, región `us-east-2`. El cambio de permisos de `abera-dev-security-seed` terminó en `UPDATE_COMPLETE` el 4 de octubre de 2026; la plantilla aplicada conserva la delegación de Chatwoot. La foundation, las imágenes ECR, el host y la actualización de core/Billing siguen pendientes. El catálogo remoto todavía no se modificó.

Los workflows `goci` y `jsci` ejecutan directamente las pruebas y compilaciones de Community con Go 1.25.7, Node 22 y pnpm 10.11.0. Incluyen formato, tipos, lint, pruebas y la comprobación de que el backend Community no importa Enterprise. Los workflows heredados requerían una aplicación privada y acceso a `SigNoz/primus`; esa dependencia se reemplazó por las herramientas públicas del repositorio. La validación de Abera también comprueba el parche de namespaces y sus pruebas de operación.

## Publicación desde los repositorios

1. Revisar los cambios de los tres repositorios y ejecutar sus pruebas. Publicar primero la extensión compatible del control plane y Billing. La política del producto anterior no cambia cuando no declara `usagePolicy`/`capacityPolicy`.
2. Entregar en Automations el seed consolidado que coincide con el cambio ya aplicado en AWS. Permite crear exclusivamente el rol del operador SigNoz con su nuevo boundary. El boundary se define dentro de la foundation del producto. El renderer y los tests verifican límites de tamaño de IAM/CloudFormation y preservan los demás permisos. No repetir el UPDATE para este mismo cambio.
3. Publicar una revisión de SigNoz y fijar `$signozCommit` a su SHA de 40 caracteres. El publicador remoto de Automations obtiene ese checkout limpio, prepara los contextos ARM64 y registra el SHA y el hash del contenido en `image-build/source.json`.

Desde Automations, una vez integrada su rama de entrega en `develop`:

```powershell
gh workflow run publish.yml --repo tomatoes-prog/abera-automation --ref develop -f product_id=abera-signoz -f version=0.1.0 -f environment=dev -f mode=publish -f set_default=false -f signoz_source_ref=$signozCommit
```

4. La alternativa local exige las credenciales del publicador DEV y los outputs reales del bootstrap. Desde Automations, preparar primero los contextos con un checkout limpio en `$signozCheckout`, validar y publicar:

```powershell
python -m abera_cli.publish.signoz --automations . --checkout $signozCheckout --source-ref $signozCommit --environment dev
python -m abera_cli product validate abera-signoz
cfn-lint --non-zero-exit-code error products/abera-signoz/foundation.yaml products/abera-signoz/subscription.yaml
python -m abera_cli --env dev --credential-chain product publish abera-signoz --via local --no-default --artifact-bucket $artifactBucket --cfn-role-arn $productExecutionRoleArn --boundary-arn $productRuntimeBoundaryArn
```

Los comandos de publicación escriben en AWS y requieren revisar los change sets antes de ejecutarlos. El workflow agrega la obtención de la revisión exacta, emulación ARM64 y una tarea de build simultánea para limitar memoria. Los cuatro contextos son generados e ignorados por Git. El helper modifica solamente el manifiesto de la copia utilizada para publicar; no se debe entregar ese cambio generado como código fuente. La guía DEV de Automations contiene los outputs comprobados y el estado de las validaciones.

5. Construir y verificar los cinco artefactos ARM64. La foundation conserva sus repositorios ECR, tabla de jobs y backups. Actualizar con un change set revisado `EnablePilot=true`; conservar las cinco URI con digest y `EnableAdmissions=false`. La creación de la máquina se habilita con imágenes listas. Verificar SSM, mount del EBS, Docker, lease y heartbeat del pool.
6. En DEV, mantener el catálogo comercial cerrado mientras se crean cuatro suscripciones sintéticas controladas para la integración. Habilitar temporalmente solo el catálogo técnico y las admisiones de prueba. Ejercitar reserva, pago de prueba, provisión, reclamación de credenciales, renovación anticipada, cambio de plan, suspensión, archivo, recuperación, reintento y quinto cliente rechazado. Los períodos deben venir de Billing. No fabricar períodos pagados en la plataforma externa.

`EnableAdmissions` actualiza un parámetro SSM; el controlador lo lee en cada heartbeat. Publicar nuevas imágenes actualiza otro parámetro SSM sin reemplazar el servidor. Las aplicaciones cambian mediante operaciones UPDATE por cliente. El controlador conserva la versión con la que arrancó hasta una actualización explícita de mantenimiento.

## Aceptación antes de vender

Copiar `acceptance.example.json` a un archivo `*.local.json` y adjuntar evidencia real de cada prueba. El checker no modifica AWS ni abre ventas:

```powershell
python abera/tools/benchmark.py --seconds 259200
python abera/tools/readiness.py --acceptance abera/acceptance.local.json --benchmark abera/results/benchmark.json
```

El benchmark debe ejecutarse en el servidor ARM de DEV, con cuatro clientes sintéticos y el observador activo. Fuera del laboratorio local exige `ENVIRONMENT=dev`. Es una carga reproducible de 50 logs, 10 spans, 40 puntos de métricas y una consulta por cliente cada cinco segundos. Debe complementarse con cardinalidad máxima, ráfagas, consultas costosas, disco cerca del límite y backups simultáneos a la actividad de los otros clientes. Una prueba de tres minutos en x86_64 no puede aprobar este checker.

Requisitos: 72 horas, ingestión p95 ≤1 s, consulta p95 ≤2,5 s, sin OOM/reinicios, colas drenadas; restauración versionada real en S3/KMS; reinicio del host; recuperación completa ≤4 horas; integración con Billing; revisión de aislamiento, vulnerabilidades, licencias y clasificación fiscal. El presupuesto debe incluir costos adicionales medidos y sostener 15 % de margen con la mezcla de planes prevista. Estos son criterios de aceptación, no SLA comercial ya demostrado.

Tras aprobar DEV, habilitar Lite/Esencial en Billing con impuestos revisados y ofertas de 31 días, publicar el contrato técnico habilitado y abrir admisiones. Conservar la trazabilidad de la evidencia y el precio aprobado. Un tope AWS Budgets avisa pero no constituye un corte automático del gasto.

## Backups y restauración

Las operaciones normales se solicitan mediante el CLI/API existente de Automations:

```powershell
abera --env dev sub backup <subscription-id> --wait
abera --env dev sub restore <subscription-id> --backup-key <manifest-key> --wait
abera --env dev sub suspend <subscription-id> --wait
```

RESTORE exige la clave y versión del último backup verificado registrado para ese cliente. La versión del producto del backup debe coincidir con la release solicitada: una restauración entre versiones requiere un procedimiento de migración revisado. Reactivación/recuperación por pago usa Billing y su revisión monotónica; no usar una reactivación manual para inventar cupo. ARCHIVE mantiene el backup final durante 30 días; no borrar foundation, imágenes o KMS mientras existan archivos recuperables.

Una interrupción durante backup puede dejar al cliente detenido. Leer el job, el estado privado y el manifiesto; reintentar la misma operación conserva identidad y versiones. Una migración o restauración fallida queda suspendida y requiere inspección. No liberar su cupo manualmente antes de eliminar los recursos. Los lotes en cuarentena necesitan diagnosticar el rechazo del exportador; no eliminarlos para aparentar una cola sana.

## Fallo del servidor

El EBS de datos y S3 usan retención. Docker no arranca si `/srv/abera` no está montado. Un alarm de EC2 intenta recuperar fallos del sistema con el mismo almacenamiento; no cubre fallo de zona ni sustituye la prueba de recuperación.

Con EBS íntegro: cerrar admisiones, detener el controlador anterior, comprobar que ya no mantiene el lease, adjuntar el volumen correcto en la misma zona, montar sin formatear y verificar estado/volúmenes. Arrancar **un solo** controlador y actualizar las rutas al nuevo instance ID mediante operaciones revisadas. Conservar namespaces, credenciales y contadores. No ejecutar `init-dev` sobre el estado administrado.

Sin EBS íntegro: restaurar los artefactos versionados desde S3, validar hashes e identidad y reconstruir las asignaciones bajo exclusión operativa. Un snapshot periódico puede perder datos posteriores; su cupo activo queda bloqueado conservadoramente. Conciliar con Billing antes de reabrir ingestión. El procedimiento de pérdida total de host requiere un simulacro y todavía no es una recuperación automática validada.

El controlador necesita permanecer activo para purgar backups operativos vencidos. Si se pierde su estado local, reconstruir el inventario desde los manifiestos S3 y el control plane antes de borrar versiones. No aplicar una expiración indiscriminada a `recovery/`.

## Producción y crecimiento

PROD queda expresamente deshabilitado. Para habilitarlo hace falta diseñar y medir redundancia, copias independientes, migración de host, capacidad de shards, upgrades del controlador/ClickHouse, recuperación de zona y alarmas operativas. Debe elegirse entre aceptar una ventana de recuperación de un host o pagar redundancia; el piloto no permite prometer ambas al costo actual.

El siguiente escalón es otro host con cuatro cupos y un asignador que seleccione host/slot. No agregar un quinto cliente al servidor actual ni activar autoscaling sin extender el contrato de capacidad, el modelo de costos y las pruebas de aislamiento.
