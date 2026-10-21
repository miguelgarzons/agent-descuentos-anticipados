"""Idempotencia por ticket_id con Firestore.

El pipeline se dispara vía Pub/Sub (`/trigger/pubsub`), cuya entrega es
*at-least-once*: un mismo ticket puede llegar más de una vez (reintentos por
timeout de ack, reentregas de la suscripción, etc.). Como el pipeline tiene
efectos externos NO idempotentes (crea la solicitud en el sistema financiero y
envía correos/comentarios al estudiante), necesitamos garantizar un único
procesamiento por ticket.

Estrategia: un documento por `ticket_id` en Firestore que actúa como lock.
La reclamación es atómica (transacción):

    - no existe                       → se reclama  ("acquired")
    - existe y status == "done"       → duplicado   ("duplicate")
    - existe, "processing" y fresco   → duplicado   ("duplicate")
    - existe, "processing" y stale    → se re-reclama ("acquired")  [recuperación de crash]
    - existe y status == "error"      → se re-reclama ("acquired")

Diseño defensivo (fail-open): si la idempotencia está desactivada o Firestore no
está disponible, se devuelve "disabled" y el pipeline continúa. Preferimos un
posible duplicado (raro) antes que bloquear el procesamiento de tickets legítimos
por una caída de Firestore.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

logger = logging.getLogger("adk.pipeline.descuentos_anticipados")

# Estados posibles del lock.
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_ERROR = "error"

# Resultados de claim().
ACQUIRED = "acquired"
DUPLICATE = "duplicate"
DISABLED = "disabled"

_client = None  # cache del cliente Firestore


def _enabled() -> bool:
    return os.getenv("IDEMPOTENCY_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def _collection() -> str:
    return os.getenv("IDEMPOTENCY_COLLECTION", "descuentos_anticipados_idempotency").strip()


def _project() -> str:
    return (os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT") or "").strip()


def _database() -> str:
    """Base Firestore con nombre. Vacío = base '(default)'.

    En este proyecto no existe '(default)'; se usa una base por entorno
    (p. ej. categorizador-dev / categorizador-prod), así que hay que fijarla.
    """
    return os.getenv("FIRESTORE_DATABASE", "").strip()


def _lock_ttl_seconds() -> int:
    """Un lock 'processing' más viejo que esto se considera stale (crash previo)."""
    try:
        return int(os.getenv("IDEMPOTENCY_LOCK_TTL_SECONDS", "900"))
    except ValueError:
        return 900


def _get_client():
    global _client
    if _client is None:
        from google.cloud import firestore  # import perezoso: dep opcional en dev/tests

        kwargs: dict = {}
        if _project():
            kwargs["project"] = _project()
        if _database():
            kwargs["database"] = _database()
        _client = firestore.Client(**kwargs)
    return _client


def _doc_ref(ticket_id: str):
    return _get_client().collection(_collection()).document(str(ticket_id))


def _claim_sync(ticket_id: str) -> str:
    from google.cloud import firestore

    client = _get_client()
    ref = client.collection(_collection()).document(str(ticket_id))
    ttl = _lock_ttl_seconds()

    @firestore.transactional
    def _txn(transaction) -> str:
        snap = ref.get(transaction=transaction)
        now = time.time()
        if snap.exists:
            data = snap.to_dict() or {}
            status = data.get("status")
            started = float(data.get("startedAt", 0) or 0)
            if status == STATUS_DONE:
                return DUPLICATE
            if status == STATUS_PROCESSING and (now - started) < ttl:
                return DUPLICATE
            # stale 'processing' o 'error' → recuperable, se re-reclama
        transaction.set(
            ref,
            {
                "status": STATUS_PROCESSING,
                "startedAt": now,
                "attempts": firestore.Increment(1),
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        return ACQUIRED

    return _txn(client.transaction())


def _mark_sync(ticket_id: str, status: str) -> None:
    from google.cloud import firestore

    _doc_ref(ticket_id).set(
        {"status": status, "finishedAt": time.time(), "updatedAt": firestore.SERVER_TIMESTAMP},
        merge=True,
    )


async def claim(ticket_id: str) -> str:
    """Reclama el procesamiento del ticket. Devuelve ACQUIRED, DUPLICATE o DISABLED.

    Nunca lanza excepción: ante cualquier fallo de Firestore hace fail-open (DISABLED).
    """
    if not _enabled() or not ticket_id:
        return DISABLED
    try:
        result = await asyncio.to_thread(_claim_sync, ticket_id)
        logger.info("IDEMPOTENCY_CLAIM ticket=%s → %s", ticket_id, result)
        return result
    except Exception as exc:  # noqa: BLE001 — fail-open ante caída de Firestore
        logger.error("IDEMPOTENCY_UNAVAILABLE fail-open ticket=%s: %s", ticket_id, exc)
        return DISABLED


async def mark_done(ticket_id: str) -> None:
    """Marca el ticket como procesado (no vuelve a procesarse en reentregas)."""
    if not _enabled() or not ticket_id:
        return
    try:
        await asyncio.to_thread(_mark_sync, ticket_id, STATUS_DONE)
        logger.info("IDEMPOTENCY_DONE ticket=%s", ticket_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("IDEMPOTENCY_MARK_DONE_ERR ticket=%s: %s", ticket_id, exc)


async def mark_failed(ticket_id: str) -> None:
    """Marca el ticket como fallido para que una reentrega pueda reprocesarlo ya
    (sin esperar al TTL del lock stale)."""
    if not _enabled() or not ticket_id:
        return
    try:
        await asyncio.to_thread(_mark_sync, ticket_id, STATUS_ERROR)
        logger.info("IDEMPOTENCY_FAILED ticket=%s", ticket_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("IDEMPOTENCY_MARK_FAILED_ERR ticket=%s: %s", ticket_id, exc)
