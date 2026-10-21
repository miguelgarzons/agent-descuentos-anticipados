"""Contrato compartido del pipeline de descuentos anticipados.

`StateKeys` es el vocabulario con el que se comunican los subagentes a través de
`ctx.session.state`. Cada clave la escribe exactamente un subagente y la leen los
siguientes. Hoy el pipeline es solo cascarón de Zoho (receptor → emisor); las
claves del negocio (clasificación, validación, ejecución) se suman aquí a medida
que se agreguen los subagentes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from google.adk.models import Gemini
from pydantic_settings import BaseSettings


class StateKeys:
    # ── Entrada ──
    RAW_TICKET = "raw_ticket"
    TICKET = "ticket"

    # ── Zoho ──
    ZOHO_COMMENT = "zoho_comment"
    ZOHO_ASSIGNEE = "zoho_assignee"

    # ── Control de flujo ──
    REVISION_MANUAL = "revision_manual"
    SKIP_IDEMPOTENTE = "skip_idempotente"

    # ── Errores ──
    ERROR = "error"                  # mensaje de negocio
    ERROR_DETALLE = "error_detalle"  # diagnóstico técnico, solo comentario interno


class Settings(BaseSettings):
    agent_model: str = "gemini-2.5-pro"
    gemini_api_key: str = ""

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


_VALORES_ENCENDIDO = frozenset({"1", "true", "si", "sí", "yes", "on"})


def opt_in_habilitado(nombre: str) -> bool:
    """Interruptor que arranca APAGADO: solo un valor afirmativo lo enciende.

    La variable vacía cuenta como apagada a propósito: el deploy pasa
    `VAR=${EV_VAR}` aunque la variable no exista en GitHub, así que en Cloud Run
    llega presente y vacía.
    """
    return os.getenv(nombre, "").strip().lower() in _VALORES_ENCENDIDO


def _to_float(valor: Any) -> float:
    """Número tolerante: '505137,50' / '505137.50' / 505137.5 → float. 0.0 si no."""
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = str(valor or "").strip().replace(" ", "")
    if not texto:
        return 0.0
    # Formato colombiano: 1.234.567,89 → 1234567.89
    if "," in texto and "." in texto:
        texto = texto.replace(".", "").replace(",", ".")
    elif "," in texto:
        texto = texto.replace(",", ".")
    try:
        return float(texto)
    except (TypeError, ValueError):
        return 0.0


def formato_pesos(valor: Any) -> str:
    """Valor en pesos legible para el estudiante: 505137.5 → '$505.137,50'."""
    monto = _to_float(valor)
    entero, decimales = f"{monto:,.2f}".split(".")
    return f"${entero.replace(',', '.')},{decimales}"


@dataclass(frozen=True)
class AgentDependencies:
    model: Any


def build_dependencies() -> AgentDependencies:
    model_name = os.getenv("AGENT_MODEL", "gemini-2.5-pro")
    if model_name.startswith("gemini/"):
        model_name = model_name.split("/", 1)[1]
    return AgentDependencies(model=Gemini(model=model_name))
