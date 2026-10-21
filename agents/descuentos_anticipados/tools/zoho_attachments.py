"""Descarga de adjuntos del ticket desde Zoho Desk (REST + token por webhook).

Patrón tomado de agent-apoyo-economico (implementación de referencia del stack):
el token OAuth se obtiene de un webhook n8n con basic auth
(ZOHO_TOKEN_WEBHOOK_URL/USER/PASS) y los adjuntos se recorren por los threads
del ticket contra ZOHO_DESK_API_BASE_SANDBOX.

Uso en este agente: descargar los soportes del ticket para leerlos.
Todo es best-effort: si falta configuración o Zoho falla, se retorna lista
vacía (no bloquea el pipeline).
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("adk.pipeline.descuentos_anticipados")

_cached_token: str | None = None

_MAX_PDFS = 5


def _clean_env(name: str) -> str:
    """Lee una env var saneando espacios y comillas envolventes (config manual)."""
    value = os.getenv(name, "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1].strip()
    return value


def _normalize_url(url: str) -> str:
    if url and not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return url.rstrip("/")


def is_configured() -> bool:
    return bool(
        _clean_env("ZOHO_TOKEN_WEBHOOK_URL")
        and _clean_env("ZOHO_TOKEN_WEBHOOK_USER")
        and _clean_env("ZOHO_TOKEN_WEBHOOK_PASS")
        and _clean_env("ZOHO_DESK_API_BASE_SANDBOX")
    )


async def _get_token() -> str:
    """Token OAuth de Zoho Desk desde el webhook n8n (cacheado en memoria)."""
    global _cached_token
    if _cached_token:
        return _cached_token

    url = _normalize_url(_clean_env("ZOHO_TOKEN_WEBHOOK_URL"))
    user = _clean_env("ZOHO_TOKEN_WEBHOOK_USER")
    password = _clean_env("ZOHO_TOKEN_WEBHOOK_PASS")
    if not (url and user and password):
        raise RuntimeError("ZOHO_TOKEN_WEBHOOK_URL/USER/PASS no configurados")

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, auth=(user, password), timeout=15.0)
        resp.raise_for_status()
        data = resp.json()

    token = (
        data.get("access_token")
        or data.get("token")
        or data.get("accessToken")
        or data.get("zoho_token")
    )
    if not token:
        raise RuntimeError(f"Webhook de token sin token. Keys: {list(data)}")
    _cached_token = token
    return token


def invalidate_token() -> None:
    global _cached_token
    _cached_token = None


def _headers(token: str, org_id: str) -> dict:
    return {"orgId": org_id, "Authorization": f"Zoho-oauthtoken {token}"}


async def list_ticket_attachments(ticket_id: str, org_id: str = "") -> list[dict]:
    """Lista los adjuntos del ticket (nombre, url, size) SIN descargarlos.

    Recorre los threads del ticket. Sirve para validar que el estudiante cargó
    los documentos de soporte antes de procesar la solicitud.
    """
    if not ticket_id or not is_configured():
        return []

    base = _normalize_url(_clean_env("ZOHO_DESK_API_BASE_SANDBOX"))
    org = str(org_id or _clean_env("ZOHO_ORG_ID"))
    token = await _get_token()
    headers = _headers(token, org)

    adjuntos: list[dict] = []
    async with httpx.AsyncClient() as client:
        threads_url = f"{base}/tickets/{ticket_id}/threads"
        resp = await client.get(threads_url, headers=headers, timeout=15.0)
        if resp.status_code == 401:
            logger.warning("ZOHO_ATTACH token expirado — reautenticando")
            invalidate_token()
            token = await _get_token()
            headers = _headers(token, org)
            resp = await client.get(threads_url, headers=headers, timeout=15.0)
        resp.raise_for_status()

        raw = resp.json()
        threads = raw.get("data") if isinstance(raw, dict) else None
        if not isinstance(threads, list):
            threads = []

        for thread in threads:
            detail_url = f"{base}/tickets/{ticket_id}/threads/{thread.get('id')}"
            detail_resp = await client.get(detail_url, headers=headers, timeout=15.0)
            if not detail_resp.is_success:
                logger.warning("ZOHO_ATTACH thread %s → %s", thread.get("id"), detail_resp.status_code)
                continue

            for att in detail_resp.json().get("attachments", []):
                name = str(att.get("name", "") or "")
                url = att.get("href") or att.get("downloadUrl") or att.get("url")
                if name and url:
                    adjuntos.append({"name": name, "url": url, "size": att.get("size")})

    logger.info("ZOHO_ATTACH adjuntos=%d ticket=%s", len(adjuntos), ticket_id)
    return adjuntos


# Formatos que Gemini puede leer para extraer datos de los documentos.
_DOC_EXTS = {"pdf", "png", "jpg", "jpeg", "webp"}


async def download_ticket_documents(
    ticket_id: str,
    org_id: str = "",
    limit: int = _MAX_PDFS,
    exts: set[str] | None = None,
) -> list[tuple[str, bytes]]:
    """Descarga los adjuntos del ticket cuyo formato esté en `exts`.

    Retorna [(nombre, contenido)], máximo `limit` archivos.
    """
    permitidas = {e.lower() for e in (exts or _DOC_EXTS)}
    adjuntos = await list_ticket_attachments(ticket_id, org_id)
    if not adjuntos:
        return []

    org = str(org_id or _clean_env("ZOHO_ORG_ID"))
    token = await _get_token()
    headers = _headers(token, org)

    archivos: list[tuple[str, bytes]] = []
    async with httpx.AsyncClient() as client:
        for att in adjuntos:
            if len(archivos) >= limit:
                break
            name = att["name"]
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext not in permitidas:
                continue
            try:
                file_resp = await client.get(att["url"], headers=headers, timeout=30.0, follow_redirects=True)
                file_resp.raise_for_status()
                archivos.append((name, file_resp.content))
                logger.info("ZOHO_ATTACH descargado %s (%d bytes)", name, len(file_resp.content))
            except Exception as exc:  # noqa: BLE001 — un adjunto fallido no bloquea los demás
                logger.warning("ZOHO_ATTACH fallo descargando %s: %s", name, exc)

    logger.info("ZOHO_ATTACH archivos=%d ticket=%s", len(archivos), ticket_id)
    return archivos


async def download_ticket_pdfs(
    ticket_id: str,
    org_id: str = "",
    limit: int = _MAX_PDFS,
) -> list[tuple[str, bytes]]:
    """Descarga solo los PDF (la API financiera únicamente acepta .pdf en el save)."""
    return await download_ticket_documents(ticket_id, org_id, limit, exts={"pdf"})
