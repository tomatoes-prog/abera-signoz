# Costos y viabilidad

La instancia propuesta es `r7g.medium` en Ohio: un vCPU ARM y 8 GiB. Se fijan 192 GiB de datos, 8 GiB raíz y una IPv4. Se reutilizan VPC, TLS y ALB de Abera. No se agrega NAT ni un balanceador por cliente.

La tarifa de EC2 consultada es USD 0,0536/h, registrada con su código en [ec2-price.json](evidence/ec2-price.json), desde el [tarifario público de AWS para Ohio/Linux](https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/ec2/USD/current/ec2-ondemand-without-sec-sel/US%20East%20(Ohio)/Linux/index.json). Se usa USD 0,08/GiB-mes para [gp3](https://aws.amazon.com/ebs/volume-types/) y USD 0,005/h para [IPv4](https://aws.amazon.com/vpc/pricing/). Reconfirmar tarifas regionales antes de desplegar.

Con 744 horas y **COP 4.000/USD como supuesto de cambio**, la base de EC2 + EBS + IPv4 es **COP 238.394 por 31 días**. No es el costo total ni una factura garantizada.

```powershell
python abera/tools/economics.py --plans lite lite lite lite --additional-cost-cop 60000 --cop-per-usd 4000
python abera/tools/economics.py --plans lite lite essential essential --additional-cost-cop 60000 --cop-per-usd 4000
```

El parámetro adicional debe cubrir S3 —incluidos clientes archivados—, ECR, KMS, DynamoDB, Lambda, secretos, logs, transferencia, parte del ALB/control plane, pasarela, impuestos aplicables, conversión y soporte. **COP 60.000 es un escenario**, todavía no una medición de esos rubros.

| Ocupación | Ingreso antes de impuestos / 31d | Base + COP 60.000 supuestos | Margen del escenario |
| --- | ---: | ---: | ---: |
| 1 Lite | 89.900 | 298.394 | Negativo |
| 4 Lite | 359.600 | 298.394 | 17,0 % |
| 2 Lite + 2 Esencial | 439.600 | 298.394 | 32,1 % |
| 4 Esencial | 519.600 | 298.394 | 42,6 % |

**COP 450.000 es un techo, no un objetivo rentable para cuatro Lite.** Para conservar al menos 15 % de margen con cuatro Lite, todos los rubros adicionales deben sumar como máximo COP 67.266 bajo este cambio. La ocupación inicial requiere financiar capacidad vacía. Impuestos y descuentos modifican el ingreso neto y deben incluirse en la revisión comercial.

El tamaño de disco deja espacio temporal para verificar backups de un cliente a la vez. Reducirlo sin medir el máximo de retención, merges y snapshots puede convertir el ahorro en interrupciones. El costo final y la capacidad se aprueban conjuntamente mediante [OPERATIONS.md](OPERATIONS.md).
