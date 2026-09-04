#!/usr/bin/env python3
"""
Importa propiedades desde EasyBroker hacia Odoo (dirección inversa a
sync_to_easybroker.py) — para el caso de propiedades que ya están
publicadas en EasyBroker/Inmuebles24 pero que todavía no existen como
producto en la tienda de Odoo (trovainmobiliaria.com).

Por defecto trae solo los public_id de EasyBroker listados en la variable
de entorno EASYBROKER_PROPERTY_IDS (separados por coma) — pensado para el
backfill puntual de las propiedades detectadas como faltantes. Si esa
variable no se define, intenta traer TODAS las propiedades con
status=published de la cuenta (ver fetch_all_published_ids, marcado como
TODO porque el filtro exacto de la API no se confirmó contra la
documentación real — pruébalo primero con un puñado antes de dejarlo
correr sobre toda la cuenta).

Es idempotente: antes de crear un producto busca en Odoo un producto cuya
Referencia interna (default_code) sea igual al public_id de EasyBroker
(ej. "EB-WW0370"); si ya existe, lo actualiza en vez de duplicarlo.

ANTES DE USARLO EN SERIO:
  - Los nombres de campo técnicos de la pestaña "Información inmobiliaria"
    de Odoo (Recámaras, Baños, Tipo de operación, Colonia, etc.) están
    marcados como TODO más abajo con un valor de ejemplo — Odoo instaló un
    módulo de bienes raíces que les puso nombres internos propios que no
    se adivinan por la etiqueta que ves en pantalla. Corre primero
    scripts/list_odoo_product_fields.py para sacar los nombres reales y
    reemplaza los TODO.
  - Las imágenes se descargan desde las URLs que da EasyBroker y se suben
    como adjuntos del producto (product_template_image_ids, el campo
    estándar de galería de imágenes del eCommerce de Odoo). Si tu módulo
    de bienes raíces tiene su propio campo de fotos en vez de usar la
    galería estándar, ajusta upload_images() más abajo.
"""

import base64
import logging
import os
import sys
import xmlrpc.client

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("import")

# --------------------------------------------------------------------------
# CONFIGURACIÓN
# --------------------------------------------------------------------------

ODOO_MODEL = "product.template"

# Campo estándar de Odoo donde se guarda el código EasyBroker (ej. "EB-WW0370").
# Es el mismo campo que ya se usa hoy en los 7 productos existentes
# ("Referencia" en la pantalla de Información general).
FIELD_REFERENCE = "default_code"

FIELD_NAME = "name"                 # Título del producto
FIELD_PRICE = "list_price"          # Precio de venta
FIELD_PUBLISHED = "is_published"    # Switch "Publicado" en la pestaña Comercio electrónico
FIELD_SALE_OK = "sale_ok"           # Casilla "Ventas"

# TODO: reemplazar estos nombres con los reales que regrese
# list_odoo_product_fields.py — estos son marcadores de ejemplo, casi
# seguro NO son los nombres técnicos correctos.
FIELD_PROPERTY_KEY = "x_clave_de_propiedad"
FIELD_PROPERTY_TYPE = "x_tipo_de_inmueble"
FIELD_OPERATION_TYPE = "x_tipo_de_operacion"
FIELD_STATE = "x_estado"
FIELD_MUNICIPALITY = "x_municipio_o_alcaldia"
FIELD_NEIGHBORHOOD = "x_colonia"
FIELD_ZIP = "x_codigo_postal"
FIELD_EXACT_ADDRESS = "x_direccion_exacta"
FIELD_LAT = "x_latitud"
FIELD_LNG = "x_longitud"
FIELD_LOT_SIZE = "x_superficie_de_terreno"
FIELD_CONSTRUCTION_SIZE = "x_superficie_de_construccion"
FIELD_BEDROOMS = "x_recamaras"
FIELD_BATHROOMS_FULL = "x_banos_completos"
FIELD_BATHROOMS_HALF = "x_medios_banos"
FIELD_PARKING = "x_estacionamientos"
FIELD_LONG_DESCRIPTION = "x_descripcion_completa"

# Campo estándar de Odoo/eCommerce para la galería de fotos del producto.
FIELD_IMAGE_IDS = "product_template_image_ids"

EASYBROKER_BASE_URL = "https://api.easybroker.com/v1"


def env(name, required=True, default=None):
    value = os.environ.get(name, default)
    if required and not value:
        log.error("Falta la variable de entorno %s.", name)
        sys.exit(1)
    return value


def connect_odoo():
    url = env("ODOO_URL").rstrip("/")
    db = env("ODOO_DB")
    username = env("ODOO_USERNAME")
    api_key = env("ODOO_API_KEY")

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, username, api_key, {})
    if not uid:
        log.error("No se pudo autenticar contra Odoo. Revisa ODOO_DB / ODOO_USERNAME / ODOO_API_KEY.")
        sys.exit(1)

    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
    return db, uid, api_key, models


def easybroker_headers():
    return {
        "X-Authorization": env("EASYBROKER_API_KEY"),
        "accept": "application/json",
    }


def fetch_easybroker_property(public_id):
    response = requests.get(
        f"{EASYBROKER_BASE_URL}/properties/{public_id}",
        headers=easybroker_headers(),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def fetch_all_published_ids():
    # TODO: el filtro exacto (nombre de parámetro para status=published,
    # paginación) no se confirmó contra la documentación real de
    # EasyBroker — revisa dev.easybroker.com/docs/propiedades antes de
    # confiar en esto para una cuenta con muchas propiedades. Se deja como
    # punto de partida razonable.
    ids = []
    page = 1
    while True:
        response = requests.get(
            f"{EASYBROKER_BASE_URL}/properties",
            headers=easybroker_headers(),
            params={"page": page, "limit": 20, "search[statuses][]": "published"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        content = data.get("content", [])
        if not content:
            break
        ids.extend(item["public_id"] for item in content)
        pagination = data.get("pagination", {})
        if page >= pagination.get("total_pages", page):
            break
        page += 1
    return ids


def find_existing_product(db, uid, api_key, models, public_id):
    ids = models.execute_kw(
        db, uid, api_key,
        ODOO_MODEL, "search",
        [[(FIELD_REFERENCE, "=", public_id)]],
    )
    return ids[0] if ids else None


def build_odoo_values(prop):
    location = prop.get("location") or {}
    operations = prop.get("operations") or []
    price = operations[0].get("amount") if operations else 0

    values = {
        FIELD_NAME: prop.get("title") or prop.get("public_id"),
        FIELD_REFERENCE: prop.get("public_id"),
        FIELD_PRICE: price or 0,
        FIELD_PUBLISHED: True,
        FIELD_SALE_OK: True,
        FIELD_PROPERTY_KEY: prop.get("public_id"),
        FIELD_PROPERTY_TYPE: (prop.get("property_type") or {}).get("name")
        if isinstance(prop.get("property_type"), dict) else prop.get("property_type"),
        FIELD_STATE: location.get("state") or location.get("province"),
        FIELD_MUNICIPALITY: location.get("municipality") or location.get("city"),
        FIELD_NEIGHBORHOOD: location.get("neighborhood") or location.get("colony"),
        FIELD_ZIP: location.get("postal_code"),
        FIELD_EXACT_ADDRESS: location.get("street") or location.get("name"),
        FIELD_LONG_DESCRIPTION: prop.get("description") or "",
    }

    if location.get("latitude") and location.get("longitude"):
        values[FIELD_LAT] = location["latitude"]
        values[FIELD_LNG] = location["longitude"]

    if prop.get("construction_size"):
        values[FIELD_CONSTRUCTION_SIZE] = prop["construction_size"]
    if prop.get("lot_size"):
        values[FIELD_LOT_SIZE] = prop["lot_size"]
    if prop.get("bedrooms"):
        values[FIELD_BEDROOMS] = prop["bedrooms"]
    if prop.get("bathrooms"):
        values[FIELD_BATHROOMS_FULL] = prop["bathrooms"]
    if prop.get("half_bathrooms"):
        values[FIELD_BATHROOMS_HALF] = prop["half_bathrooms"]
    if prop.get("parking_spaces"):
        values[FIELD_PARKING] = prop["parking_spaces"]

    # Quita claves con valor None para no pisar campos con vacíos por error.
    return {k: v for k, v in values.items() if v is not None}


def download_image_b64(url):
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    return base64.b64encode(response.content).decode("ascii")


def upload_images(db, uid, api_key, models, product_tmpl_id, prop):
    images = prop.get("property_images") or []
    if not images:
        log.info("  Sin fotos que subir (EasyBroker no devolvió property_images).")
        return

    for index, image in enumerate(images):
        url = image.get("url")
        if not url:
            continue
        try:
            data_b64 = download_image_b64(url)
        except requests.RequestException as exc:
            log.warning("  No se pudo descargar la imagen %s: %s", url, exc)
            continue

        models.execute_kw(
            db, uid, api_key,
            "product.image", "create",
            [{
                "name": f"{prop.get('public_id')}-{index}",
                "image_1920": data_b64,
                "product_tmpl_id": product_tmpl_id,
            }],
        )
    log.info("  %s foto(s) subida(s).", len(images))


def main():
    db, uid, api_key, models = connect_odoo()

    explicit_ids = env("EASYBROKER_PROPERTY_IDS", required=False)
    if explicit_ids:
        public_ids = [p.strip() for p in explicit_ids.split(",") if p.strip()]
        log.info("Usando la lista explícita de %s propiedad(es): %s", len(public_ids), ", ".join(public_ids))
    else:
        log.info("EASYBROKER_PROPERTY_IDS no está definida; buscando todas las publicadas en EasyBroker...")
        public_ids = fetch_all_published_ids()
        log.info("Encontradas %s propiedad(es) publicadas en EasyBroker.", len(public_ids))

    created, updated, errors = 0, 0, 0

    for public_id in public_ids:
        log.info("Procesando %s...", public_id)
        try:
            prop = fetch_easybroker_property(public_id)
        except requests.RequestException as exc:
            log.error("  No se pudo obtener %s de EasyBroker: %s", public_id, exc)
            errors += 1
            continue

        values = build_odoo_values(prop)
        existing_id = find_existing_product(db, uid, api_key, models, public_id)

        try:
            if existing_id:
                models.execute_kw(db, uid, api_key, ODOO_MODEL, "write", [[existing_id], values])
                log.info("  Actualizado producto existente (id %s) en Odoo.", existing_id)
                updated += 1
                product_tmpl_id = existing_id
            else:
                product_tmpl_id = models.execute_kw(db, uid, api_key, ODOO_MODEL, "create", [values])
                log.info("  Creado producto nuevo (id %s) en Odoo.", product_tmpl_id)
                created += 1

            upload_images(db, uid, api_key, models, product_tmpl_id, prop)
        except xmlrpc.client.Fault as exc:
            log.error("  Error al escribir en Odoo para %s: %s", public_id, exc)
            errors += 1

    log.info(
        "Listo. %s creada(s), %s actualizada(s), %s con error.",
        created, updated, errors,
    )
    if errors and not (created or updated):
        sys.exit(1)


if __name__ == "__main__":
    main()
