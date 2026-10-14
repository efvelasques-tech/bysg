# Sistema de preventa BYSG

La carpeta contiene una aplicación de prueba completa y funciona sin cuentas externas. Utiliza Python y SQLite, incluidos en macOS.

## Iniciar

Desde la carpeta `muestra-web`:

```bash
python3 server.py
```

Abrir:

- Tienda: `http://127.0.0.1:4180/muestra-web/index.html`
- Administración: `http://127.0.0.1:4180/admin.html`
- Consulta de Bauti: `http://127.0.0.1:4180/bauti.html`

Credenciales locales predeterminadas:

- Administración: `bysg-admin-demo`
- Bauti: `bauti`

Estas claves solo son apropiadas para una computadora local. Antes de publicar se deben reemplazar mediante `.env`.

## Flujo disponible

1. El cliente agrega diseños al carrito.
2. En el checkout completa los datos del adulto responsable y la entrega.
3. El servidor crea un pedido, verifica el cupo y lo reserva en una transacción.
4. El simulador permite aprobar, dejar pendiente o rechazar el pago.
5. Un pago aprobado pasa el cupo de reservado a vendido.
6. Un pago rechazado libera el cupo.
7. El administrador puede avanzar el pedido y cargar transportista, código y enlace de seguimiento.
8. Bauti puede consultar ventas, cupos y su cuenta corriente sin modificar datos.
9. El panel permite editar costos, precio, margen y cupo por categoría; el cambio se refleja en los diez diseños y en la planilla financiera.
10. Los mensajes de contacto, suscriptores, solicitudes de arrepentimiento, correos y copias de seguridad se administran desde el mismo panel.

## Mercado Pago

El modo predeterminado es `mock`. La integración preparada usa Checkout Pro mediante Orders API, crea una orden con clave de idempotencia y recibe Webhooks firmados.

Para conectarla a Mercado Pago:

1. Copiar `.env.example` como `.env`.
2. Cambiar `BYSG_PAYMENT_MODE=mercado_pago`.
3. Completar `MP_ACCESS_TOKEN`, `MP_SELLER_ID` y `MP_WEBHOOK_SECRET` con las credenciales y el ID del vendedor titular de la aplicación BYSG.
4. Publicar el servidor en HTTPS.
5. Configurar el webhook de órdenes en `https://DOMINIO/api/webhooks/mercadopago`.
6. Probar primero con usuarios de prueba de Mercado Pago.

Nunca se deben copiar contraseñas personales ni publicar el archivo `.env`.

### Ensayo aislado y receptor de avisos

El ensayo no usa la base de la tienda, no envía correos y conserva las claves
solamente en memoria. Para preparar un receptor separado, **sin publicar nada**:

```bash
python3 sandbox_server.py --seller-id 3738474084 --buyer-id 3737970722 --port 4181 --webhook-port 4182
```

- La tienda y la pantalla privada siguen en `127.0.0.1:4181`.
- `127.0.0.1:4182` acepta únicamente `POST /api/webhooks/mercadopago`; no sirve
  archivos, tienda, administración ni formularios de credenciales.
- La clave de Webhooks se carga en el paso 3 de `/local/pagos`. No es el Access
  Token. Los dos secretos se borran al desconectar o detener la sesión.
- No hay una dirección pública ni un túnel iniciado por este comando. Se
  requiere autorización explícita para conectar un HTTPS externo **solo al
  puerto 4182**, nunca al puerto privado 4181.
- La URL completa se configura en Webhooks de la aplicación correspondiente
  al vendedor de prueba. La cuenta y la order deben seguir siendo de ensayo,
  aunque el panel de Mercado Pago denomine productiva a alguna pestaña.
- Si ya existe un ensayo activo, `--resume-test-db /RUTA/TEMPORAL/pruebas.sqlite3`
  permite copiar su base a otra sesión sin detener ni modificar la original.
  Solo admite bases temporales de este programa con pedidos del comprador y
  proveedor de prueba esperados. La nueva sesión requiere cargar nuevamente las
  claves en su pantalla privada; no las copia de la sesión anterior.
- `--webhook-url https://NOMBRE.trycloudflare.com/api/webhooks/mercadopago`
  informa al programa de un enlace temporal **ya autorizado y abierto por
  separado**. No inicia el túnel por sí mismo ni verifica la recepción. Al cerrar
  la conexión o apagar la Mac, el enlace deja de servir: no es alojamiento
  estable ni debe usarse para ventas reales.

El receptor verifica la firma, consulta la order directamente a Mercado Pago y
comprueba vendedor de prueba, ID, referencia del pedido, importe completo y
moneda. Un regreso del navegador no marca el pedido como pagado. Los avisos
repetidos no duplican stock, ventas ni correos; un error transitorio conserva el
aviso pendiente para su reintento. No se guardan datos de tarjeta ni tokens del
proveedor en el registro del pago.

**Estado de implementación (5 de octubre de 2026):** el pedido de ensayo
BYSG-202610-00002 fue confirmado como aprobado por la API de Mercado Pago y
conciliado en la base temporal; el cupo pasó de reservado a vendido. No hubo
aviso automático en esa sesión, porque se había iniciado sin receptor. El botón
«Comprobar pedido pendiente (sin cobrar)» sigue siendo de solo lectura. La nueva
opción «Conciliar pago aprobado (sin nuevo cobro)» reconsulta la order, verifica
vendedor, referencia, moneda e importe completo y solo entonces actualiza la
copia temporal. No permite marcar un pago por el regreso del navegador ni por
un dato escrito manualmente. Para comprobar los avisos automáticos con pagos
futuros todavía hay que conectar una URL HTTPS al receptor separado y configurar
el evento `order` en Mercado Pago. Ninguna de estas pruebas habilita ventas reales.
Una sesión iniciada antes de un cambio de código no recibe el cambio hasta
reiniciarla; antes de detenerla, prever la recarga del token en memoria y la
conservación de los pedidos de ensayo necesarios.

Pruebas locales:

```bash
python3 -m unittest discover -s tests -v
```

La configuración oficial se describe en
[notificaciones de Checkout Pro Orders](https://www.mercadopago.com.ar/developers/es/docs/checkout-pro-orders/notifications).

## Finanzas y precios

La sección **Administración → Finanzas y precios** usa el costo completo de cada categoría: producto terminado, bolsa, llavero y otros costos. Calcula automáticamente el margen bruto en pesos y en porcentaje.

Al guardar una categoría:

- se actualizan todos sus diseños;
- se actualiza la tienda;
- se crea una copia previa;
- se sincroniza `Finanzas-BYSG.xlsx`.

## Base de datos

La base se crea automáticamente en `data/bysg.sqlite3`. Contiene productos, variantes, SKUs, pedidos, pagos, eventos, movimientos de stock, webhooks y cuenta corriente.

Los precios iniciales son ficticios y están marcados como valores de muestra:

- Remeras: ARS 25.000
- Buzos: ARS 48.000
- Gorras: ARS 20.000

## Envíos

El pedido admite retiro coordinado, entrega a domicilio, Mercado Envíos, Correo Argentino, courier nacional y envío internacional a Key West. Las entregas sin tarifa definida figuran como pendientes y no pueden elegirse ni reservar cupo, tanto en ensayos como en cobros reales. Retiro coordinado queda disponible sin cargo. Además, el servidor rechaza precios de muestra antes de cualquier cobro real. Los campos `tracking_provider`, `tracking_code` y `tracking_url` permiten registrar el operador real, su código y el enlace de seguimiento.

## Seguridad incluida

- Consultas parametrizadas a SQLite.
- Transacciones para reservar y reponer stock.
- Cookies de sesión firmadas, `HttpOnly` y `SameSite=Strict`.
- Control de origen en operaciones que modifican datos.
- Límite básico de intentos de acceso y checkout.
- Límite de tamaño para cuerpos JSON.
- Validación HMAC de Webhooks de Mercado Pago.
- Idempotencia de notificaciones.
- Encabezados de seguridad en todas las respuestas.
- Lista explícita de páginas y recursos públicos: no se pueden descargar por URL las copias, la base, las planillas ni otros archivos internos.
- El catálogo público no incluye costos ni márgenes internos.

## Correos y copias de seguridad

Los correos se guardan sin perderse aunque todavía no exista una cuenta SMTP. Al configurar SMTP, el panel puede procesar la bandeja y enviarlos. El servidor crea una copia diaria de la base y la planilla, y permite generar copias manuales antes de cambios importantes.

La publicación en Internet requiere HTTPS, secretos aleatorios, credenciales reales, los datos legales y de contacto de Emilio como adulto titular, y la revisión legal de los borradores de políticas. El panel muestra esta lista con indicadores rojos y verdes; los datos exactos se cargan mediante `.env` y no deben reemplazarse por información ficticia.
