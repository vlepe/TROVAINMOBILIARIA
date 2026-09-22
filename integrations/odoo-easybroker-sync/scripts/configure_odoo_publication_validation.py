#!/usr/bin/env python3
"""
Instala/actualiza en Odoo una regla de automatización que impide publicar
viviendas incompletas para la sincronización con EasyBroker.

La regla solo valida cuando is_published=True, por lo que no convierte estos
campos en obligatorios para todos los product.template ni bloquea archivos
históricos. Esto es intencional porque Trova usa product.template y no un
modelo exclusivo de vivienda.
"""

import os
import sys
import xmlrpc.client

ODOO_MODEL = "product.template"
AUTOMATION_NAME = "Trova - Validar vivienda antes de publicar en EasyBroker"
ACTION_NAME = "Trova - Campos obligatorios EasyBroker"

REQUIRED_FIELD_NAMES = [
    "is_published",
    "list_price",
    "x_studio_tipo_de_inmueble",
    "x_studio_tipo_de_operacion",
    "x_studio_estatus_comercial",
    "x_studio_estado",
    "x_studio_municipio_o_alcaldia",
    "x_studio_ciudad",
    "x_studio_colonia",
    "x_studio_descripcion_completa",
    "x_studio_descripcion_corta",
]

SERVER_CODE = r"""
if record and record.is_published:
    missing = []

    if not record.x_studio_tipo_de_inmueble:
        missing.append("Tipo de inmueble")
    elif record.x_studio_tipo_de_inmueble not in ["Casa", "Departamento", "Terreno", "Local comercial", "Bodega", "Oficina"]:
        raise UserError(
            "No se puede publicar la vivienda. El Tipo de inmueble '%s' todavía no tiene un mapeo válido a EasyBroker. "
            "Usa Casa, Departamento, Terreno, Local comercial, Bodega u Oficina."
            % record.x_studio_tipo_de_inmueble
        )

    if not record.x_studio_tipo_de_operacion:
        missing.append("Tipo de operación")
    elif record.x_studio_tipo_de_operacion not in ["Venta", "Renta"]:
        raise UserError(
            "No se puede publicar la vivienda. El Tipo de operación debe ser Venta o Renta para EasyBroker."
        )

    if not record.x_studio_estatus_comercial:
        missing.append("Estatus comercial")
    if not record.x_studio_estado:
        missing.append("Estado")
    if not (record.x_studio_ciudad or record.x_studio_municipio_o_alcaldia):
        missing.append("Ciudad o municipio/alcaldía")
    if not record.x_studio_colonia:
        missing.append("Colonia")
    if not (record.x_studio_descripcion_completa or record.x_studio_descripcion_corta):
        missing.append("Descripción completa o descripción corta")
    if not record.list_price or record.list_price <= 0:
        missing.append("Precio mayor a 0")

    if missing:
        raise UserError(
            "No se puede publicar esta vivienda porque faltan datos obligatorios para EasyBroker:\n- "
            + "\n- ".join(missing)
        )
""".strip()


def env(name):
    value = os.environ.get(name)
    if not value:
        print(f"Falta variable de entorno {name}", file=sys.stderr)
        sys.exit(1)
    return value


def main():
    url = env("ODOO_URL").rstrip("/")
    db = env("ODOO_DB")
    username = env("ODOO_USERNAME")
    api_key = env("ODOO_API_KEY")

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, username, api_key, {})
    if not uid:
        raise SystemExit("No se pudo autenticar contra Odoo.")

    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")

    model_rows = models.execute_kw(
        db, uid, api_key,
        "ir.model", "search_read",
        [[("model", "=", ODOO_MODEL)]],
        {"fields": ["id", "name"], "limit": 1},
    )
    if not model_rows:
        raise SystemExit("No se encontró ir.model para product.template.")
    model_id = model_rows[0]["id"]

    field_rows = models.execute_kw(
        db, uid, api_key,
        "ir.model.fields", "search_read",
        [[("model_id", "=", model_id), ("name", "in", REQUIRED_FIELD_NAMES)]],
        {"fields": ["id", "name"]},
    )
    found = {row["name"]: row["id"] for row in field_rows}
    missing_schema = [name for name in REQUIRED_FIELD_NAMES if name not in found]
    if missing_schema:
        raise SystemExit(
            "Faltan campos esperados en product.template: " + ", ".join(missing_schema)
        )

    automation_ids = models.execute_kw(
        db, uid, api_key,
        "base.automation", "search",
        [[("name", "=", AUTOMATION_NAME), ("model_id", "=", model_id)]],
        {"limit": 1},
    )

    automation_vals = {
        "name": AUTOMATION_NAME,
        "model_id": model_id,
        "trigger": "on_create_or_write",
        "filter_domain": "[('is_published', '=', True)]",
        "active": True,
        "trigger_field_ids": [(6, 0, list(found.values()))],
    }

    if automation_ids:
        automation_id = automation_ids[0]
        models.execute_kw(
            db, uid, api_key,
            "base.automation", "write",
            [[automation_id], automation_vals],
        )
        print(f"Regla actualizada: {AUTOMATION_NAME} (ID {automation_id})")
    else:
        automation_id = models.execute_kw(
            db, uid, api_key,
            "base.automation", "create",
            [automation_vals],
        )
        print(f"Regla creada: {AUTOMATION_NAME} (ID {automation_id})")

    action_ids = models.execute_kw(
        db, uid, api_key,
        "ir.actions.server", "search",
        [[("name", "=", ACTION_NAME), ("model_id", "=", model_id)]],
        {"limit": 1},
    )

    action_vals = {
        "name": ACTION_NAME,
        "model_id": model_id,
        "state": "code",
        "code": SERVER_CODE,
        "base_automation_id": automation_id,
    }

    if action_ids:
        action_id = action_ids[0]
        models.execute_kw(
            db, uid, api_key,
            "ir.actions.server", "write",
            [[action_id], action_vals],
        )
        print(f"Acción actualizada: {ACTION_NAME} (ID {action_id})")
    else:
        action_id = models.execute_kw(
            db, uid, api_key,
            "ir.actions.server", "create",
            [action_vals],
        )
        print(f"Acción creada: {ACTION_NAME} (ID {action_id})")

    print("Validación de publicación para EasyBroker instalada correctamente.")


if __name__ == "__main__":
    main()
