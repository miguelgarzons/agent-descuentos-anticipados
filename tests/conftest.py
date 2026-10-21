from __future__ import annotations

import os
import secrets


def _fixture_secret(label: str) -> str:
    """Genera un valor sintético (no una credencial real) para usar en pruebas."""
    return f"{label}-{secrets.token_hex(6)}"


# Debe fijarse ANTES de que cualquier módulo bajo `descuentos_anticipados` se
# importe, porque agent.py construye root_agent (y lee AGENT_MODEL) al importarse.
os.environ.setdefault("AGENT_MODEL", "gemini-2.5-pro")
os.environ.setdefault("GEMINI_API_KEY", _fixture_secret("fixture-gemini-key"))
os.environ.setdefault("ZOHO_MCP_URL", "")
os.environ.setdefault("ZOHO_ORG_ID", "")
# Idempotencia desactivada por defecto en tests (no tocar Firestore).
os.environ.setdefault("IDEMPOTENCY_ENABLED", "false")

import pytest
from google.adk.runners import InMemoryRunner
from google.genai import types as genai_types


async def run_agent(agent, initial_state: dict, app_name: str = "test_app", message: str = "run"):
    """Ejecuta un agente ADK vía InMemoryRunner y retorna (eventos, estado_final)."""
    runner = InMemoryRunner(agent=agent, app_name=app_name)
    await runner.session_service.create_session(
        app_name=app_name,
        user_id="test-user",
        session_id="test-session",
        state=initial_state,
    )
    events = []
    async for event in runner.run_async(
        user_id="test-user",
        session_id="test-session",
        new_message=genai_types.Content(role="user", parts=[genai_types.Part(text=message)]),
    ):
        events.append(event)
    final_session = await runner.session_service.get_session(
        app_name=app_name, user_id="test-user", session_id="test-session"
    )
    return events, final_session.state


@pytest.fixture
def agent_runner():
    return run_agent


@pytest.fixture
def sample_raw_ticket() -> dict:
    return {
        "id": "7777000010",
        "contactId": "7777000011",
        "subject": "Quiero el descuento por pago anticipado",
        "description": "<p>Solicito el descuento por pago anticipado.</p>",
        "cf_numero_de_documento": "1055678901",
        "cf_periodo": "2026-2",
        "org_id": "707762349",
        "email": "estudiante@test.local",
        "departmentId": "dept-1",
        "contact": {"firstName": "Ana", "lastName": "Pérez"},
    }
