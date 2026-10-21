"""
Emisor: publica el resultado del pipeline en el ticket de Zoho Desk.

Versión cascarón (aún no hay flujo de negocio ni respuesta al estudiante):
  1. Comentario interno (privado) con el resumen del procesamiento.
  2. Reasignación del ticket al agente humano (`ZOHO_ASSIGNEE_AGENT_ID`).
  3. Cierre de la idempotencia (`mark_done`).

Diseño defensivo: nunca rompe el pipeline por un canal caído. Si el comentario
falla se libera el lock (`mark_failed`) y se propaga la excepción para que
Pub/Sub reintente; la asignación es best-effort.

Cuando exista la respuesta al estudiante (reply_content), se suman aquí el
comentario público y el sendReply — ver `agent-saldos-a-favor/.../emisor.py`.
"""
from __future__ import annotations

import logging
import os
from typing import AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from google.genai import types as genai_types

from ..tools import idempotency, zoho_actions
from .common import StateKeys
from .logging_hooks import log_after_agent, log_before_agent

logger = logging.getLogger("adk.pipeline.descuentos_anticipados")


def _flag_enabled(name: str, default: str = "true") -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off")


def _build_internal_summary(state) -> str:
    ticket = state.get(StateKeys.TICKET) or {}
    lineas = [
        "Agente Descuentos Anticipados — resumen del procesamiento",
        f"Ticket: {ticket.get('id', '')}",
        f"Documento: {ticket.get('numero_documento', '') or 'N/D'}",
        f"Período: {ticket.get('periodo', '') or 'N/D'}",
        "Estado: flujo de negocio aún no implementado; el ticket queda para revisión humana.",
    ]
    if state.get(StateKeys.ERROR):
        lineas.append(f"Error: {state.get(StateKeys.ERROR)}")
    if state.get(StateKeys.ERROR_DETALLE):
        lineas.append(f"Detalle técnico: {state.get(StateKeys.ERROR_DETALLE)}")
    return "\n".join(lineas)


class EmisorAgent(BaseAgent):
    """Comenta y asigna el ticket en Zoho Desk; hace skip seguro si no puede."""

    model_config = {"arbitrary_types_allowed": True}

    def _event(self, text: str, delta: dict) -> Event:
        return Event(
            author=self.name,
            actions=EventActions(state_delta=delta),
            content=genai_types.Content(parts=[genai_types.Part(text=text)]),
        )

    async def _asignar_a_humano(self, ticket_id: str, org_id: str, delta: dict) -> None:
        """Reasigna el ticket al agente humano. Un fallo aquí deja el ticket sin dueño, no sin comentario.

        Las tools de Zoho no lanzan, devuelven {"error": ...}, por eso se inspecciona el resultado.
        """
        try:
            asignacion = await zoho_actions.assign_ticket_owner(ticket_id=ticket_id, org_id=org_id)
            delta[StateKeys.ZOHO_ASSIGNEE] = asignacion
            if isinstance(asignacion, dict) and (asignacion.get("skipped") or asignacion.get("error")):
                logger.warning("EMISOR_ASSIGN_SKIP ticket=%s %s", ticket_id, asignacion)
            else:
                logger.info("EMISOR_TICKET_REASIGNADO ticket=%s", ticket_id)
        except Exception as exc:  # noqa: BLE001 — la asignación no tumba el pipeline
            logger.error("EMISOR_ASSIGN_ERR %s", exc)
            delta[StateKeys.ZOHO_ASSIGNEE] = {"error": str(exc)}

    async def _run_async_impl(self, ctx: InvocationContext) -> AsyncGenerator[Event, None]:
        state = ctx.session.state

        if state.get(StateKeys.SKIP_IDEMPOTENTE):
            logger.info("EMISOR_SKIP_IDEMPOTENTE")
            yield self._event("[emisor] Skip — idempotencia (duplicado)", {})
            return

        ticket = state.get(StateKeys.TICKET)
        if not ticket:
            logger.warning("EMISOR_SKIP sin ticket en el estado")
            yield self._event("[emisor] Skip — no hay ticket en el estado", {})
            return

        ticket_id = str(ticket.get("id", "")).strip()
        org_id = str(ticket.get("org_id", "") or "").strip()

        if not ticket_id or not zoho_actions.is_configured():
            logger.warning("EMISOR_SKIP ticket_id=%s zoho_configurado=%s", ticket_id, zoho_actions.is_configured())
            yield self._event("[emisor] Skip — falta ticket_id o ZOHO_MCP_URL", {})
            return

        delta: dict = {}

        if _flag_enabled("ZOHO_COMMENT_ENABLED"):
            comentario = await zoho_actions.add_ticket_comment(
                ticket_id=ticket_id,
                content=_build_internal_summary(state),
                org_id=org_id,
                is_public=False,
            )
            delta[StateKeys.ZOHO_COMMENT] = comentario
            if isinstance(comentario, dict) and comentario.get("error"):
                # Sin comentario no queda rastro del caso: se libera el lock y se
                # propaga para que Pub/Sub reintente.
                await idempotency.mark_failed(ticket_id)
                logger.error("EMISOR_FALLO_TOTAL ticket=%s: %s", ticket_id, comentario)
                yield self._event(f"[emisor] FALLO ticket {ticket_id}: {comentario}", delta)
                raise RuntimeError(
                    f"Emisor: no se pudo comentar el ticket {ticket_id}; se deja a Pub/Sub reintentar."
                )

        await self._asignar_a_humano(ticket_id, org_id, delta)
        await idempotency.mark_done(ticket_id)

        yield self._event(f"[emisor] ticket {ticket_id}: comentado y asignado", delta)


def build_agente_emisor() -> EmisorAgent:
    return EmisorAgent(
        name="agente_emisor",
        description="Publica el comentario interno en Zoho y asigna el ticket al agente humano.",
        before_agent_callback=log_before_agent,
        after_agent_callback=log_after_agent,
    )
