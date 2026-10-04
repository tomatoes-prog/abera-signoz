# Evidencia del piloto local

Validación realizada el 3 de octubre de 2026 en Docker Desktop Linux/x86_64, con cuatro clientes sintéticos. Los contenedores del producto compartieron el núcleo lógico 0. El equipo constructor tiene más CPU y RAM que el servidor propuesto; esta prueba no certifica capacidad ARM.

El [registro resumido](local-validation.json) contiene resultados y hashes de los reportes originales, sin tokens, contraseñas ni contenido de clientes. Los reportes completos permanecen en `abera/results/`, ignorados por Git.

| Validación | Resultado |
| --- | --- |
| Python SigNoz | 17 pruebas aprobadas |
| Python Automations | 377 pruebas aprobadas |
| Python Billing | 592 pruebas aprobadas |
| Go | Gateway, namespace, límites de consultas y reescritura AST aprobados |
| CloudFormation | Foundation y subscription sin errores de cfn-lint |
| Ingestión | Logs, trazas y métricas almacenados para los cuatro clientes |
| Aislamiento | 32 comprobaciones de ClickHouse; cuatro tokens de otros clientes rechazados |
| Archivo y recuperación | 55 tablas verificadas; recursos eliminados antes de recuperar; consumo y cuentas conservados |
| Carga local durante 180 s | 432 peticiones de ingestión y 144 consultas autenticadas; sin errores |
| Latencias p95 | Ingestión 62 ms; consultas 282 ms |
| Entrega | 7.200 logs, 1.440 spans, 5.760 puntos; cola drenada |
| Contenedores durante carga | Sin OOM ni reinicios |

Las pruebas de S3 usan un sustituto local que conserva versiones y permite verificar reintentos. Las tablas y volúmenes restaurados sí son ClickHouse y SQLite reales. Falta comprobar la integración AWS, IAM, S3/KMS, facturación y recuperación del servidor con las condiciones de [OPERATIONS.md](../OPERATIONS.md).

No se han ejecutado 72 horas en ARM, pruebas adversariales al límite de cardinalidad/disco, escaneo completo de dependencias ni un simulacro de pérdida de zona. Las ventas y PROD permanecen cerrados. `readiness.py` rechaza deliberadamente esta evidencia local como autorización del piloto comercial.
