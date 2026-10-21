#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════
# SA de deploy del agente Descuentos Anticipados
# ══════════════════════════════════════════════════════════════
#
# Replica el patrón de los agentes hermanos (saldos-a-favor@, descuento-convenios@,
# gestion-arl@, ...): una SA por agente, con exactamente los mismos 5 roles, cuya
# llave JSON va en el secret GCP_SA_KEY del repo. El servicio de Cloud Run NO
# corre con esta SA sino con la de compute por defecto (el deploy no fija
# --service-account); esa ya tiene roles/datastore.user para el lock de
# idempotencia en Firestore.
#
# Este agente no está conectado al categorizador: aquí no se crea topic ni
# suscripción Pub/Sub.
#
# Idempotente. Se corre UNA vez, a mano, con un usuario que pueda administrar
# IAM — no desde el CI.
#
# Requiere: `gcloud auth login` vigente y, para CREAR_LLAVE=1, `gh` autenticado
# con permiso de admin sobre el repo.
#
# Uso:
#   bash infra/setup_service_account.sh                 # SA + roles + repo AR de dev
#   CREAR_LLAVE=1 bash infra/setup_service_account.sh   # además sube GCP_SA_KEY
#                                                       # y GCP_PROJECT_ID al repo
#   ENTORNOS="dev prod" bash infra/setup_service_account.sh   # al preparar prod
# ══════════════════════════════════════════════════════════════

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-it-fab-soft-sandbox-t}"
REGION="${REGION:-us-central1}"
SA_NAME="descuentos-anticipados"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
REPO_GH="${REPO_GH:-miguelgarzons/agent-descuentos-anticipados}"
ROLES=(
  roles/run.admin
  roles/artifactregistry.writer
  roles/iam.serviceAccountUser
  roles/cloudbuild.builds.editor
  roles/datastore.user
)

# ── 1. Service account ───────────────────────────────────────
if gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1; then
  echo "✔ SA ya existe: $SA_EMAIL"
else
  gcloud iam service-accounts create "$SA_NAME" \
    --project "$PROJECT_ID" \
    --display-name "Descuentos Anticipados" \
    --description "CI/CD del agente ADK Descuentos Anticipados"
  for _ in $(seq 1 15); do
    gcloud iam service-accounts describe "$SA_EMAIL" --project "$PROJECT_ID" >/dev/null 2>&1 && break
    sleep 2
  done
  echo "✔ SA creada: $SA_EMAIL"
fi

# ── 2. Roles ─────────────────────────────────────────────────
for ROLE in "${ROLES[@]}"; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member "serviceAccount:${SA_EMAIL}" \
    --role "$ROLE" \
    --condition=None \
    --format=none
  echo "✔ $ROLE"
done

# ── 3. Repositorios de Artifact Registry ─────────────────────
# Deben existir antes del primer push: el CI no los crea. Por ahora solo dev;
# el de prod se crea al preparar prod (ENTORNOS="dev prod").
for ENV in ${ENTORNOS:-dev}; do
  REPO="adk-descuentos-anticipados-${ENV}"
  if gcloud artifacts repositories describe "$REPO" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
    echo "✔ Artifact Registry ya existe: $REPO"
  else
    gcloud artifacts repositories create "$REPO" \
      --repository-format docker \
      --location "$REGION" \
      --project "$PROJECT_ID" \
      --description "Imágenes del agente ADK Descuentos Anticipados (${ENV})"
    echo "✔ Artifact Registry creado: $REPO"
  fi
done

# ── 4. Llave → secret GCP_SA_KEY y GCP_PROJECT_ID (opcional) ─
if [ "${CREAR_LLAVE:-0}" = "1" ]; then
  # Archivo temporal y no pipe: si gcloud falla, `gh secret set` subiría un
  # GCP_SA_KEY vacío y rompería el deploy.
  TMP_KEY="$(mktemp)"
  trap 'rm -f "$TMP_KEY"' EXIT
  gcloud iam service-accounts keys create "$TMP_KEY" --iam-account "$SA_EMAIL"
  [ -s "$TMP_KEY" ] || { echo "❌ gcloud no generó la llave" >&2; exit 1; }
  KEY_ID="$(sed -n 's/.*"private_key_id": *"\([^"]*\)".*/\1/p' "$TMP_KEY")"
  if ! gh secret set GCP_SA_KEY -R "$REPO_GH" < "$TMP_KEY"; then
    gcloud iam service-accounts keys delete "$KEY_ID" --iam-account "$SA_EMAIL" --quiet
    echo "❌ No se pudo subir GCP_SA_KEY; la llave $KEY_ID se eliminó." >&2
    exit 1
  fi
  echo "✔ GCP_SA_KEY actualizado en $REPO_GH (llave $KEY_ID)"

  gh secret set GCP_PROJECT_ID -R "$REPO_GH" --body "$PROJECT_ID"
  echo "✔ GCP_PROJECT_ID actualizado en $REPO_GH"
else
  echo ""
  echo "Falta la llave. Para generarla y subirla al repo (usa un archivo temporal que se borra al terminar):"
  echo "  CREAR_LLAVE=1 bash infra/setup_service_account.sh"
fi
