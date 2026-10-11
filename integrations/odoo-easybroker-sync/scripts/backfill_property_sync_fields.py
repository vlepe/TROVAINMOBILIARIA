#!/usr/bin/env python3
"""Completa metadatos de sincronización en propiedades publicadas de Odoo.

Acción intencional y acotada:
- Fija Tipo de operación = Venta.
- Fija Estatus comercial = Disponible.
- Completa únicamente Estado, Ciudad/Municipio y Colonia que estén vacíos,
  usando la ubicación estructurada de la propiedad enlazada en EasyBroker.

No cambia precios, descripciones, domicilios, fotos ni referencias.
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

FIELD_ACTIVE = "active"
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

REFERENCE_PATTERN = re.compile(r"^EB-[A-Za-z0-9]+$")
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
        FIELD_ACTIVE,
        FIELD_READY,
        FIELD_REFERENCE,
        FIELD_NAME,
        FIELD_WEB_TITLE,
        FIELD_OPERATION_TYPE,
        FIELD_COMMERCIAL_STATUS,
        FIELD_STATE,
        FIELD_MUNICIPALITY,
        FIELD_CITY,
        FIELD_NEIGHBORHOOD,
    ]
    return models.execute_kw(
        db,
        uid,
        api_key,
        ODOO_MODEL,
        "search_read",
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


def resolve_state_id(db, uid, api_key, models, state_name):
    if not state_name:
        return None

    key = str(state_name).strip().casefold()
    if key in _state_id_cache:
        return _state_id_cache[key]

    exact_domain = [
        ("name", "=ilike", state_name),
        ("country_id.code", "=", "MX"),
    ]
    ids = models.execute_kw(
        db,
        uid,
        api_key,
        "res.country.state",
        "search",
        [exact_domain],
        {"limit": 1},
    )
    if not ids:
        fuzzy_domain = [
            ("name", "ilike", state_name),
            ("country_id.code", "=", "MX"),
        ]
        ids = models.execute_kw(
            db,
            uid,
            api_key,
            "res.country.state",
            "search",
            [fuzzy_domain],
            {"limit": 1},
        )

    result = ids[0] if ids else None
    _state_id_cache[key] = result
    return result


def nonempty(value):
    if value is None or value is False:
        return None
    value = str(value).strip()
    return value or None


def location_values(prop, db, uid, api_key, models):
    location = prop.get("location") or {}

    state_name = nonempty(location.get("state") or location.get("province"))
    municipality = nonempty(location.get("municipality") or location.get("city"))
    city = nonempty(location.get("city") or location.get("municipality"))
    neighborhood = nonempty(
        location.get("neighborhood") or location.get("colony")
    )

    result = {}
    state_id = resolve_state_id(
        db, uid, api_key, models, state_name
    ) if state_name else None
    if state_id:
        result[FIELD_STATE] = state_id
    if municipality:
        result[FIELD_MUNICIPALITY] = municipality
    if city:
        result[FIELD_CITY] = city
    if neighborhood:
        result[FIELD_NEIGHBORHOOD] = neighborhood
    return result


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
        filled = ["Tipo de operación", "Estatus comercial"]

        missing_location = [
            field
            for field in (
                FIELD_STATE,
                FIELD_MUNICIPALITY,
                FIELD_CITY,
                FIELD_NEIGHBORHOOD,
            )
            if not record.get(field)
        ]

        if missing_location:
            if reference and REFERENCE_PATTERN.match(reference):
                try:
                    prop = fetch_easybroker_property(reference)
                    inferred = location_values(
                        prop, db, uid, api_key, models
                    )
                    for field in missing_location:
                        if inferred.get(field):
                            values[field] = inferred[field]
                            filled.append({
                                FIELD_STATE: "Estado",
                                FIELD_MUNICIPALITY: "Municipio",
                                FIELD_CITY: "Ciudad",
                                FIELD_NEIGHBORHOOD: "Colonia",
                            }[field])
                except requests.RequestException as exc:
                    log.error(
                        "No se pudo consultar EasyBroker para %s (%s): %s",
                        title,
                        reference,
                        exc,
                    )
                    errors += 1
            else:
                message = (
                    f"{title}: no tiene una referencia EB válida; "
                    "no fue posible completar su ubicación automáticamente."
                )
                log.warning(message)
                emit_warning(message)
                warnings += 1

        try:
            models.execute_kw(
                db,
                uid,
                api_key,
                ODOO_MODEL,
                "write",
                [[record_id], values],
            )
            updated += 1
        except xmlrpc.client.Fault as exc:
            log.error("Error al actualizar %s en Odoo: %s", title, exc)
            errors += 1
            continue

        still_missing = []
        for field in missing_location:
            if not values.get(field):
                still_missing.append({
                    FIELD_STATE: "Estado",
                    FIELD_MUNICIPALITY: "Municipio",
                    FIELD_CITY: "Ciudad",
                    FIELD_NEIGHBORHOOD: "Colonia",
                }[field])

        if still_missing:
            message = f"{title}: ubicación aún incompleta ({', '.join(still_missing)})."
            log.warning(message)
            emit_warning(message)
            warnings += 1
            summary.append(f"- ⚠️ {message}")
        else:
            summary.append(f"- ✅ {title}: {', '.join(filled)}.")

    log.info(
        "Listo. %s actualizada(s), %s advertencia(s), %s error(es).",
        updated,
        warnings,
        errors,
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
