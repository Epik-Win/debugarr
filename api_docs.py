"""Helpers for serving Debugarr's OpenAPI document and Swagger UI."""
from __future__ import annotations

import json
from pathlib import Path


OPENAPI_PATH = Path(__file__).with_name("openapi.json")
DEFAULT_SEERR_WEBHOOK_PATH = "/debuggarr/seerr-webhook"


def load_openapi_document(seerr_webhook_path: str) -> dict:
    document = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    paths = document["paths"]
    webhook = paths.pop(DEFAULT_SEERR_WEBHOOK_PATH)
    configured_path = seerr_webhook_path.strip() or DEFAULT_SEERR_WEBHOOK_PATH
    if not configured_path.startswith("/"):
        configured_path = f"/{configured_path}"
    paths[configured_path] = webhook
    return document


def swagger_ui_html() -> str:
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Debugarr API</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-standalone-preset.js"></script>
    <script>
      window.onload = () => SwaggerUIBundle({
        url: "/api/openapi.json",
        dom_id: "#swagger-ui",
        deepLinking: true,
        presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
        layout: "StandaloneLayout"
      });
    </script>
  </body>
</html>"""
