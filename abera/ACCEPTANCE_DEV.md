# Aceptación operativa corta de SigNoz DEV

Cuenta `374786578563`, región `us-east-2`, producto `abera-signoz`, oferta Lite
`signoz-lite-31d` a COP 89.900/31 días. Esta aceptación usa un comprador ficticio
y ePayco en modo de pruebas. No establece una aceptación de PROD ni de carga
sostenida de 72 horas. El checker prolongado `readiness.py` conserva su finalidad.

## Preparación

Guardar por GET el catálogo técnico completo, el catálogo público de Billing,
las releases y las instalaciones actuales. La última lectura registrada del
default DEV es 0.2.0; la release 0.2.2 publicada tiene otros digests. Comprobar
de nuevo el default y las imágenes antes de usar el puente: no sustituir esos
digests por el número de versión más reciente del manifiesto.

Publicar las fuentes revisadas y construir las imágenes antes de crear clientes.
La release 0.2.4 actualiza solo el controlador y conserva app, collector,
ClickHouse y Keeper de la release fuente revisada. La 0.3.0 actualiza app y
collector sobre el mismo almacenamiento. Las dos usan el publicador existente,
manifiesto/driver v4, revisiones nativas y `--no-default`.

## Compra ficticia y reanudación

Payments contiene `services/billing/scripts/signoz_dev_checkout.py`. El helper
comprueba la cuenta, el endpoint DEV y `EPAYCO_TEST_MODE=true` de los alias live
de API, procesador y worker antes de preparar la compra. Usa la conexión existente
de Billing; no imprime ni guarda sus API keys o Bearer tokens. Los nombres,
documento, dirección, teléfono y correo del comprador son ficticios.

Desde el checkout revisado de Payments:

```powershell
python services/billing/scripts/signoz_dev_checkout.py --state-file services/billing/dist/signoz-dev/checkout.json
python services/billing/scripts/signoz_dev_checkout.py --state-file services/billing/dist/signoz-dev/checkout.json --observe
```

El archivo privado conserva la identidad de la ejecución, claves de idempotencia,
órdenes y sesiones de checkout. Reutilizarlo tras una interrupción. `--observe`
solo consulta. La tarjeta publicada por el [SDK oficial de ePayco](https://github.com/epayco/epayco-python)
para pruebas es `4575623182290326`, vencimiento `12/2027`, CVV `123`, con
`test=true`. La confirmación debe provenir de ePayco y de su conciliación en
Billing; no enviar un webhook inventado ni fabricar períodos pagados.

## Escenarios y cierre

1. Con una suscripción, verificar pago y provisión, reclamar las credenciales por
   Billing, entrar a SigNoz e ingerir logs, métricas y trazas. Guardar el vínculo
   entre orden, período pagado, suscripción, host y operación.
2. Mediante las operaciones existentes de Automations, verificar BACKUP y
   RESTORE versionados S3/KMS, suspensión, apagado/encendido y recuperación.
   Conservar contraseña, token, namespace, contadores y límites pagados.
3. Revisar/aplicar UPDATE al puente 0.2.4 y después a 0.3.0 con mantenimiento
   del host. Verificar las migraciones 133–135 y un fallo inducido previo a abrir
   ingestión: la compensación restaura datos e imágenes fuente. Un fallo después
   de abrir ingestión conserva los datos nuevos y exige recuperación revisada.
4. Solo después del recorrido principal, usar `--index 2`, `3` y `4` para ocupar
   brevemente el primer host. Ejecutar 15 minutos de carga y comprobar aislamiento.
   `--index 5` prueba exclusivamente la creación de otro host; retirar ese cliente
   y su host en cuanto quede comprobado. El helper rechaza un sexto cliente.
5. Ejecutar el cierre también si falla una etapa: ARCHIVE/DELETE de los IDs de
   esta ejecución, retirada del último host y EBS, ausencia de contenedores,
   rutas y reservas, y comparación de los catálogos vecinos. SUSPEND conserva
   datos y recursos; cancelar renovación en Billing tampoco acredita retirada.

Antes de apagar un producto completo, comprobar que sus instalaciones pertenecen
a esta ejecución. No incluir vecinos ni recursos históricos. Guardar recibos y
comprobar ausencia física en AWS, además de los estados del core/CloudFormation.
Los backups sujetos a las 24 horas de retención DEV se inventarían y purgan por
el reconciliador existente, sin mantener hosts encendidos ni acortar su retención.

El informe final debe registrar fuente, digests, versiones S3, operaciones,
escenarios ejecutados, latencias observadas, recursos retirados y retenciones.
Mantener separados tests locales, simulaciones y evidencia nativa AWS. Promover
el default DEV únicamente con el informe aceptado por el core y el publicador.
