import logging

# httpx registra en INFO la URL completa de cada request ("HTTP Request: POST
# https://...zohomcp.com/mcp/<token>/message") y `adk api_server` deja el root en
# INFO: sin esto la URL del MCP de Zoho, con su llave, termina en Cloud Logging.
for _nombre in ("httpx", "httpcore"):
    logging.getLogger(_nombre).setLevel(logging.WARNING)

from descuentos_anticipados.subagents import build_root_agent  # noqa: E402

root_agent = build_root_agent()
