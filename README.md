# agent-descuentos-anticipados

Agente ADK de CUN para gestionar solicitudes de **descuentos anticipados** que llegan como tickets de Zoho Desk. Misma arquitectura que `agent-saldos-a-favor` y `agent-descuento-convenios`: pipeline secuencial desplegado en Cloud Run.

> **No está conectado al categorizador.** No existe topic ni suscripción Pub/Sub que le entregue tickets. El endpoint `/apps/descuentos_anticipados/trigger/pubsub` queda disponible, y también `/run`, por si se conecta más adelante.

> **Estado: cascarón.** El flujo de negocio todavía no está definido. Hoy el agente solo recibe el ticket, lo comenta (nota interna) y lo asigna a un asesor humano. Los subagentes de negocio se agregan entre receptor y emisor.

## Pipeline actual

```
Pub/Sub → /trigger/pubsub → receptor → emisor
```

| Subagente | Qué hace |
|---|---|
| `receptor` | Parsea el ticket (dict, JSON o repr; desenvuelve el sobre de Pub/Sub), valida campos mínimos, normaliza a `state["ticket"]` y reclama la idempotencia en Firestore. |
| `emisor` | Comentario interno con el resumen, reasignación al agente de `ZOHO_ASSIGNEE_AGENT_ID` y `mark_done` de la idempotencia. Si el comentario falla, libera el lock y propaga para que Pub/Sub reintente. |

## Tools (`agents/descuentos_anticipados/tools/`)

- `zoho_actions.py` — Zoho Desk vía MCP: comentar, responder (`sendReply`), asignar, cerrar, leer contacto.
- `zoho_attachments.py` — listar y descargar adjuntos del ticket (REST + token por webhook).
- `idempotency.py` — lock por `ticket_id` en Firestore (fail-open).

Pendientes de portar cuando haga falta: `doc_reader` (lectura de soportes con Gemini visión), `api_auth` (si hay API financiera), plantillas de respuesta y `respondedor`. Referencia: `agent-saldos-a-favor`.

## Estructura

```
agents/descuentos_anticipados/
  agent.py              root_agent (lo descubre ADK)
  subagents/            common (StateKeys), receptor, emisor, orchestrator, logging_hooks
  tools/                zoho_actions, zoho_attachments, idempotency
  templates/            solicitud_reply_email.html (envoltorio del correo, aún sin usar)
tests/
```

## Desarrollo

```bash
cp .env.example .env     # completar valores
pip install -r requirements.txt pytest pytest-asyncio pytest-cov
python -m pytest
```

```bash
docker-compose up --build descuentos-anticipados-dev    # adk web en :8000
docker-compose up --build descuentos-anticipados-api    # adk api_server en :8080
```

`ZOHO_MCP_URL` solo funciona desde el runtime autorizado (Cloud Run); desde una máquina local el MCP responde `Invalid server domain`.

## Deploy

`.github/workflows/deploy.yml`: `develop` → servicio `adk-descuentos-anticipados-dev`, `main` → `-prod`. Corre gates de seguridad (gitleaks, `.env`, pip-audit), tests con cobertura, valida la configuración requerida, `gcloud run deploy` y comprueba con `/list-apps?detailed=true` que el agente carga.

La configuración sale del environment de GitHub (`develop` o `main`). Las credenciales (`GEMINI_API_KEY`, `ZOHO_MCP_URL`, `ZOHO_TOKEN_WEBHOOK_USER`, `ZOHO_TOKEN_WEBHOOK_PASS`) van como **Secrets**, porque el repo es público y el log del deploy imprime las Variables sin enmascarar. El resto va como Variables.

Preparación, una sola vez (ver `infra/`):

```bash
gcloud auth login
CREAR_LLAVE=1 bash infra/setup_service_account.sh     # SA descuentos-anticipados@ + 5 roles + AR dev + GCP_SA_KEY
cp infra/variables.develop.example infra/variables.develop.env   # completar
bash infra/cargar_variables_github.sh develop infra/variables.develop.env
```

Después, un push a `develop` despliega dev. Para prod: `ENTORNOS="dev prod" bash infra/setup_service_account.sh`, cargar el environment `main` con valores de prod y hacer push a `main`.
