"""Escritura hacia Zoho Desk vía MCP.

Reutiliza el mismo servidor MCP de Zoho (ZOHO_MCP_URL) que ya usa el stack para
publicar en el ticket:
  - `send_ticket_reply`  → respuesta pública al estudiante (correo).
  - `add_ticket_comment` → comentario interno con el resultado del pipeline.

Las variables de entorno se leen dinámicamente (igual que `api_auth`) para
soportar múltiples entornos y facilitar las pruebas con monkeypatch.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger("adk.pipeline.descuentos_anticipados")

_DEFAULT_TIMEOUT = 30.0

# El servidor MCP de Zoho exige estos Accept/Content-Type en el handshake.
_BASE_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

# Remitente por defecto (mismo portal Zoho Desk de CUN que usa apoyo_economico).
# Solo se usa como último recurso si no hay override por env ni dirección activa en el portal.
_DEFAULT_REPLY_FROM = "support@corporaciontelecampus.zohodesk.com"


def _mcp_url() -> str:
    return os.getenv("ZOHO_MCP_URL", "").strip()


def _org_id() -> str:
    return os.getenv("ZOHO_ORG_ID", "").strip()


def _mcp_headers() -> dict[str, str]:
    """Headers para el MCP: base obligatorios + extras opcionales (JSON en ZOHO_MCP_HEADERS)."""
    headers = dict(_BASE_HEADERS)
    raw = os.getenv("ZOHO_MCP_HEADERS", "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("ZOHO_MCP_HEADERS no es JSON válido; se ignora.")
        else:
            if isinstance(data, dict):
                headers.update({str(k): str(v) for k, v in data.items()})
    return headers


def is_configured() -> bool:
    return bool(_mcp_url())


def _result_to_dict(result: Any) -> dict:
    """Convierte un CallToolResult del MCP en un dict Python."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured

    texts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            texts.append(text)
    payload = "\n".join(texts).strip()
    if not payload:
        return {}
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return {"raw": payload}
    return parsed if isinstance(parsed, dict) else {"data": parsed}


# La URL del MCP lleva el token en la ruta (https://<tenant>.zohomcp.com/mcp/<token>/message).
_MCP_RUTA_RE = re.compile(r"/mcp/[^\s'\"<>]+")


def redactar_url_mcp(texto: str) -> str:
    """Quita de un texto la URL del MCP de Zoho, que lleva el token en la ruta.

    Ante un 4xx/5xx del MCP, httpx arma el mensaje de la excepción con la URL
    completa ("Client error '400 Bad Request' for url 'https://...'"), y ese
    mensaje termina en el log y en el estado de la sesión.
    """
    url = _mcp_url()
    if url:
        texto = texto.replace(url, "<ZOHO_MCP_URL>")
    return _MCP_RUTA_RE.sub("/mcp/<redactado>", texto)


class FiltroUrlMcp(logging.Filter):
    """Redacta la URL del MCP en el mensaje y el traceback de cada registro.

    Un filtro de logger solo ve los registros creados en ESE logger, así que se
    instala por nombre (ver `instalar_filtro_url_mcp`).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Lo que lanza un filtro no lo atrapa logging: rompería la llamada de log
        # de quien registra. Ante cualquier fallo, el registro sale tal cual.
        try:
            mensaje = record.getMessage()
            limpio = redactar_url_mcp(mensaje)
            if limpio != mensaje:
                record.msg, record.args = limpio, None
            # El Formatter estándar reutiliza exc_text: se arma aquí, redactado.
            if record.exc_info and not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            if record.exc_text:
                traza = redactar_url_mcp(record.exc_text)
                if traza != record.exc_text:
                    record.exc_text = traza
                    # Un formatter que rearme la traza desde exc_info la filtraría.
                    record.exc_info = None
        except Exception:  # noqa: BLE001
            pass
        return True


# Loggers que pueden escribir la URL del MCP: el del pipeline, el transporte de
# mcp (su post_writer hace logger.exception con el HTTPStatusError), la sesión
# de mcp (se llama "client") y httpx/httpcore.
_LOGGERS_CON_URL_MCP = (
    "adk.pipeline.descuentos_anticipados",
    "mcp.client.streamable_http",
    "client",
    "httpx",
    "httpcore",
)


def instalar_filtro_url_mcp() -> None:
    """Instala `FiltroUrlMcp` en los loggers que pueden registrar la URL (idempotente)."""
    for nombre in _LOGGERS_CON_URL_MCP:
        log = logging.getLogger(nombre)
        if not any(isinstance(f, FiltroUrlMcp) for f in log.filters):
            log.addFilter(FiltroUrlMcp())


def _exc_detail(exc: BaseException) -> str:
    """Extrae un mensaje legible, desenrollando ExceptionGroup de anyio/MCP.

    Sale redactado: el mensaje de un HTTPStatusError trae la URL del MCP.
    """
    subs = getattr(exc, "exceptions", None)
    if subs:
        return " || ".join(_exc_detail(e) for e in subs)
    return redactar_url_mcp(f"{type(exc).__name__}: {exc}")


async def _call_zoho(tool_name: str, arguments: dict) -> dict:
    """Invoca una tool del MCP de Zoho y devuelve su resultado como dict.

    Los fallos de conexión/handshake (p. ej. un 400 'Invalid server domain' del
    servidor) se devuelven como {"error": ...} en lugar de propagarse, para no
    tumbar el pipeline y dejar el motivo real en el estado.
    """
    url = _mcp_url()
    if not url:
        raise RuntimeError("ZOHO_MCP_URL no configurado en el entorno.")

    logger.info("ZOHO_MCP_CALL tool=%s", tool_name)
    try:
        async with streamablehttp_client(url, headers=_mcp_headers(), timeout=_DEFAULT_TIMEOUT) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
    except Exception as exc:  # noqa: BLE001 — conexión/transporte MCP
        detail = _exc_detail(exc)
        logger.error("ZOHO_MCP_CONN_ERR tool=%s: %s", tool_name, detail)
        return {"error": "zoho_mcp_connection_failed", "detail": detail}

    data = _result_to_dict(result)
    if getattr(result, "isError", False):
        logger.error("ZOHO_MCP_ERR tool=%s data=%s", tool_name, redactar_url_mcp(str(data))[:300])
        return {"error": f"MCP tool {tool_name} error", "detail": data}
    return data


async def get_contact_email(contact_id: str) -> str:
    """Obtiene el correo del contacto (para el destinatario del sendReply)."""
    if not contact_id:
        return ""
    args = {
        "path_variables": {"contactId": str(contact_id)},
        "query_params": {"orgId": _org_id()},
    }
    data = await _call_zoho("ZohoDesk_getContact", args)
    if data.get("error"):
        return ""
    return str(data.get("email") or data.get("secondaryEmail") or "").strip()


async def get_contact_name(contact_id: str) -> str:
    """Nombre visible del contacto que abrió el ticket (para el saludo)."""
    if not contact_id:
        return ""
    args = {
        "path_variables": {"contactId": str(contact_id)},
        "query_params": {"orgId": _org_id()},
    }
    data = await _call_zoho("ZohoDesk_getContact", args)
    if data.get("error"):
        return ""
    first = str(data.get("firstName") or "").strip()
    last = str(data.get("lastName") or "").strip()
    name = " ".join(p for p in (first, last) if p)
    return name or str(data.get("fullName") or "").strip()


def _configured_from_email() -> str:
    """Override explícito por entorno (ZOHO_FROM_EMAIL o ZOHO_REPLY_FROM_EMAIL)."""
    return (
        os.getenv("ZOHO_FROM_EMAIL", "").strip()
        or os.getenv("ZOHO_REPLY_FROM_EMAIL", "").strip()
    )


def _collect_emails(value: Any, found: list[str]) -> None:
    """Recolecta recursivamente cualquier cadena que parezca un email."""
    if isinstance(value, dict):
        for v in value.values():
            _collect_emails(v, found)
    elif isinstance(value, list):
        for item in value:
            _collect_emails(item, found)
    elif isinstance(value, str) and "@" in value and "." in value:
        email = value.strip()
        if email and email not in found:
            found.append(email)


def _pick_reply_from_email(payload: Any) -> str:
    """Elige una dirección 'from' del payload de getReplyMailAddresses.

    Prioriza data[].address; si no, escanea cualquier email y prefiere los de
    zohodesk.com / support@ (igual que el agente apoyo_economico).
    """
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("mailReplyAddress")
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    addr = str(
                        item.get("address")
                        or item.get("fromEmailAddress")
                        or item.get("email")
                        or ""
                    ).strip()
                    if addr:
                        return addr

    emails: list[str] = []
    _collect_emails(payload, emails)
    for email in emails:
        low = email.lower()
        if "zohodesk.com" in low or low.startswith("support@"):
            return email
    return emails[0] if emails else ""


async def get_default_from_address(department_id: str = "") -> str:
    """Devuelve SIEMPRE una dirección 'from' usable para responder.

    Prioridad:
      1. Override por entorno (ZOHO_FROM_EMAIL / ZOHO_REPLY_FROM_EMAIL).
      2. Dirección activa del portal: por departamento y luego a nivel organización.
      3. Fallback garantizado (_DEFAULT_REPLY_FROM) para que el correo siempre pueda enviarse.
    """
    env_from = _configured_from_email()
    if env_from:
        return env_from

    filters: list[dict] = []
    if department_id:
        filters.append({"departmentId": str(department_id)})
    filters.append({})

    for extra in filters:
        query = {"orgId": _org_id(), "isActive": True, **extra}
        data = await _call_zoho("ZohoDesk_getReplyMailAddresses", {"query_params": query})
        if data.get("error"):
            continue
        email = _pick_reply_from_email(data)
        if email:
            return email

    return _DEFAULT_REPLY_FROM


async def send_ticket_reply(
    ticket_id: str,
    to_email: str,
    content_html: str,
    from_email: str,
    org_id: str = "",
) -> dict:
    """Envía una respuesta pública (correo) al estudiante en el ticket."""
    args = {
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": org_id or _org_id()},
        "body": {
            "channel": "EMAIL",
            "contentType": "html",
            "content": content_html,
            "to": to_email,
            "fromEmailAddress": from_email,
            "isForward": False,
        },
    }
    return await _call_zoho("ZohoDesk_sendReply", args)


async def add_ticket_comment(
    ticket_id: str,
    content: str,
    org_id: str = "",
    is_public: bool = False,
    content_type: str = "plainText",
) -> dict:
    """Agrega un comentario al ticket (privado por defecto; público para la respuesta al estudiante)."""
    args = {
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": org_id or _org_id()},
        "body": {
            "content": content,
            "contentType": content_type,
            "isPublic": bool(is_public),
            "attachmentIds": [],
        },
    }
    return await _call_zoho("ZohoDesk_createTicketComment", args)


async def assign_ticket_owner(ticket_id: str, org_id: str = "") -> dict:
    """Reasigna el propietario del ticket al agente humano responsable.

    Patrón del agent-categorizador: ZohoDesk_updateTicket con body.assigneeId.
    ZOHO_ASSIGNEE_AGENT_ID acepta el ID numérico del agente en Zoho o su correo
    (en ese caso se resuelve el ID vía ZohoDesk_getAgents).
    """
    valor = os.getenv("ZOHO_ASSIGNEE_AGENT_ID", "").strip()
    if not valor:
        return {"skipped": "ZOHO_ASSIGNEE_AGENT_ID no configurado"}

    org = org_id or _org_id()
    agent_id = valor
    if "@" in valor:
        data = await _call_zoho(
            "ZohoDesk_getAgents",
            {"query_params": {"orgId": org, "searchStr": valor}},
        )
        agent_id = ""
        agentes = data.get("data") if isinstance(data, dict) else None
        # El MCP puede devolver la lista doble-codificada como string JSON.
        if isinstance(agentes, str):
            try:
                agentes = json.loads(agentes)
            except json.JSONDecodeError:
                agentes = None
        for a in agentes or []:
            if not isinstance(a, dict):
                continue
            if str(a.get("emailId", "")).strip().lower() == valor.lower():
                agent_id = str(a.get("id", ""))
                break
        if not agent_id:
            logger.warning(
                "ZOHO_ASSIGN agente no encontrado por correo %s (respuesta: %s)",
                valor, str(data)[:200],
            )
            return {"error": f"agente con correo {valor} no encontrado en Zoho"}

    return await _call_zoho(
        "ZohoDesk_updateTicket",
        {
            "query_params": {"orgId": org},
            "path_variables": {"ticketId": str(ticket_id)},
            "body": {"assigneeId": str(agent_id)},
        },
    )


async def close_ticket(ticket_id: str, org_id: str = "") -> dict:
    """Cierra el ticket vía ZohoDesk_closeTickets (cierre determinístico, mismo
    patrón que agent-apoyo-economico)."""
    args = {
        "query_params": {"orgId": org_id or _org_id()},
        "body": {"ids": [str(ticket_id)]},
    }
    return await _call_zoho("ZohoDesk_closeTickets", args)


async def update_ticket_status(ticket_id: str, status: str = "", org_id: str = "") -> dict:
    """Cambia el estado del ticket (p. ej. para cerrarlo cuando falta el período).

    El nombre del estado debe existir en el portal de Zoho Desk; se puede
    sobreescribir con ZOHO_CLOSE_STATUS (default "Cerrado").
    """
    status = (status or os.getenv("ZOHO_CLOSE_STATUS", "Cerrado")).strip()
    args = {
        "path_variables": {"ticketId": str(ticket_id)},
        "query_params": {"orgId": org_id or _org_id()},
        "body": {"status": status},
    }
    return await _call_zoho("ZohoDesk_updateTicket", args)
