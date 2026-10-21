import pytest

from descuentos_anticipados.subagents.common import StateKeys
from descuentos_anticipados.subagents.emisor import build_agente_emisor
from descuentos_anticipados.tools import idempotency, zoho_actions

TICKET = {"id": "42", "org_id": "9", "numero_documento": "1055678901", "periodo": "2026-2"}


@pytest.fixture
def zoho(monkeypatch):
    llamadas = {"comment": [], "assign": [], "done": [], "failed": []}
    monkeypatch.setattr(zoho_actions, "is_configured", lambda: True)

    async def _comment(**kwargs):
        llamadas["comment"].append(kwargs)
        return llamadas.get("comment_result", {"id": "c1"})

    async def _assign(**kwargs):
        llamadas["assign"].append(kwargs)
        return {"ok": True}

    async def _done(ticket_id):
        llamadas["done"].append(ticket_id)

    async def _failed(ticket_id):
        llamadas["failed"].append(ticket_id)

    monkeypatch.setattr(zoho_actions, "add_ticket_comment", _comment)
    monkeypatch.setattr(zoho_actions, "assign_ticket_owner", _assign)
    monkeypatch.setattr(idempotency, "mark_done", _done)
    monkeypatch.setattr(idempotency, "mark_failed", _failed)
    return llamadas


async def test_comenta_asigna_y_marca_done(agent_runner, zoho):
    _, state = await agent_runner(build_agente_emisor(), {StateKeys.TICKET: TICKET})
    assert zoho["comment"][0]["is_public"] is False
    assert "1055678901" in zoho["comment"][0]["content"]
    assert zoho["assign"][0]["ticket_id"] == "42"
    assert zoho["done"] == ["42"]
    assert state[StateKeys.ZOHO_ASSIGNEE] == {"ok": True}


async def test_skip_si_es_duplicado(agent_runner, zoho):
    await agent_runner(build_agente_emisor(), {StateKeys.TICKET: TICKET, StateKeys.SKIP_IDEMPOTENTE: True})
    assert not zoho["comment"] and not zoho["assign"]


async def test_skip_sin_zoho_configurado(agent_runner, zoho, monkeypatch):
    monkeypatch.setattr(zoho_actions, "is_configured", lambda: False)
    await agent_runner(build_agente_emisor(), {StateKeys.TICKET: TICKET})
    assert not zoho["comment"]


async def test_fallo_del_comentario_libera_lock_y_propaga(agent_runner, zoho):
    zoho["comment_result"] = {"error": "zoho_mcp_connection_failed"}
    with pytest.raises(RuntimeError):
        await agent_runner(build_agente_emisor(), {StateKeys.TICKET: TICKET})
    assert zoho["failed"] == ["42"]
    assert not zoho["done"]
