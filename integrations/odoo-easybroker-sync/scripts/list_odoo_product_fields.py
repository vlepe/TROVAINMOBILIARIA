#!/usr/bin/env python3
"""
Utilidad de una sola vez: lista los campos técnicos reales del modelo de
producto en tu Odoo (donde viven las viviendas), para poder llenar los
FIELD_* de import_from_easybroker.py y sync_to_easybroker.py con los
nombres correctos en vez de adivinar.

Odoo tiene instalado un módulo de bienes raíces que agrega una pestaña
"Información inmobiliaria" (Recámaras, Tipo de operación, Ubicación, etc.)
sobre el modelo estándar de producto (product.template). Ese módulo le
puso a cada campo un nombre técnico interno que NO se ve en la pantalla
normal — solo se ve activando el modo desarrollador en Odoo, o —más fácil—
pidiéndoselo a la API con este script.

Uso:
    ODOO_URL=https://trovainmobiliaria.odoo.com \
    ODOO_DB=trovainmobiliaria \
    ODOO_USERNAME=tu_correo \
    ODOO_API_KEY=tu_api_key \
    python scripts/list_odoo_product_fields.py

Filtra automáticamente los campos cuyo nombre o etiqueta suena a algo
relacionado con bienes raíces (recámaras, baños, colonia, m2, etc.) para
no tener que leer las ~200 columnas estándar de un producto de Odoo.
Pasa --all como argumento si quieres ver la lista completa sin filtrar.
"""

import os
import sys
import xmlrpc.client

MODEL = os.environ.get("ODOO_PRODUCT_MODEL", "product.template")

KEYWORDS = [
    "recamar", "bano", "baño", "estacionamient", "terreno", "construcc",
    "colonia", "municipio", "estado", "ciudad", "codigo_postal", "cp",
    "latitud", "longitud", "clave", "inmueble", "operacion", "oportunidad",
    "comercial", "juridic", "publicad", "amenidad", "desarrollo",
    "propiedad", "inmobiliari", "referencia", "default_code", "list_price",
    "is_published", "website_description", "descripcion",
]


def env(name):
    value = os.environ.get(name)
    if not value:
        print(f"Falta la variable de entorno {name}.", file=sys.stderr)
        sys.exit(1)
    return value


def main():
    show_all = "--all" in sys.argv

    url = env("ODOO_URL").rstrip("/")
    db = env("ODOO_DB")
    username = env("ODOO_USERNAME")
    api_key = env("ODOO_API_KEY")

    common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
    uid = common.authenticate(db, username, api_key, {})
    if not uid:
        print("No se pudo autenticar contra Odoo. Revisa ODOO_DB / ODOO_USERNAME / ODOO_API_KEY.", file=sys.stderr)
        sys.exit(1)

    models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
    fields = models.execute_kw(
        db, uid, api_key,
        MODEL, "fields_get",
        [],
        {"attributes": ["string", "type", "required", "relation"]},
    )

    rows = []
    for technical_name, info in fields.items():
        label = info.get("string", "")
        haystack = f"{technical_name} {label}".lower()
        if show_all or any(kw in haystack for kw in KEYWORDS):
            rows.append((technical_name, info.get("type", ""), label, info.get("relation") or ""))

    rows.sort(key=lambda r: r[0])

    name_w = max((len(r[0]) for r in rows), default=20)
    type_w = max((len(r[1]) for r in rows), default=10)
    print(f"{'nombre técnico'.ljust(name_w)}  {'tipo'.ljust(type_w)}  etiqueta (lo que ves en pantalla)")
    print("-" * (name_w + type_w + 40))
    for technical_name, ftype, label, relation in rows:
        extra = f" -> {relation}" if relation else ""
        print(f"{technical_name.ljust(name_w)}  {ftype.ljust(type_w)}  {label}{extra}")

    print(f"\n{len(rows)} campo(s) mostrados de {len(fields)} totales en el modelo {MODEL}.")
    if not show_all:
        print("(usa --all para ver los ~200 campos estándar de producto sin filtrar)")


if __name__ == "__main__":
    main()
