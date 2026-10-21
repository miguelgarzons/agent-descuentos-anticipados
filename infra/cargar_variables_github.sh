#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
# Carga la configuración de un environment de GitHub (develop | main) desde un
# archivo LOCAL con líneas KEY=valor (p. ej. infra/variables.develop.env, que git
# ignora). Crea el environment si no existe. Nunca imprime los valores.
#
# Las credenciales (GEMINI_API_KEY, ZOHO_MCP_URL, ZOHO_TOKEN_WEBHOOK_USER,
# ZOHO_TOKEN_WEBHOOK_PASS) se guardan como SECRETS del environment (GitHub las
# enmascara en el log del deploy, que en este repo es público); el resto como
# Variables. Una capa de comillas que envuelva el valor se quita.
#
# Solo acepta los nombres que usa .github/workflows/deploy.yml; las líneas vacías
# o con valor vacío no se cargan.
#
# Uso:
#   cp infra/variables.develop.example infra/variables.develop.env   # y completar
#   bash infra/cargar_variables_github.sh develop infra/variables.develop.env
#
# Requiere `gh` autenticado con permiso de admin sobre el repo.
# ══════════════════════════════════════════════════════════════

set -euo pipefail

ENTORNO="${1:?uso: $0 <develop|main> <archivo KEY=valor>}"
ARCHIVO="${2:?uso: $0 <develop|main> <archivo KEY=valor>}"
REPO_GH="${REPO_GH:-miguelgarzons/agent-descuentos-anticipados}"

case "$ENTORNO" in
  develop|main) ;;
  *) echo "❌ Entorno inválido: '$ENTORNO' (develop o main)." >&2; exit 1 ;;
esac
[ -f "$ARCHIVO" ] || { echo "❌ No existe el archivo $ARCHIVO" >&2; exit 1; }

# Las mismas que lee el deploy (paso "Deploy to Cloud Run").
PERMITIDAS=" AGENT_MODEL GEMINI_API_KEY ZOHO_MCP_URL ZOHO_ORG_ID ZOHO_TOKEN_WEBHOOK_URL ZOHO_TOKEN_WEBHOOK_USER ZOHO_TOKEN_WEBHOOK_PASS ZOHO_DESK_API_BASE_SANDBOX ZOHO_ASSIGNEE_AGENT_ID ASSETS_FAB WATERMARK_PRYMARY_MAME WATERMARK_PRYMARY_AREA WATERMARK_PRYMARY_VICE WATERMARK_WEB "
# De esas, las que van como Secrets (el workflow las lee de secrets.*).
SECRETOS=" GEMINI_API_KEY ZOHO_MCP_URL ZOHO_TOKEN_WEBHOOK_USER ZOHO_TOKEN_WEBHOOK_PASS "

# Crea el environment si no existe (idempotente).
gh api -X PUT "repos/${REPO_GH}/environments/${ENTORNO}" >/dev/null
echo "✔ Environment '${ENTORNO}' listo en ${REPO_GH}"

CARGADAS=0
IGNORADAS=0
while IFS= read -r LINEA || [ -n "$LINEA" ]; do
  LINEA="${LINEA%$'\r'}"
  [[ -z "${LINEA//[[:space:]]/}" || "$LINEA" =~ ^[[:space:]]*# ]] && continue
  [[ "$LINEA" == *=* ]] || { echo "⚠️  Línea sin '=' ignorada"; IGNORADAS=$((IGNORADAS + 1)); continue; }

  NOMBRE="$(printf '%s' "${LINEA%%=*}" | tr -d '[:space:]')"
  VALOR="${LINEA#*=}"
  # Como un .env: una capa de comillas que envuelva el valor no es parte de él.
  case "$VALOR" in
    \"*\"|\'*\') [ "${#VALOR}" -ge 2 ] && VALOR="${VALOR:1:${#VALOR}-2}" ;;
  esac

  if [[ "$PERMITIDAS" != *" ${NOMBRE} "* ]]; then
    echo "⚠️  ${NOMBRE}: el workflow no la usa, se ignora"
    IGNORADAS=$((IGNORADAS + 1))
    continue
  fi
  if [ -z "$(printf '%s' "$VALOR" | tr -d '[:space:]')" ]; then
    echo "·  ${NOMBRE}: vacía, no se carga"
    continue
  fi

  # Por stdin (no como argumento) para que el valor no quede en la lista de procesos.
  if [[ "$SECRETOS" == *" ${NOMBRE} "* ]]; then
    printf '%s' "$VALOR" | gh secret set "$NOMBRE" --env "$ENTORNO" --repo "$REPO_GH" >/dev/null
    echo "✔ ${NOMBRE} (secret)"
  else
    printf '%s' "$VALOR" | gh variable set "$NOMBRE" --env "$ENTORNO" --repo "$REPO_GH" >/dev/null
    echo "✔ ${NOMBRE}"
  fi
  CARGADAS=$((CARGADAS + 1))
done < "$ARCHIVO"

echo ""
echo "Listo: ${CARGADAS} valores cargados en '${ENTORNO}' (${IGNORADAS} ignorados)."
