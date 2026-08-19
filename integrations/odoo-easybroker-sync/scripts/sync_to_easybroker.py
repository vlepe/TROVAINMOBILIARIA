#!/usr/bin/env python3
"""
Sincroniza viviendas de Odoo hacia EasyBroker (que a su vez las publica en
Inmuebles24, Vivanuncios, Lamudi, Propiedades.com, etc. si tienes esos
portales conectados en tu cuenta de EasyBroker).

Cómo funciona (arquitectura completa en el documento de diseño):
  1. Este script corre en GitHub Actions cada 10 minutos (ver
     .github/workflows/sync-viviendas.yml).
  2. Se conecta a Odoo por su API externa (XML-RPC) y busca viviendas
     marcadas como "lista para publicar" que aún no se han enviado.
  3. Traduce cada una al formato que espera la API de EasyBroker.
  4. Llama a POST /properties de EasyBroker (con el API key como header
     X-Authorization).
  5. Marca en Odoo el resultado (enviado / error) para que el equipo lo vea.

ANTES DE USARLO EN SERIO:
  - Este script asume nombres de modelo y de campos técnicos que TODAVÍA NO
    EXISTEN hasta que se construya el modelo "Vivienda" en Odoo Studio (ver
    sección 4 del documento de diseño). Ajusta las constantes de la sección
    "CONFIGURACIÓN" más abajo con los nombres técnicos reales.
  - Para ver el nombre técnico de un campo en Odoo: activa el modo
    desarrollador (Ajustes > Técnico, o agrega ?debug=1 a la URL), abre el
    campo en el formulario y usa "Ver información del campo" / "Edit Field"
    en Studio, o revisa Ajustes > Técnico > Modelos.
  - Los valores válidos de "property_type" y del tipo de operación en
    EasyBroker deben confirmarse contra su catálogo real:
      GET https://api.easybroker.com/v1/property_types
    (usa scripts/list_easybroker_property_types.py, incluido en este mismo
    paquete, para consultarlo con tu API key).
"""

import logging
import os
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
# CONFIGURACIÓN — ajustar esto una vez que exista el modelo "Vivienda" en
# Odoo Studio. Los nombres de abajo son un punto de partida razonable (así
# es como Studio suele nombrar los campos que crea), pero HAY QUE
# CONFIRMARLOS contra el modelo real antes de usar esto en producción.
# --------------------------------------------------------------------------

ODOO_MODEL = "x_vivienda"

# Campo booleano que el equipo marca en Odoo para disparar la publicación.
FIELD_READY = "x_studio_listo_para_publicar"

# Campo de selección donde este script anota el resultado del envío.
# Valores usados por el script: "enviado" / "error".
FIELD_SYNC_STATUS = "x_studio_estatus_de_sincronizacion"

# Campo de texto donde se guarda el id de EasyBroker tras un envío exitoso.
FIELD_EASYBROKER_ID = "x_studio_easybroker_id"

# Campo de texto donde se guarda el último mensaje de error, si lo hay.
FIELD_SYNC_ERROR = "x_studio_ultimo_error_sincronizacion"

# Mapeo de los campos de la vivienda en Odoo -> nombre esperado en el
# payload que arma este script (ver build_easybroker_payload más abajo).
FIELD_TITLE = "x_studio_titulo"
FIELD_DESCRIPTION = "x_studio_descripcion"
FIELD_PROPERTY_TYPE = "x_studio_tipo_de_propiedad"  # selección en Odoo
FIELD_OPERATION_TYPE = "x_studio_tipo_de_operacion"  # 'venta' / 'renta'
FIELD_PRICE = "x_studio_precio"
FIELD_CURRENCY = "x_studio_moneda"  # 'MXN' / 'USD'
FIELD_ADDRESS = "x_studio_direccion"
FIELD_STREET_NUMBER = "x_studio_numero"
FIELD_NEIGHBORHOOD = "x_studio_colonia"
FIELD_CITY = "x_studio_ciudad"
FIELD_STATE = "x_studio_estado"
FIELD_ZIP = "x_studio_codigo_postal"
FIELD_LAT = "x_studio_latitud"
FIELD_LNG = "x_studio_longitud"
FIELD_CONSTRUCTION_SIZE = "x_studio_m2_construccion"
FIELD_LOT_SIZE = "x_studio_m2_terreno"
FIELD_BEDROOMS = "x_studio_recamaras"
FIELD_BATHROOMS = "x_studio_banos"
FIELD_PARKING = "x_studio_estacionamientos"
FIELD_PHOTO_URLS = "x_studio_fotos_urls"  # texto con URLs separadas por coma

# TODO: confirmar estos símbolos contra GET /property_types de EasyBroker
# antes de usar el script. Los valores de la izquierda son la selección que
# el equipo captura en Odoo; los de la derecha deben ser EXACTAMENTE el
# "value"/symbol que EasyBroker espera.
PROPERTY_TYPE_MAP = {
    "casa": "Casa",
    "departamento": "Apartamento",
    "terreno": "Terreno",
    "oficina": "Oficina",
}

# TODO: confirmar el nombre exacto que EasyBroker espera para el tipo de
# operación dentro del arreglo "operations" (ver documentación de
# dev.easybroker.com/docs/propiedades).
OPERATION_TYPE_MAP = {
    "venta": "sale",
    "renta": "rental",
}

EASYBROKER_BASE_URL = "https://api.easybroker.com/v1"
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5


def env(name):
    value = os.environ.get(name)
    if not value:
        log.error("Falta la variable de entorno %s (revisa los GitHub Secrets).", name)
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

def fetch_pending_properties(db, uid, api_key, models):
    fields = [
        FIELD_TITLE, FIELD_DESCRIPTION, FIELD_PROPERTY_TYPE, FIELD_OPERATION_TYPE,
        FIELD_PRICE, FIELD_CURRENCY, FIELD_ADDRESS, FIELD_STREET_NUMBER,
        FIELD_NEIGHBORHOOD, FIELD_CITY, FIELD_STATE, FIELD_ZIP, FIELD_LAT, FIELD_LNG,
        FIELD_CONSTRUCTION_SIZE, FIELD_LOT_SIZE, FIELD_BEDROOMS, FIELD_BATHROOMS,
        FIELD_PARKING, FIELD_PHOTO_URLS,
    ]
    domain = [
        (FIELD_READY, "=", True),
        (FIELD_SYNC_STATUS, "!=", "enviado"),
    ]
    odoo_url = env("ODOO_URL").rstrip("/")
    return models.execute_kw(
        db, uid, api_key,
        ODOO_MODEL, "search_read",
        [domain, ["id"] + fields],
    ), odoo_url

def build_easybroker_payload(record):
    operation_key = OPERATION_TYPE_MAP.get(record.get(FIELD_OPERATION_TYPE))
    property_type = PROPERTY_TYPE_MAP.get(record.get(FIELD_PROPERTY_TYPE))

    payload = {
        "title": record.get(FIELD_TITLE) or "",
        "description": (record.get(FIELD_DESCRIPTION) or "")[:4000],
        "property_type": property_type,
        "status": "published",
        "operations": [
            {
                "type": operation_key,
                "amount": record.get(FIELD_PRICE) or 0,
                "currency": record.get(FIELD_CURRENCY) or "MXN",
            }
        ],
        "location": {
            "name": ", ".join(
                filter(None, [
                    record.get(FIELD_ADDRESS),
                    record.get(FIELD_NEIGHBORHOOD),
                    record.get(FIELD_CITY),
                    record.get(FIELD_STATE),
                ])
            ),
            "street": record.get(FIELD_ADDRESS) or "",
            "exterior_number": record.get(FIELD_STREET_NUMBER) or "",
            "postal_code": record.get(FIELD_ZIP) or "",
        },
    }

    if record.get(FIELD_LAT) and record.get(FIELD_LNG):
        payload["location"]["latitude"] = record[FIELD_LAT]
        payload["location"]["longitude"] = record[FIELD_LNG]

    if record.get(FIELD_CONSTRUCTION_SIZE):
        payload["construction_size"] = record[FIELD_CONSTRUCTION_SIZE]
    if record.get(FIELD_LOT_SIZE):
        payload["lot_size"] = record[FIELD_LOT_SIZE]
    if record.get(FIELD_BEDROOMS):
        payload["bedrooms"] = record[FIELD_BEDROOMS]
    if record.get(FIELD_BATHROOMS):
        payload["bathrooms"] = record[FIELD_BATHROOMS]
    if record.get(FIELD_PARKING):
        payload["parking_spaces"] = record[FIELD_PARKING]

    photo_urls = record.get(FIELD_PHOTO_URLS)
    if photo_urls:
        urls = [u.strip() for u in photo_urls.split(",") if u.strip()]
        if urls:
            payload["property_images"] = [{"url": u} for u in urls]

    return payload

def send_to_easybroker(payload):
    api_key = env("EASYBROKER_API_KEY")
    headers = {
        "X-Authorization": api_key,
        "Content-Type": "application/json",
        "accept": "application/json",
    }

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(
                f"{EASYBROKER_BASE_URL}/properties",
                json=payload,
                headers=headers,
                timeout=20,
            )
        except requests.RequestException as exc:
            last_error = str(exc)
            log.warning("Intento %s/%s: error de red (%s).", attempt, MAX_RETRIES, exc)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue
        if response.status_code in (200, 201):
            return True, response.json(), None

        if 500 <= response.status_code < 600:
            last_error = f"HTTP {response.status_code}: {response.text[:500]}"
            log.warning("Intento %s/%s: %s", attempt, MAX_RETRIES, last_error)
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
            continue

        # Error 4xx: no tiene sentido reintentar solo; se reporta de una vez.
        return False, None, f"HTTP {response.status_code}: {response.text[:500]}"

    return False, None, last_error or "Se agotaron los reintentos."


def update_odoo_record(db, uid, api_key, models, record_id, values):
    odoo_url = env("ODOO_URL").rstrip("/")
    models.execute_kw(db, uid, api_key, ODOO_MODEL, "write", [[record_id], values])

def main():
    log.info("Conectando a Odoo...")
    db, uid, api_key, models = connect_odoo()

    log.info("Buscando viviendas listas para publicar...")
    records, _ = fetch_pending_properties(db, uid, api_key, models)
    log.info("Encontradas %s vivienda(s) pendientes.", len(records))

    ok_count = 0
    error_count = 0

    for record in records:
        record_id = record["id"]
        title = record.get(FIELD_TITLE) or f"registro {record_id}"
        log.info("Procesando: %s", title)

        payload = build_easybroker_payload(record)
        success, response_body, error = send_to_easybroker(payload)

        if success:
            public_id = (response_body or {}).get("public_id", "")
            update_odoo_record(db, uid, api_key, models, record_id, {
                FIELD_SYNC_STATUS: "enviado",
                FIELD_EASYBROKER_ID: public_id,
                FIELD_SYNC_ERROR: False,
            })
            log.info("OK: %s -> EasyBroker %s", title, public_id)
            ok_count += 1
        else:
            update_odoo_record(db, uid, api_key, models, record_id, {
                FIELD_SYNC_STATUS: "error",
                FIELD_SYNC_ERROR: error,
            })
            log.error("ERROR: %s -> %s", title, error)
            error_count += 1

    log.info("Listo. %s enviada(s) con éxito, %s con error.", ok_count, error_count)

    if error_count and not ok_count:
        # Si TODO falló, marca el job de GitHub Actions en rojo para que se
        # note en la pestaña Actions (útil si luego se agregan notificaciones).
        sys.exit(1)


if __name__ == "__main__":
    main()
