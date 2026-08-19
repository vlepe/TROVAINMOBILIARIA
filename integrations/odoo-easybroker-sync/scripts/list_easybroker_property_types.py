#!/usr/bin/env python3
"""
Utilidad de una sola vez: consulta el catálogo real de tipos de propiedad de
tu cuenta de EasyBroker, para reemplazar los valores de ejemplo en
PROPERTY_TYPE_MAP dentro de sync_to_easybroker.py con los símbolos exactos.

Uso:
    EASYBROKER_API_KEY=tu_api_key python scripts/list_easybroker_property_types.py
"""

import os
import sys

import requests

API_KEY = os.environ.get("EASYBROKER_API_KEY")
if not API_KEY:
    print("Define la variable de entorno EASYBROKER_API_KEY antes de correr esto.", file=sys.stderr)
    sys.exit(1)

response = requests.get(
    "https://api.easybroker.com/v1/property_types",
    headers={"X-Authorization": API_KEY, "accept": "application/json"},
    timeout=20,
)
response.raise_for_status()

for item in response.json().get("content", response.json()):
    print(item)
