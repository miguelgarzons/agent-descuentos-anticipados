"""Los logs van a Cloud Logging: no deben llevar datos del estudiante ni la URL
del MCP de Zoho (lleva el token en la ruta)."""
from __future__ import annotations

import json
import logging
import secrets
from contextlib import asynccontextmanager

import httpx
import pytest

from descuentos_anticipados.subagents.receptor import build_agente_receptor
from descuentos_anticipados.tools import zoho_actions

PIPELINE_LOGGER = "adk.pipeline.descuentos_anticipados"


@pytest.fixture
def mcp_url(monkeypatch) -> tuple[str, str]:
    """URL sintética del MCP con un token aleatorio (no es una credencial real)."""
    token = secrets.token_hex(16)
    url = f"https://dev-zoho-prueba.zohomcp.com/mcp/{token}/message"
    monkeypatch.setenv("ZOHO_MCP_URL", url)
    return url, token


def _http_400(url: str) -> httpx.HTTPStatusError:
    """El error real que lanza mcp ante un 400 del servidor: su mensaje trae la URL."""
    response = httpx.Response(400, request=httpx.Request("POST", url))
    with pytest.raises(httpx.HTTPStatusError) as info:
        response.raise_for_status()
    assert url in str(info.value)  # precondición: sin redactar, la URL viaja en el mensaje
    return info.value


def test_redactar_quita_la_url_exacta_y_cualquier_ruta_mcp(mcp_url):
    url, token = mcp_url
    texto = f"fallo en {url} y en https://otro.zohomcp.com/mcp/abc123/message"
    limpio = zoho_actions.redactar_url_mcp(texto)
    assert token not in limpio
    assert "abc123" not in limpio
    assert "<ZOHO_MCP_URL>" in limpio
    assert zoho_actions.redactar_url_mcp("sin urls aquí") == "sin urls aquí"


def test_exc_detail_redacta_el_http_status_error_dentro_de_un_exception_group(mcp_url):
    url, token = mcp_url
    grupo = ExceptionGroup("unhandled errors in a TaskGroup", [_http_400(url)])
    detalle = zoho_actions._exc_detail(grupo)
    assert "HTTPStatusError" in detalle
    assert token not in detalle


async def test_call_zoho_no_filtra_la_url_ni_en_el_resultado_ni_en_el_log(mcp_url, monkeypatch, caplog):
    url, token = mcp_url

    @asynccontextmanager
    async def _transporte_que_falla(*_args, **_kwargs):
        raise ExceptionGroup("unhandled errors in a TaskGroup", [_http_400(url)])
        yield  # pragma: no cover

    monkeypatch.setattr(zoho_actions, "streamablehttp_client", _transporte_que_falla)
    caplog.set_level(logging.INFO, logger=PIPELINE_LOGGER)

    resultado = await zoho_actions._call_zoho("ZohoDesk_createTicketComment", {})

    assert resultado["error"] == "zoho_mcp_connection_failed"
    assert token not in json.dumps(resultado)
    assert "ZOHO_MCP_CONN_ERR" in caplog.text
    assert token not in caplog.text


def test_cargar_el_agente_instala_el_filtro_en_todos_los_loggers():
    """Es agent.py, no los tests, quien debe instalarlo: ADK solo carga ese módulo."""
    import descuentos_anticipados.agent  # noqa: F401

    for nombre in zoho_actions._LOGGERS_CON_URL_MCP:
        filtros = logging.getLogger(nombre).filters
        assert any(isinstance(f, zoho_actions.FiltroUrlMcp) for f in filtros), nombre


def test_filtro_redacta_el_mensaje_sin_traceback(mcp_url, caplog):
    """mcp también registra la excepción dentro del mensaje: f"Session termination failed: {exc}"."""
    url, token = mcp_url
    zoho_actions.instalar_filtro_url_mcp()
    caplog.set_level(logging.WARNING, logger="client")

    logging.getLogger("client").warning(f"Session termination failed: {_http_400(url)}")

    assert "Session termination failed" in caplog.text
    assert token not in caplog.text


def test_filtro_redacta_el_traceback_del_transporte_mcp(mcp_url, caplog):
    """mcp.client.streamable_http hace logger.exception("Error in post_writer")."""
    url, token = mcp_url
    zoho_actions.instalar_filtro_url_mcp()
    zoho_actions.instalar_filtro_url_mcp()  # idempotente
    log = logging.getLogger("mcp.client.streamable_http")
    assert sum(isinstance(f, zoho_actions.FiltroUrlMcp) for f in log.filters) == 1

    caplog.set_level(logging.ERROR, logger="mcp.client.streamable_http")
    try:
        raise _http_400(url)
    except httpx.HTTPStatusError:
        log.exception("Error in post_writer")

    assert "Error in post_writer" in caplog.text
    assert "HTTPStatusError" in caplog.text  # el traceback sigue, redactado
    assert token not in caplog.text


async def test_pipeline_no_registra_datos_del_estudiante(agent_runner, sample_raw_ticket, caplog):
    """PIPELINE_STEP_START registraba 220 caracteres de la entrada y RECEPTOR_OK
    los primeros 6 dígitos del documento."""
    caplog.set_level(logging.INFO, logger=PIPELINE_LOGGER)

    await agent_runner(build_agente_receptor(), {}, message=json.dumps(sample_raw_ticket))

    assert "PIPELINE_STEP_START" in caplog.text
    assert "input_chars=" in caplog.text
    assert "RECEPTOR_OK" in caplog.text
    assert f"doc_len={len(sample_raw_ticket['cf_numero_de_documento'])}" in caplog.text
    # Prefijo de 6 dígitos (lo que se registraba antes), no uno más corto: el
    # invocation_id es un uuid aleatorio y podría contener 4 dígitos seguidos.
    for dato in (
        sample_raw_ticket["cf_numero_de_documento"][:6],
        sample_raw_ticket["email"],
        sample_raw_ticket["contact"]["lastName"],
        sample_raw_ticket["subject"],
    ):
        assert dato not in caplog.text
