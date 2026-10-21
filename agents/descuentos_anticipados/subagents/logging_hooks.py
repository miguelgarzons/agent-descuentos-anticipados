from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("adk.pipeline.descuentos_anticipados")


def _input_chars(user_content: Any) -> int:
    """Largo del texto de entrada. Solo el largo: la entrada es el ticket de Zoho
    (correo, cédula, nombre, descripción) y no debe llegar a Cloud Logging."""
    parts = getattr(user_content, "parts", None) or []
    return sum(len(p.text) for p in parts if isinstance(getattr(p, "text", None), str))


def log_before_agent(callback_context: Any = None, **_: Any):
    ctx = callback_context
    if ctx is None:
        return None
    session_id = getattr(getattr(ctx, "session", None), "id", "")
    logger.info(
        "PIPELINE_STEP_START agent=%s session_id=%s invocation_id=%s input_chars=%d",
        getattr(ctx, "agent_name", ""),
        session_id,
        getattr(ctx, "invocation_id", ""),
        _input_chars(getattr(ctx, "user_content", None)),
    )
    return None


def log_after_agent(callback_context: Any = None, **_: Any):
    ctx = callback_context
    if ctx is None:
        return None
    session_id = getattr(getattr(ctx, "session", None), "id", "")
    # El State de CallbackContext no expone keys(); se cuenta vía to_dict().
    state = getattr(ctx, "state", None)
    if state is None:
        state = getattr(getattr(ctx, "session", None), "state", None)
    try:
        if callable(getattr(state, "to_dict", None)):
            state_size = len(state.to_dict())
        elif isinstance(state, dict):
            state_size = len(state)
        else:
            state_size = 0
    except Exception:
        state_size = 0
    logger.info(
        "PIPELINE_STEP_END agent=%s session_id=%s state_keys=%s",
        getattr(ctx, "agent_name", ""),
        session_id,
        state_size,
    )
    return None
