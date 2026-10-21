# ══════════════════════════════════════════════════════════════
# Descuentos Anticipados — CUN ADK Agent — imagen multi-stage
# ══════════════════════════════════════════════════════════════
#
# Paquete del agente: agents/descuentos_anticipados/  (ADK descubre el root_agent
# recorriendo /app/agents, por eso el WORKDIR del runtime es esa carpeta).
#
# Stages:
#   base    → python 3.11 slim + usuario sin privilegios `adk`
#   deps    → virtualenv con requirements.txt (capa cacheable, se invalida
#             solo cuando cambia requirements.txt)
#   dev     → `adk web` en :8000; el código se monta por volumen (docker-compose)
#   runtime → `adk api_server` en :8080; es la imagen que se publica a Cloud Run
#
# ══════════════════════════════════════════════════════════════

FROM python:3.11-slim-bookworm AS base

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:${PATH}"

# El proceso nunca corre como root: Cloud Run no lo exige, pero limita el daño
# de cualquier ejecución inesperada dentro del contenedor.
RUN groupadd --system adk \
    && useradd --system --create-home --gid adk --shell /usr/sbin/nologin adk

FROM base AS deps

# Se instala en un venv propio (/opt/venv) y se limpian pycache/tests/docs:
# la imagen final solo copia el venv ya depurado, sin toolchain de build.
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install --no-compile -r /tmp/requirements.txt \
    && find /opt/venv -type d -name __pycache__ -prune -exec rm -rf {} + \
    && find /opt/venv -type f -name "*.pyc" -delete \
    && find /opt/venv/lib -type d \( -name tests -o -name test -o -name docs -o -name doc \) -prune -exec rm -rf {} +

FROM base AS dev

COPY --from=deps /opt/venv /opt/venv
# No se copia el código: en dev llega por bind mount (ver docker-compose.yml),
# así se editan los subagentes sin reconstruir la imagen.
RUN mkdir -p /app/agents \
    && chown -R adk:adk /app /opt/venv

USER adk

EXPOSE 8000 8001 8080

CMD ["adk", "web", ".", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS runtime

COPY --from=deps /opt/venv /opt/venv
COPY --chown=adk:adk agents /app/agents

WORKDIR /app/agents

USER adk

# Cloud Run requiere este puerto
ENV PORT=8080

# Concurrencia de invocaciones del trigger por instancia (semaforo interno de ADK).
ENV ADK_TRIGGER_MAX_CONCURRENT=10

EXPOSE 8080

# --trigger_sources "pubsub" registra POST /apps/{app}/trigger/pubsub:
# recibe el push de Pub/Sub, decodifica el base64, auto-crea la sesion y corre
# el pipeline. Devuelve 200 (ack) u 500 (Pub/Sub reintenta segun retry policy).
#
# Cloud Run requiere --host 0.0.0.0 y el puerto de $PORT.
CMD ["sh", "-c", "adk api_server . --host 0.0.0.0 --port ${PORT} --trigger_sources pubsub"]
