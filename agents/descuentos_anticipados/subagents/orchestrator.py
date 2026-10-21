"""
Orquestador: construye el SequentialAgent raíz del flujo Descuentos Anticipados.

Pipeline actual (cascarón de Zoho; el flujo de negocio aún no está definido):
    1. receptor → parsea y normaliza el ticket, reclama la idempotencia
    2. emisor   → comentario interno en Zoho y reasignación al agente humano

Los subagentes de negocio (clasificador, validador, ejecutor, respondedor) se
insertan entre receptor y emisor a medida que se definan.
"""
from __future__ import annotations

from google.adk.agents import SequentialAgent

from .emisor import build_agente_emisor
from .logging_hooks import log_after_agent, log_before_agent
from .receptor import build_agente_receptor


def build_root_agent() -> SequentialAgent:
    return SequentialAgent(
        name="agente_descuentos_anticipados",
        description=(
            "Pipeline secuencial para gestión de Descuentos Anticipados en CUN. "
            "Recibe el ticket de Zoho Desk y lo deja anotado y asignado a un asesor."
        ),
        before_agent_callback=log_before_agent,
        after_agent_callback=log_after_agent,
        sub_agents=[
            build_agente_receptor(),
            build_agente_emisor(),
        ],
    )
