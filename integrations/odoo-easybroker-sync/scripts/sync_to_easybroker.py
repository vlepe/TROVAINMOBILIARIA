#!/usr/bin/env python3
"""
Sincroniza viviendas de Odoo hacia EasyBroker (que a su vez las publica en
Inmuebles24, Vivanuncios, Lamudi, Propiedades.com, etc. si tienes esos
portales conectados en tu cuenta de EasyBroker).

Cómo funciona:
  1. Este script corre en GitHub Actions (ver .github/workflows/sync-viviendas.yml).
  2. Se conecta a Odoo por su API externa (XML-RPC) y busca productos
     (modelo product.template, el mismo que ya usa la tienda web) marcados
     como "Publicado" (is_published = True).
  3. Traduce cada uno al formato que espera la API de EasyBroker.
  4. Si el producto no tiene todavía una Referencia (default_code) con
     formato EasyBroker (EB-...), lo CREA en EasyBroker (POST /properties)
     y guarda el public_id que regresa de vuelta en default_code — así la
     siguiente corrida ya sabe que existe y lo actualiza en vez de
     duplicarlo. Si ya tiene una Referencia EB-..., lo ACTUALIZA
     (PUT /properties/{public_id}).

Nombres de campo (lado Odoo): confirmados el 19/ago/2026 y el 04/sep/2026
corriendo scripts/list_odoo_product_fields.py --all contra la Odoo real de
Trova (ver integrations/odoo-easybroker-sync/README.md). El modelo real NO
es "x_vivienda" (ese nunca se construyó) — son campos x_studio_* agregados
directo sobre product.template, el mismo modelo de producto de la tienda.

Nombres de campo (lado EasyBroker, para ESCRITURA): confirmados el
04/sep/2026 contra dev.easybroker.com/reference/post_properties, después de
que la primera corrida real (#231, 04/sep/2026) fallara con
"HTTP 422 Unpermitted parameters" en todas las propiedades que sí tenían
tipo de inmueble y tipo de operación mapeados. Dos cosas estaban mal en la
versión anterior de este script (ya corregidas):
  - "location" en escritura NO acepta state/province/municipality/city/
    neighborhood/colony (esas llaves son del lado de LECTURA, con otro
    formato). Solo acepta: name, street, exterior_number, interior_number,
    cross_street, postal_code, latitude, longitude. "name" debe ser el
    string jerárquico "Colonia, Ciudad, Estado" (mismo formato que el
    "full_name" del endpoint /locations).
  - La lista de fotos se manda en la llave "images", no "property_images"
    (esa es la llave del lado de lectura).
  - Cada operación en "operations" necesita también "active": true
    (booleano requerido por el esquema de EasyBroker).

Diseño para no publicar por accidente / no filtrar información sensible:
  - Solo se consideran productos con is_published = True (el switch
    "Publicado" que el equipo ya usa para la tienda web).
  - El campo "status" que se manda a EasyBroker sale de
    x_studio_estatus_comercial: "Disponible" -> "published"; cualquier otro
    valor (Apartada/Vendida/Rentada/No publicada) o vacío -> "not_published".
    Así, para probar una propiedad sin que salga en los portales todavía,
    basta con dejarla en un estatus comercial distinto de "Disponible".
  - x_studio_direccion_exacta (la calle y número exactos) solo se manda si
    x_studio_mostrar_direccion_exacta_en_web está activado. Si no, solo se
    manda la ubicación pública/aproximada (colonia, municipio, ciudad,
    estado, x_studio_ubicacion_publica).
  - Los campos de negocio interno (comisión, situación jurídica, datos del
    propietario, restricciones de visita, asesor responsable, etc.) NO se
    mandan a EasyBroker a propósito — son para uso interno del equipo, no
    para portales públicos.

Lo que este script SÍ manda a EasyBroker (ver build_easybroker_payload):
  título, descripción, tipo de inmueble, tipo de operación, precio,
  estatus (published/not_published), ubicación completa (estado, municipio,
  ciudad, colonia, código postal, ubicación pública, lat/long, y dirección
  exacta solo si se autorizó mostrarla), superficie de terreno y de
  construcción, recámaras, baños completos, medios baños, estacionamientos,
  y fotos.

Lo que NO manda (y por qué):
  - x_studio_numero_de_niveles, x_studio_piso, x_studio_antiguedad_anos,
    x_studio_superficie_privativa_m2, x_studio_orientacion: existen en Odoo
    pero no se confirmó todavía el nombre exacto que usa la API de
    EasyBroker para ellos (no aparecen en los datos que trajimos al
    importar propiedades reales). Agregarlos es un TODO seguro para
    después, una vez confirmados contra dev.easybroker.com.
  - x_studio_situacion_juridica, x_studio_comision, x_studio_descuento_estimado,
    x_studio_valor_estimado_de_mercado, x_studio_documentacion_disponible,
    x_studio_restricciones_de_visita, x_studio_situacion_de_ocupacion,
    x_studio_inmueble_ocupado, x_studio_asesor_responsable,
    x_studio_propietario_o_proveedor, x_studio_formas_de_pago_aceptadas,
    x_studio_servicios_disponibles: son información de negocio/interna, no
    debe salir a los portales públicos.
  - x_studio_tour_virtual, x_studio_video_de_la_propiedad,
    x_studio_texto_para_whatsapp, x_studio_etiqueta_principal,
    x_studio_url_de_google_maps: no se confirmó que EasyBroker tenga un
    campo equivalente en su API de creación/actualización de propiedades.

Fotos: se arma una URL pública por cada imagen de la galería del producto
(product_template_image_ids) usando el visor de imágenes estándar de Odoo
(/web/image/product.image/<id>/image_1920) y se manda esa lista de URLs en
property_images, igual que como EasyBroker las entrega al leer una
propiedad (confirmado en import_from_easybroker.py). ESTO NO SE HA
VERIFICADO EN VIVO todavía -- revisa la primera propiedad de prueba en el
panel de EasyBroker para confirmar que las fotos sí aparecen.
"""

import base64
import logging
import os
import re
import sys
import time
import xmlrpc.client

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("sync")

# --------------------------------------------------------------------------
# CONFIGURACIÓN
# --------------------------------------------------------------------------

ODOO_MODEL = "product.template"

# Campo estándar de Odoo/eCommerce: solo se sincronizan productos publicados.
FIELD_READY = "is_published"

# Referencia interna: si ya tiene forma "EB-XXXXXX" quiere decir que el
# producto ya existe en EasyBroker (o se importó de ahí, o ya se sincronizó
# antes) y hay que actualizar en vez de crear.
FIELD_REFERENCE = "default_code"

FIELD_NAME = "name"
FIELD_WEB_TITLE = "x_studio_titulo_comercial_web"  # si existe, se prefiere sobre "name"
FIELD_PRICE = "list_price"
FIELD_LONG_DESCRIPTION = "x_studio_descripcion_completa"
FIELD_SHORT_DESCRIPTION = "x_studio_descripcion_corta"

FIELD_PROPERTY_TYPE = "x_studio_tipo_de_inmueble"       # selection
FIELD_OPERATION_TYPE = "x_studio_tipo_de_operacion"      # selection
FIELD_COMMERCIAL_STATUS = "x_studio_estatus_comercial"   # selection

FIELD_STATE = "x_studio_estado"                          # many2one -> res.country.state
FIELD_MUNICIPALITY = "x_studio_municipio_o_alcaldia"
FIELD_CITY = "x_studio_ciudad"
FIELD_NEIGHBORHOOD = "x_studio_colonia"
FIELD_ZIP = "x_studio_codigo_postal"
FIELD_PUBLIC_LOCATION = "x_studio_ubicacion_publica"
FIELD_EXACT_ADDRESS = "x_studio_direccion_exacta"
FIELD_SHOW_EXACT_ADDRESS = "x_studio_mostrar_direccion_exacta_en_web"
FIELD_LAT = "x_studio_latitud"
FIELD_LNG = "x_studio_longitud"

FIELD_LOT_SIZE = "x_studio_superficie_de_terreno_m2"
FIELD_CONSTRUCTION_SIZE = "x_studio_superficie_de_construccion_m2"
FIELD_BEDROOMS = "x_studio_recamaras"
FIELD_BATHROOMS_FULL = "x_studio_banos_completos"
FIELD_BATHROOMS_HALF = "x_studio_medios_banos"
FIELD_PARKING = "x_studio_estacionamientos"

FIELD_IMAGE_IDS = "product_template_image_ids"

EASYBROKER_BASE_URL = "https://api.easybroker.com/v1"
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5

DEFAULT_CURRENCY = "MXN"

REFERENCE_PATTERN = re.compile(r"^EB-[A-Za-z0-9]+$")

# Confirmado el 04/sep/2026 corriendo scripts/list_easybroker_property_types.py
# contra la cuenta real de EasyBroker de Trova (GET /property_types).
# x_studio_tipo_de_inmueble en Odoo solo tiene estas 8 opciones fijas; las
# que no tienen un símbolo claro de EasyBroker (Desarrollo, Otro) se dejan
# sin mapear a propósito -- ver map_property_type().
PROPERTY_TYPE_MAP = {
    "Casa": "house",
    "Departamento": "apartment",
    "Terreno": "lot",
    "Local comercial": "retail_space",
    "Bodega": "industrial_warehouse",
    "Oficina": "office",
    # "Desarrollo" y "Otro" no tienen un símbolo confiable en el catálogo de
    # EasyBroker -- se dejan sin mapear (ver map_property_type).
}

# Confirmado con la propia documentación de EasyBroker y con los datos
# reales leídos en import_from_easybroker.py.
OPERATION_TYPE_MAP = {
    "Venta": "sale",
    "Renta": "rental",
}


def map_property_type(odoo_value):
    """Traduce x_studio_tipo_de_inmueble (Odoo) al symbol real de EasyBroker.

    Regresa None si no hay un mapeo confiable -- en ese caso NO se sincroniza
    esa propiedad (mejor eso que mandar un tipo inventado a un portal
    público). Ver el log de la corrida para ver cuáles se saltaron.
    """
    return PROPERTY_TYPE_MAP.get(odoo_value)


def map_operation_type(odoo_value):
    return OPERATION_TYPE_MAP.get(odoo_value)


def map_status(commercial_status):
    """"Disponible" es la única situación comercial que debe salir como
    publicada en los portales. Cualquier otra cosa (Apartada, Vendida,
    Rentada, No publicada, o vacío) se manda como "not_published" -- así
    una propiedad vendida no se sigue anunciando como disponible.
    """
    if commercial_status == "Disponible":
        return "published"
    return "not_published"


def env(name, required=True, default=None):
    value = os.environ.get(name, default)
    if required and not value:
        log.error("Falta la variable de entorno %s (revisa los GitHub Secrets/Variables).", name)
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
    return url, db, uid, api_key, models


def strip_html(value):
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_ready_products(db, uid, api_key, models):
    fields = [
        FIELD_REFERENCE, FIELD_NAME, FIELD_WEB_TITLE, FIELD_PRICE,
        FIELD_LONG_DESCRIPTION, FIELD_SHORT_DESCRIPTION,
        FIELD_PROPERTY_TYPE, FIELD_OPERATION_TYPE, FIELD_COMMERCIAL_STATUS,
        FIELD_STATE, FIELD_MUNICIPALITY, FIELD_CITY, FIELD_NEIGHBORHOOD,
        FIELD_ZIP, FIELD_PUBLIC_LOCATION, FIELD_EXACT_ADDRESS,
        FIELD_SHOW_EXACT_ADDRESS, FIELD_LAT, FIELD_LNG,
        FIELD_LOT_SIZE, FIELD_CONSTRUCTION_SIZE, FIELD_BEDROOMS,
        FIELD_BATHROOMS_FULL, FIELD_BATHROOMS_HALF, FIELD_PARKING,
        FIELD_IMAGE_IDS,
    ]
    domain = [(FIELD_READY, "=", True)]
    return models.execute_kw(
        db, uid, api_key,
        ODOO_MODEL, "search_read",
        [domain, ["id"] + fields],
    )


def many2one_name(value):
    """Los campos many2one regresan [id, "Nombre a mostrar"] por XML-RPC, o
    False si están vacíos."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return value[1]
    return None


def build_image_urls(odoo_url, image_ids):
    # NOTA: no verificado en vivo todavía -- confirma con la primera
    # propiedad de prueba que EasyBroker sí puede descargar estas URLs.
    return [f"{odoo_url}/web/image/product.image/{image_id}/image_1920" for image_id in image_ids]


def build_easybroker_payload(record, odoo_url):
    property_type = map_property_type(record.get(FIELD_PROPERTY_TYPE))
    operation_type = map_operation_type(record.get(FIELD_OPERATION_TYPE))

    title = record.get(FIELD_WEB_TITLE) or record.get(FIELD_NAME) or record.get(FIELD_REFERENCE) or ""
    description = strip_html(record.get(FIELD_LONG_DESCRIPTION)) or strip_html(record.get(FIELD_SHORT_DESCRIPTION))

    state_name = many2one_name(record.get(FIELD_STATE))
    municipality = record.get(FIELD_MUNICIPALITY) or ""
    city = record.get(FIELD_CITY) or municipality
    neighborhood = record.get(FIELD_NEIGHBORHOOD) or ""

    # Confirmado el 04/sep/2026 contra dev.easybroker.com/reference/post_properties:
    # "location" en escritura (POST/PUT) SOLO acepta estas llaves --
    # state/province/municipality/city/neighborhood/colony NO existen ahí
    # (esas son del lado de lectura, con otro formato) y mandarlas causaba
    # "Unpermitted parameters" (HTTP 422) en todas las propiedades.
    # "name" debe ser el string jerárquico "Colonia, Ciudad, Estado" --
    # el mismo formato que regresa el endpoint /locations en su campo
    # "full_name" (ver dev.easybroker.com/reference/get_locations) -- por
    # eso se construye a partir de colonia/ciudad/estado en vez de usar
    # x_studio_ubicacion_publica directo (ese texto libre puede no calzar
    # exactamente con ese formato).
    location_name = ", ".join(filter(None, [neighborhood, city, state_name])) or (
        record.get(FIELD_PUBLIC_LOCATION) or ""
    )
    location = {
        "name": location_name,
        "postal_code": record.get(FIELD_ZIP) or "",
    }
    if record.get(FIELD_SHOW_EXACT_ADDRESS) and record.get(FIELD_EXACT_ADDRESS):
        location["street"] = record[FIELD_EXACT_ADDRESS]
    if record.get(FIELD_LAT) and record.get(FIELD_LNG):
        location["latitude"] = record[FIELD_LAT]
        location["longitude"] = record[FIELD_LNG]

    payload = {
        "title": title,
        "description": description[:4000],
        "property_type": property_type,
        "status": map_status(record.get(FIELD_COMMERCIAL_STATUS)),
        "operations": [
            {
                "type": operation_type,
                "active": True,
                "amount": record.get(FIELD_PRICE) or 0,
                "currency": DEFAULT_CURRENCY,
            }
        ],
        "location": location,
    }

    if record.get(FIELD_LOT_SIZE):
        payload["lot_size"] = record[FIELD_LOT_SIZE]
    if record.get(FIELD_CONSTRUCTION_SIZE):
        payload["construction_size"] = record[FIELD_CONSTRUCTION_SIZE]
    if record.get(FIELD_BEDROOMS):
        payload["bedrooms"] = record[FIELD_BEDROOMS]
    if record.get(FIELD_BATHROOMS_FULL):
        payload["bathrooms"] = record[FIELD_BATHROOMS_FULL]
    if record.get(FIELD_BATHROOMS_HALF):
        payload["half_bathrooms"] = record[FIELD_BATHROOMS_HALF]
    if record.get(FIELD_PARKING):
        payload["parking_spaces"] = record[FIELD_PARKING]

    image_ids = record.get(FIELD_IMAGE_IDS) or []
    if image_ids:
        urls = build_image_urls(odoo_url, image_ids)
        # Confirmado el 04/sep/2026: la llave real en POST/PUT es "images"
        # (no "property_images" -- ese es el nombre del lado de lectura).
        payload["images"] = [{"url": u} for u in urls]

    return payload


def easybroker_headers():
    return {
        "X-Authorization": env("EASYBROKER_API_KEY"),
        "Content-Type": "application/json",
        "accept": "application/json",
    }


def send_to_easybroker(payload, existing_public_id):
    if existing_public_id:
        method = "PUT"
        url = f"{EASYBROKER_BASE_URL}/properties/{existing_public_id}"
    else:
        method = "POST"
        url = f"{EASYBROKER_BASE_URL}/properties"

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.request(method, url, json=payload, headers=easybroker_headers(), timeout=20)
        except requests.RequestException as exc:
            last_error = str(exc)
            log.warning("Intento %s/%s: error de red (%s).", attempt, MAX_RETRIES, exc)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        if response.status_code in (200, 201):
            try:
                return True, response.json(), None
            except ValueError:
                return True, {}, None

        if 500 <= response.status_code < 600:
            last_error = f"HTTP {response.status_code}: {response.text[:500]}"
            log.warning("Intento %s/%s: %s", attempt, MAX_RETRIES, last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        # Error 4xx: no tiene sentido reintentar solo; se reporta de una vez.
        return False, None, f"HTTP {response.status_code}: {response.text[:500]}"

    return False, None, last_error or "Se agotaron los reintentos."


def update_odoo_reference(db, uid, api_key, models, record_id, public_id):
    models.execute_kw(db, uid, api_key, ODOO_MODEL, "write", [[record_id], {FIELD_REFERENCE: public_id}])


def main():
    log.info("Conectando a Odoo...")
    odoo_url, db, uid, api_key, models = connect_odoo()

    log.info("Buscando productos publicados en Odoo...")
    records = fetch_ready_products(db, uid, api_key, models)
    log.info("Encontrados %s producto(s) publicado(s).", len(records))

    created, updated, skipped, errors = 0, 0, 0, 0
    summary_lines = []

    for record in records:
        record_id = record["id"]
        reference = (record.get(FIELD_REFERENCE) or "").strip()
        title = record.get(FIELD_WEB_TITLE) or record.get(FIELD_NAME) or f"producto {record_id}"

        # Odoo regresa `False` (no None ni "") en los campos de selección
        # que están vacíos -- se muestra como "(vacío en Odoo)" en vez de
        # "False" para que sea obvio que es una propiedad sin ese dato
        # capturado todavía, no un error del script.
        property_type_raw = record.get(FIELD_PROPERTY_TYPE)
        operation_type_raw = record.get(FIELD_OPERATION_TYPE)

        if not map_property_type(property_type_raw):
            shown = property_type_raw if property_type_raw else "(vacío en Odoo)"
            log.warning(
                "  SALTADO %s: tipo de inmueble '%s' no tiene mapeo confiable a EasyBroker.",
                title, shown,
            )
            summary_lines.append(f"- SALTADO: {title} (tipo de inmueble sin mapeo: {shown})")
            skipped += 1
            continue
        if not map_operation_type(operation_type_raw):
            shown = operation_type_raw if operation_type_raw else "(vacío en Odoo)"
            log.warning(
                "  SALTADO %s: tipo de operación '%s' no tiene mapeo confiable a EasyBroker.",
                title, shown,
            )
            summary_lines.append(f"- SALTADO: {title} (tipo de operación sin mapeo: {shown})")
            skipped += 1
            continue

        existing_public_id = reference if REFERENCE_PATTERN.match(reference) else None
        payload = build_easybroker_payload(record, odoo_url)

        log.info("Procesando: %s (%s)", title, "actualizar " + existing_public_id if existing_public_id else "crear nuevo")
        success, response_body, error = send_to_easybroker(payload, existing_public_id)

        if success:
            if existing_public_id:
                log.info("  OK: actualizado en EasyBroker (%s).", existing_public_id)
                summary_lines.append(f"- Actualizado: {title} ({existing_public_id})")
                updated += 1
            else:
                public_id = (response_body or {}).get("public_id", "")
                if public_id:
                    update_odoo_reference(db, uid, api_key, models, record_id, public_id)
                log.info("  OK: creado en EasyBroker (%s).", public_id or "sin public_id en la respuesta")
                summary_lines.append(f"- Creado: {title} ({public_id or 'sin public_id'})")
                created += 1
        else:
            log.error("  ERROR: %s -> %s", title, error)
            summary_lines.append(f"- ERROR: {title} -> {error}")
            errors += 1

    log.info(
        "Listo. %s creada(s), %s actualizada(s), %s saltada(s), %s con error.",
        created, updated, skipped, errors,
    )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## Sincronización Odoo -> EasyBroker\n\n")
            fh.write(f"{created} creada(s), {updated} actualizada(s), {skipped} saltada(s), {errors} con error.\n\n")
            fh.writelines(line + "\n" for line in summary_lines)

    if errors and not (created or updated):
        sys.exit(1)


if __name__ == "__main__":
    main()
