# Publicación de BYSG

La aplicación tiene una base técnica para ejecutarse detrás de HTTPS con Docker y Caddy, pero **todavía no está autorizada para ventas reales**. BYSG permanece en prueba. No publicar con las claves de demostración ni activar cobros reales por el solo hecho de que una prueba de Mercado Pago haya sido aprobada.

Antes del primer cobro real, confirmar con el contador cómo facturar las preventas de ropa bajo el CUIT actual, cómo tratarlas en Ingresos Brutos de Chubut y si corresponde agregar una actividad. Esa consulta no impide continuar con desarrollo y ensayos.

## Antes de publicar

1. Copiar `.env.example` como `.env`.
2. Verificar que el registro de `bysg.com.ar` esté vigente a nombre del titular adulto. Cuando se haya elegido el alojamiento y configurado el DNS, definir `BYSG_DOMAIN=bysg.com.ar` y `BYSG_BASE_URL=https://bysg.com.ar`.
3. Reemplazar `BYSG_SESSION_SECRET`, `BYSG_ADMIN_PASSWORD` y `BYSG_BAUTI_CODE`.
4. Completar `BYSG_LEGAL_NAME`, `BYSG_LEGAL_CUIT`, `BYSG_CONTACT_EMAIL` y `BYSG_BUSINESS_ADDRESS` con los datos reales de Emilio, como adulto titular.
5. Cargar las credenciales de la aplicación de Mercado Pago de la misma persona adulta titular y configurar `MP_SELLER_ID` con el ID numérico de esa cuenta.
6. Configurar el webhook en `https://bysg.com.ar/api/webhooks/mercadopago` una vez que el servidor público con HTTPS esté funcionando.
7. Configurar SMTP para que salgan los correos guardados en la bandeja.
8. Aprobar los precios desde Administración.
9. Completar los enlaces públicos de contacto y redes.
10. Confirmar tarifas y configuración de entrega a domicilio, Mercado Envíos, Correo Argentino y courier nacional.
11. Elegir courier y tarifa para el envío internacional a Key West.
12. Mantener `BYSG_PRODUCTION=false` y usar sólo `mock` o `mercado_pago_prueba` durante el trabajo local. Sólo en el despliegue aprobado, luego de completar y verificar los puntos anteriores, configurar `BYSG_PRODUCTION=true` junto con `BYSG_PAYMENT_MODE=mercado_pago`. No exponer al público una tienda en modo simulado.

Los precios de muestra se bloquean para un cobro real. Las entregas sin tarifa se bloquean en todos los modos, incluso en los ensayos. Retiro coordinado tiene costo de envío cero; las demás modalidades requieren un importe configurado si se ofrecen al comprar. Los modos simulado y de prueba siguen disponibles para ensayar el recorrido con retiro coordinado o entregas de tarifa definida.

También falta una verificación integral antes de publicar: credencial y vendedor productivos correctos, aviso firmado de Mercado Pago y conciliación automática de un pago, salida efectiva de correos, restauración de una copia de seguridad y manejo de pedidos abandonados para que no inmovilicen cupos. La lista visual del panel comprueba presencia de configuración, no sustituye estas pruebas.

## Inicio con contenedores

```bash
docker compose up -d --build
```

Caddy obtiene y renueva el certificado HTTPS cuando el dominio apunta al servidor y los puertos 80 y 443 están disponibles.

## Persistencia y copias

- `data/` conserva la base SQLite.
- `outputs/` conserva la planilla financiera.
- `backups/` conserva copias consistentes de ambos archivos.
- El servidor crea una copia diaria y el panel permite generar otra antes de cambios importantes.

Antes de actualizar el servidor, conviene copiar `data/`, `outputs/` y `backups/` a otro dispositivo o almacenamiento privado.
