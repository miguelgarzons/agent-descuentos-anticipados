from descuentos_anticipados.subagents.common import StateKeys
from descuentos_anticipados.subagents.receptor import (
    _parse_payload,
    _unwrap_trigger_envelope,
    build_agente_receptor,
)


async def test_normaliza_ticket(agent_runner, sample_raw_ticket):
    _, state = await agent_runner(build_agente_receptor(), {StateKeys.RAW_TICKET: sample_raw_ticket})
    ticket = state[StateKeys.TICKET]
    assert ticket["id"] == "7777000010"
    assert ticket["numero_documento"] == "1055678901"
    assert ticket["periodo"] == "2026-2"
    assert ticket["nombre"] == "Ana Pérez"
    assert ticket["description"] == "Solicito el descuento por pago anticipado."


async def test_toma_ticket_del_mensaje(agent_runner, sample_raw_ticket):
    import json

    _, state = await agent_runner(build_agente_receptor(), {}, message=json.dumps(sample_raw_ticket))
    assert state[StateKeys.TICKET]["id"] == "7777000010"


async def test_sin_ticket_registra_error(agent_runner):
    _, state = await agent_runner(build_agente_receptor(), {})
    assert StateKeys.ERROR in state
    assert StateKeys.TICKET not in state


async def test_duplicado_marca_skip(agent_runner, sample_raw_ticket, monkeypatch):
    from descuentos_anticipados.tools import idempotency

    async def _dup(_ticket_id):
        return idempotency.DUPLICATE

    monkeypatch.setattr(idempotency, "claim", _dup)
    _, state = await agent_runner(build_agente_receptor(), {StateKeys.RAW_TICKET: sample_raw_ticket})
    assert state[StateKeys.SKIP_IDEMPOTENTE] is True
    assert StateKeys.TICKET not in state


def test_desenvuelve_sobre_pubsub(sample_raw_ticket):
    envelope = {"data": sample_raw_ticket, "attributes": {}}
    assert _unwrap_trigger_envelope(envelope)["id"] == "7777000010"


def test_parse_payload_acepta_repr_de_python():
    assert _parse_payload("{'id': '1', 'ok': True}") == {"id": "1", "ok": True}
