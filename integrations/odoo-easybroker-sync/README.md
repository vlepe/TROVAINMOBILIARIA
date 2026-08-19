# Sincronización Odoo → EasyBroker → Portales inmobiliarios

Este paquete es el middleware que le faltaba a la integración: recoge las viviendas marcadas como "listas para publicar" en Odoo y las envía a EasyBroker, que a su vez las publica en Inmuebles24 y los demás portales que tengas conectados en tu cuenta de EasyBroker.

Corre como un workflow programado de **GitHub Actions** (gratis en el plan estándar de GitHub para este volumen), cada 10 minutos. No requiere servidor propio ni que Odoo tenga módulos personalizados instalados — por eso funciona igual con Odoo Online (SaaS).

Ver el documento de diseño completo ("Diseno_Odoo_EasyBroker_Portales.docx") para el contexto completo de la arquitectura, el modelo de datos sugerido y las alternativas que se descartaron.

## Antes de activarlo: 3 cosas pendientes

1. **Construir el modelo "Vivienda" en Odoo Studio** (sección 4 del documento de diseño) si aún no existe.
2. **Ajustar los nombres de campo** en `scripts/sync_to_easybroker.py` (sección `CONFIGURACIÓN` al inicio del archivo) para que coincidan con los nombres técnicos reales que Studio le puso a tus campos. Se ven en modo desarrollador (`?debug=1` en la URL) o en Ajustes > Técnico > Modelos.
3. **Confirmar los catálogos reales de EasyBroker** para `property_type` y el tipo de operación. Corre:

   ```bash
   EASYBROKER_API_KEY=tu_api_key python scripts/list_easybroker_property_types.py
   ```

   y actualiza `PROPERTY_TYPE_MAP` / `OPERATION_TYPE_MAP` en `sync_to_easybroker.py` con los valores exactos que regrese EasyBroker.

## Configuración en GitHub

1. Este contenido ya vive en el repositorio `vlepe/TROVAINMOBILIARIA`, en la carpeta `integrations/odoo-easybroker-sync/`. El workflow de GitHub Actions vive en `.github/workflows/sync-viviendas.yml` (tiene que estar en la raíz del repo para que GitHub lo detecte).
2. En el repo, ve a **Settings → Secrets and variables → Actions** y crea estas variables y secretos.

    **Variables** (no son sensibles, se guardan en texto plano):

    | Variable | Valor |
    |---|---|
    | `ODOO_URL` | URL base de tu instancia, **sin** `/odoo` al final: `https://trovainmobiliaria.odoo.com` (el XML-RPC vive en la raíz del dominio, no bajo `/odoo`, que es solo la ruta del cliente web) |
    | `ODOO_DB` | Nombre de la base de datos de Odoo (normalmente el mismo subdominio, ej. `trovainmobiliaria`; confírmalo en Ajustes > Técnico > Base de datos, o pregúntale a tu partner de Odoo si no estás seguro) |
    | `ODOO_USERNAME` | Usuario (correo) que hará las lecturas/escrituras — idealmente un usuario técnico dedicado, no una cuenta personal |

    **Secretos** (valores sensibles, GitHub los oculta):

    | Secreto | Valor |
    |---|---|
    | `API_ODOO_TROVA` | API Key del usuario de Odoo indicado arriba (Ajustes > Mi perfil > Seguridad de la cuenta > Claves de API) |
    | `API_EASY_BROKER` | El API key que ya generaste en EasyBroker |
3. Sube el repositorio a GitHub. El workflow en `.github/workflows/sync-viviendas.yml` empieza a correr solo cada 10 minutos.

## Probarlo sin esperar al cron

En la pestaña **Actions** del repositorio, entra al workflow "Sincronizar viviendas Odoo -> EasyBroker" y usa el botón **Run workflow**. Revisa los logs de esa corrida — ahí se ve cuántas viviendas encontró, cuáles se enviaron bien y cuáles fallaron (y por qué).

Antes de dejarlo en automático de verdad: prueba primero con una vivienda de ejemplo en Odoo, y considera dejar el campo `status` del payload en `not_published` (cambiar esa línea en `build_easybroker_payload`) hasta confirmar que el mapeo de campos es correcto — así la propiedad llega a EasyBroker pero no se distribuye todavía a los portales mientras se revisa.

## Seguridad

- Ningún secreto se escribe en el código ni en los logs — todos llegan por variables de entorno inyectadas por GitHub Actions.
- Si el repositorio es público, los *Secrets* de GitHub siguen sin ser visibles para nadie fuera del repo, pero de cualquier forma se recomienda un repositorio **privado** por tratarse de datos de negocio.
- El usuario de Odoo usado en `ODOO_USERNAME` debería tener permisos limitados solo al modelo "Vivienda" (crear un usuario técnico dedicado, no usar una cuenta personal de administrador).

## Ajustar la frecuencia

La línea `cron: "*/10 * * * *"` en `.github/workflows/sync-viviendas.yml` controla cada cuánto corre. GitHub Actions no garantiza el minuto exacto en horarios de mucha carga, así que 10 minutos es un margen razonable para algo que no necesita ser instantáneo.
