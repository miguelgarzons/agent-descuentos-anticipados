"""
Receptor: extrae y normaliza el ticket entrante.

Fuentes de entrada (en orden de prioridad):
  1. `raw_ticket` en el estado de sesión (lo inyecta el webhook/categorizador).
  2. El mensaje de entrada (`user_content`) cuando el JSON del ticket llega como
     texto — por ejemplo al probar manualmente desde la interfaz web de ADK.

Valida los campos mínimos y escribe `ticket` normalizado en el estado.
El payload puede llegar como dict, string JSON o repr de dict de Python.
"""
from __future__ import annotations

import ast
import json
import logging
import re
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types as genai_types

from ..tools import idempotency, zoho_actions
from .common import StateKeys
from .logging_hooks import log_before_agent, log_after_agent

logger = logging.getLogger("adk.pipeline.descuentos_anticipados")

REQUIRED_FIELDS = ["id", "contactId", "cf_numero_de_documento"]

# Valores de relleno que Zoho pone en campos de selección sin diligenciar.
_PLACEHOLDER_VALUES = {"--ninguna--", "--ninguno--", "- ninguno -", "-ninguno-", "ninguno", "n/a", "none"}
_HTML_TAG_RE = re.compile(r"<[^>]+>")

def _cf(raw: dict, key: str) -> str:
    """Lee un custom field del ticket: primero en la raíz y luego dentro del
    objeto anidado `cf` (Zoho envía ambos según el canal). Filtra placeholders."""
    value = raw.get(key)
    if value in (None, ""):
        value = (raw.get("cf") or {}).get(key)
    text = str(value).strip() if value is not None else ""
    return "" if text.lower() in _PLACEHOLDER_VALUES else text


def _plain_text(html: str) -> str:
    """Convierte la description HTML de Zoho a texto plano para el clasificador."""
    text = _HTML_TAG_RE.sub(" ", str(html or ""))
    return re.sub(r"\s+", " ", text).strip()


def _contact_name_from_payload(raw: dict) -> str:
    """Nombre del contacto si el payload del ticket lo trae embebido."""
    contact = raw.get("contact")
    if isinstance(contact, dict):
        first = str(contact.get("firstName") or "").strip()
        last = str(contact.get("lastName") or "").strip()
        name = " ".join(p for p in (first, last) if p)
        if name:
            return name
    return str(raw.get("contactName") or "").strip()


def _unwrap_trigger_envelope(payload: dict) -> dict:
    """Desenvuelve el sobre que agrega el endpoint `/trigger/pubsub` de ADK.

    El trigger nativo NO entrega el ticket crudo: lo envuelve como
    ``{"data": <ticket>, "attributes": {...}}`` (ver trigger_routes.py de ADK).
    Cuando detectamos ese sobre, devolvemos el ticket real de ``data``. Si el
    payload ya es un ticket plano (p. ej. prueba manual o /run directo), se
    devuelve tal cual.
    """
    if not isinstance(payload, dict):
        return payload
    if "data" in payload and "attributes" in payload and not payload.get("id"):
        data = payload["data"]
        if isinstance(data, str):
            data = _parse_payload(data) or {}
        if isinstance(data, dict):
            logger.info("RECEPTOR_SRC ticket extraído del sobre del trigger Pub/Sub")
            return data
    return payload


def _extract_from_user_content(ctx: InvocationContext) -> str | None:
    """Devuelve el texto del mensaje de entrada solo si parece un payload de ticket."""
    user_content = getattr(ctx, "user_content", None)
    parts = getattr(user_content, "parts", None) or []
    texts = [p.text for p in parts if isinstance(getattr(p, "text", None), str)]
    text = "\n".join(t for t in texts if t).strip()
    # Solo lo tratamos como ticket si parece un objeto (JSON o dict repr).
    return text if text.startswith("{") else None


def _parse_payload(raw: str) -> dict | None:
    """Parsea un payload de ticket de forma tolerante.

    Soporta JSON estándar y repr de dict de Python (comillas simples, None/True/False).
    Como segundo intento aplana los saltos de línea a espacios, lo que permite parsear
    reprs multilínea (p. ej. cuando `description` trae saltos de línea reales dentro del
    string, como al copiar el objeto del ticket desde la interfaz de ADK/Zoho).
    """
    for text in (raw.strip(), raw.replace("\r", " ").replace("\n", " ").strip()):
        if not text:
            continue
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        try:
            parsed = ast.literal_eval(text)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, SyntaxError):
            pass
    return None


class ReceptorAgent(BaseAgent):
    """Parsea y valida el ticket entrante; escribe StateKeys.TICKET."""

    model_config = {"arbitrary_types_allowed": True}

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state
        raw: Any = state.get(StateKeys.RAW_TICKET)

        # Fallback: si no vino en el estado, intentar extraerlo del mensaje de entrada.
        if raw is None:
            raw = _extract_from_user_content(ctx)
            if raw is not None:
                logger.info("RECEPTOR_SRC raw_ticket tomado del mensaje de entrada")

        if raw is None:
            msg = (
                "raw_ticket no encontrado ni en el estado ni en el mensaje. "
                "Verifica que el webhook lo inyecte o envía el JSON del ticket como mensaje."
            )
            logger.error("RECEPTOR_ERR %s", msg)
            yield Event(
                author=self.name,
                actions=EventActions(state_delta={StateKeys.ERROR: msg}),
                content=genai_types.Content(parts=[genai_types.Part(text=f"[receptor] ERROR: {msg}")]),
            )
            return

        if isinstance(raw, str):
            parsed = _parse_payload(raw)
            if parsed is None:
                msg = "raw_ticket no es JSON válido."
                logger.error("RECEPTOR_ERR %s", msg)
                yield Event(
                    author=self.name,
                    actions=EventActions(state_delta={StateKeys.ERROR: msg}),
                    content=genai_types.Content(parts=[genai_types.Part(text=f"[receptor] ERROR: {msg}")]),
                )
                return
            raw = parsed

        # El trigger Pub/Sub entrega el ticket envuelto en {"data":..., "attributes":...}.
        raw = _unwrap_trigger_envelope(raw)

        # Los cf_ se buscan con `_cf` (raíz y `cf{}` anidado): Zoho manda una u
        # otra forma según el canal, y mirar solo la raíz avisaba de un campo
        # "faltante" que sí venía anidado y que el pipeline usaba sin problema.
        missing = [
            f for f in REQUIRED_FIELDS
            if not (_cf(raw, f) if f.startswith("cf_") else raw.get(f))
        ]
        if missing:
            msg = f"Campos obligatorios faltantes en el ticket: {missing}"
            logger.warning("RECEPTOR_WARN %s", msg)

        # ── Idempotencia: Pub/Sub es at-least-once y el pipeline tiene efectos
        #    externos no idempotentes (escribe en Zoho y, más adelante, en sistemas
        #    externos). Reclamamos el ticket_id; si ya está en proceso/hecho,
        #    cortamos el pipeline. ──
        ticket_id = str(raw.get("id", "")).strip()
        claim = await idempotency.claim(ticket_id)
        if claim == idempotency.DUPLICATE:
            msg = f"Ticket {ticket_id} ya está en procesamiento o fue procesado; se omite (idempotencia)."
            logger.info("RECEPTOR_SKIP_IDEMPOTENTE %s", msg)
            yield Event(
                author=self.name,
                actions=EventActions(state_delta={StateKeys.SKIP_IDEMPOTENTE: True}),
                content=genai_types.Content(parts=[genai_types.Part(text=f"[receptor] {msg}")]),
            )
            return

        periodo = (
            _cf(raw, "cf_periodo")
            or _cf(raw, "cf_periodo_academico")
            or _cf(raw, "cf_orden_period")
            or str(raw.get("periodo") or "")
        ).strip().upper()

        # Nombre de quien abrió el ticket: del payload si viene; si no, consulta
        # best-effort al contacto de Zoho (es cosmético — nunca bloquea el pipeline).
        nombre = _contact_name_from_payload(raw)
        contact_id = str(raw.get("contactId", "") or "")
        if not nombre and contact_id and zoho_actions.is_configured():
            try:
                nombre = await zoho_actions.get_contact_name(contact_id)
            except Exception as exc:  # noqa: BLE001 — el saludo genérico es aceptable
                logger.warning("RECEPTOR_NOMBRE_ERR %s", exc)

        ticket = {
            "id": raw.get("id", ""),
            "contact_id": raw.get("contactId", ""),
            "org_id": raw.get("org_id", ""),
            "email": raw.get("email") or _cf(raw, "cf_correo_electronico_1") or "",
            "department_id": raw.get("departmentId", ""),
            "numero_documento": _cf(raw, "cf_numero_de_documento"),
            "nombre": nombre,
            "subject": raw.get("subject", ""),
            "description": _plain_text(raw.get("description", "")),
            "periodo": periodo,
        }

        # Del documento solo el largo: los primeros dígitos de una cédula corta
        # son casi el número completo.
        logger.info(
            "RECEPTOR_OK ticket_id=%s doc_len=%d periodo=%s",
            ticket["id"],
            len(ticket["numero_documento"]),
            ticket["periodo"] or "-",
        )
        yield Event(
            author=self.name,
            actions=EventActions(state_delta={StateKeys.TICKET: ticket}),
            content=genai_types.Content(
                parts=[genai_types.Part(text=f"[receptor] Ticket {ticket['id']} recibido y normalizado.")]
            ),
        )


def build_agente_receptor() -> ReceptorAgent:
    return ReceptorAgent(
        name="agente_receptor",
        description="Parsea y valida el ticket del webhook; normaliza campos para el pipeline.",
        before_agent_callback=log_before_agent,
        after_agent_callback=log_after_agent,
    )
