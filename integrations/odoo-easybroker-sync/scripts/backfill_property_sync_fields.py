#!/usr/bin/env python3
"""Completa metadatos de sincronización en propiedades publicadas de Odoo.

Acción intencional y acotada:
- Fija Tipo de operación = Venta.
- Fija Estatus comercial = Disponible.
- Completa únicamente Estado, Ciudad/Municipio y Colonia que estén vacíos.
- Deduce esos campos localmente desde la ubicación enlazada en EasyBroker
  y desde el domicilio ya capturado en Odoo.

No cambia precios, descripciones, domicilios, fotos ni referencias, y no
envía domicilios a servicios externos adicionales.
"""

import logging
import os
import re
import sys
import xmlrpc.client

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backfill")

ODOO_MODEL = "product.template"
EASYBROKER_BASE_URL = "https://api.easybroker.com/v1"

FIELD_READY = "is_published"
FIELD_REFERENCE = "default_code"
FIELD_NAME = "name"
FIELD_WEB_TITLE = "x_studio_titulo_comercial_web"
FIELD_OPERATION_TYPE = "x_studio_tipo_de_operacion"
FIELD_COMMERCIAL_STATUS = "x_studio_estatus_comercial"
FIELD_STATE = "x_studio_estado"
FIELD_MUNICIPALITY = "x_studio_municipio_o_alcaldia"
FIELD_CITY = "x_studio_ciudad"
FIELD_NEIGHBORHOOD = "x_studio_colonia"
FIELD_PUBLIC_LOCATION = "x_studio_ubicacion_publica"
FIELD_EXACT_ADDRESS = "x_studio_direccion_exacta"

REFERENCE_PATTERN = re.compile(r"^EB-[A-Za-z0-9]+$")
POSTAL_PART_PATTERN = re.compile(r"^(?:C\.?\s*P\.?\s*)?\d{5}$", re.IGNORECASE)
COUNTRY_PARTS = {"mexico", "méxico", "mx"}
_state_id_cache = {}


def env(name):
    value = os.environ.get(name)
    if not value:
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
        log.error("No se pudo autenticar contra Odoo.")
        sys.exit(1)
    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
    return db, uid, api_key, models


def easybroker_headers():
    return {
        "X-Authorization": env("EASYBROKER_API_KEY"),
        "accept": "application/json",
    }


def fetch_published_properties(db, uid, api_key, models):
    fields = [
        FIELD_READY, FIELD_REFERENCE, FIELD_NAME, FIELD_WEB_TITLE,
        FIELD_OPERATION_TYPE, FIELD_COMMERCIAL_STATUS, FIELD_STATE,
        FIELD_MUNICIPALITY, FIELD_CITY, FIELD_NEIGHBORHOOD,
        FIELD_PUBLIC_LOCATION, FIELD_EXACT_ADDRESS,
    ]
    return models.execute_kw(
        db, uid, api_key, ODOO_MODEL, "search_read",
        [[(FIELD_READY, "=", True)], ["id"] + fields],
        {"context": {"active_test": False}},
    )


def fetch_easybroker_property(public_id):
    response = requests.get(
        f"{EASYBROKER_BASE_URL}/properties/{public_id}",
        headers=easybroker_headers(),
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


def nonempty(value):
    if value is None or value is False:
        return None
    value = str(value).strip()
    return value or None


def resolve_state_id(db, uid, api_key, models, state_name):
    if not state_name:
        return None
    state_name = re.sub(r"\s*\(MX\)\s*$", "", str(state_name)).strip()
    key = state_name.casefold()
    if key in _state_id_cache:
        return _state_id_cache[key]

    for operator in ("=ilike", "ilike"):
        ids = models.execute_kw(
            db, uid, api_key, "res.country.state", "search",
            [[
                ("name", operator, state_name),
                ("country_id.code", "=", "MX"),
            ]],
            {"limit": 1},
        )
        if ids:
            _state_id_cache[key] = ids[0]
            return ids[0]

    _state_id_cache[key] = None
    return None


def parse_location_text(value):
    """Interpreta localmente [..., Colonia, Ciudad/Municipio, Estado]."""
    value = nonempty(value)
    if not value:
        return {}

    parts = [
        part.strip()
        for part in re.split(r"[,;\n]+", value)
        if part.strip()
    ]
    parts = [
        part for part in parts
        if part.casefold() not in COUNTRY_PARTS
        and not POSTAL_PART_PATTERN.match(part)
    ]
    if len(parts) < 3:
        return {}

    return {
        "neighborhood": parts[-3],
        "city": parts[-2],
        "municipality": parts[-2],
        "state": parts[-1],
    }


def extract_easybroker_location(prop):
    location = prop.get("location") or {}
    if not isinstance(location, dict):
        return {}

    result = {
        "state": nonempty(location.get("state") or location.get("province")),
        "municipality": nonempty(
            location.get("municipality") or location.get("city")
        ),
        "city": nonempty(location.get("city") or location.get("municipality")),
        "neighborhood": nonempty(
            location.get("neighborhood") or location.get("colony")
        ),
    }
    parsed = parse_location_text(
        location.get("full_name") or location.get("name")
    )
    for key, value in parsed.items():
        if not result.get(key):
            result[key] = value
    return {key: value for key, value in result.items() if value}


def merge_missing(target, source):
    for key, value in source.items():
        if value and not target.get(key):
            target[key] = value


def emit_warning(message):
    escaped = str(message).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::warning::{escaped}")


def main():
    db, uid, api_key, models = connect_odoo()
    records = fetch_published_properties(db, uid, api_key, models)
    log.info("Propiedades publicadas encontradas: %s.", len(records))

    updated = 0
    warnings = 0
    errors = 0
    summary = []

    for record in records:
        record_id = record["id"]
        title = record.get(FIELD_WEB_TITLE) or record.get(FIELD_NAME) or f"producto {record_id}"
        reference = nonempty(record.get(FIELD_REFERENCE))
        values = {
            FIELD_OPERATION_TYPE: "Venta",
            FIELD_COMMERCIAL_STATUS: "Disponible",
        }

        inferred = {}
        if reference and REFERENCE_PATTERN.match(reference):
            try:
                prop = fetch_easybroker_property(reference)
                merge_missing(inferred, extract_easybroker_location(prop))
            except requests.RequestException as exc:
                log.warning(
                    "No se pudo consultar la ubicación enlazada para %s: %s",
                    title, exc,
                )

        # Si el objeto de EasyBroker no trae sus partes estructuradas,
        # interpreta localmente el domicilio/ubicación existente en Odoo.
        merge_missing(
            inferred,
            parse_location_text(record.get(FIELD_PUBLIC_LOCATION)),
        )
        merge_missing(
            inferred,
            parse_location_text(record.get(FIELD_EXACT_ADDRESS)),
        )

        current_municipality = nonempty(record.get(FIELD_MUNICIPALITY))
        current_city = nonempty(record.get(FIELD_CITY))
        if current_municipality and not current_city:
            values[FIELD_CITY] = current_municipality
        if current_city and not current_municipality:
            values[FIELD_MUNICIPALITY] = current_city

        if not record.get(FIELD_STATE) and inferred.get("state"):
            state_id = resolve_state_id(
                db, uid, api_key, models, inferred["state"]
            )
            if state_id:
                values[FIELD_STATE] = state_id

        inferred_municipality = inferred.get("municipality") or inferred.get("city")
        inferred_city = inferred.get("city") or inferred.get("municipality")
        if not (current_municipality or current_city) and inferred_municipality:
            values[FIELD_MUNICIPALITY] = inferred_municipality
        if not current_city and inferred_city:
            values[FIELD_CITY] = inferred_city
        if not record.get(FIELD_NEIGHBORHOOD) and inferred.get("neighborhood"):
            values[FIELD_NEIGHBORHOOD] = inferred["neighborhood"]

        complete_state = bool(record.get(FIELD_STATE) or values.get(FIELD_STATE))
        complete_city = bool(
            current_municipality or current_city
            or values.get(FIELD_MUNICIPALITY) or values.get(FIELD_CITY)
        )
        complete_neighborhood = bool(
            record.get(FIELD_NEIGHBORHOOD)
            or values.get(FIELD_NEIGHBORHOOD)
        )

        if not (complete_state and complete_city and complete_neighborhood):
            missing_labels = []
            if not complete_state:
                missing_labels.append("Estado")
            if not complete_city:
                missing_labels.append("Ciudad/Municipio")
            if not complete_neighborhood:
                missing_labels.append("Colonia")
            message = (
                f"{title}: no se actualizó porque el domicilio no permitió "
                f"deducir {', '.join(missing_labels)} con suficiente certeza."
            )
            log.warning(message)
            emit_warning(message)
            warnings += 1
            summary.append(f"- ⚠️ {message}")
            continue

        try:
            models.execute_kw(
                db, uid, api_key, ODOO_MODEL, "write",
                [[record_id], values],
            )
            updated += 1
            summary.append(
                f"- ✅ {title}: Venta, Disponible y ubicación completa."
            )
        except xmlrpc.client.Fault as exc:
            log.error("Error al actualizar %s en Odoo: %s", title, exc)
            errors += 1

    log.info(
        "Listo. %s actualizada(s), %s advertencia(s), %s error(es).",
        updated, warnings, errors,
    )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("## Completar metadatos de propiedades\n\n")
            fh.write(
                f"{updated} actualizada(s), {warnings} advertencia(s), "
                f"{errors} error(es).\n\n"
            )
            fh.writelines(line + "\n" for line in summary)

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
