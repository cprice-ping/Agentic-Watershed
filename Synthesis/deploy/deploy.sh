#!/usr/bin/env bash
# deploy.sh — provision Azure infrastructure and deploy the Synthesis agent
#             as an Azure Container Apps Job with a cron schedule.
#
# What this creates:
#   - Azure Container Registry (ACR) — stores the container image
#   - Azure Storage Account + File Share — persistent /data volume
#   - Azure Container Apps Environment — the execution context
#   - Azure Container Apps Job — cron-triggered, run-to-completion
#
# Usage:
#   export ANTHROPIC_API_KEY=sk-ant-...
#   export BSKY_SYNTH_HANDLE=napasynth01.bsky.social
#   export BSKY_SYNTH_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
#   ./deploy.sh
#
# To re-deploy after a code change (image only, no infrastructure reprovisioning):
#   ./deploy.sh --image-only
#
# Prerequisites:
#   - Azure CLI installed and logged in: az login
#   - az containerapp extension: az extension add --name containerapp
#   - Docker (or Azure CLI can build via 'az acr build' without local Docker)

set -euo pipefail

# ── Configuration — override with environment variables if needed ──────────

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-agentic-watershed}"
LOCATION="${LOCATION:-westus2}"

# Storage account name must be 3-24 chars, lowercase alphanumeric only, globally unique.
STORAGE_ACCOUNT="${STORAGE_ACCOUNT:-agwsynthdata}"
FILE_SHARE="${FILE_SHARE:-synthesis-data}"

# ACR name must be 5-50 chars, alphanumeric, globally unique.
ACR_NAME="${ACR_NAME:-agenticwatershed}"
IMAGE_NAME="${IMAGE_NAME:-synthesis}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

ENVIRONMENT="${ENVIRONMENT:-cae-agentic-watershed}"
JOB_NAME="${JOB_NAME:-synthesis-agent}"

# Cron schedule (UTC). Twice daily at 06:00 and 18:00.
CRON_EXPRESSION="${CRON_EXPRESSION:-0 6,18 * * *}"

# Maximum seconds a single pipeline run may take before being killed.
REPLICA_TIMEOUT="${REPLICA_TIMEOUT:-600}"

# ── Validate required secrets ──────────────────────────────────────────────

: "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY before running deploy.sh}"
: "${BSKY_SYNTH_HANDLE:?Set BSKY_SYNTH_HANDLE before running deploy.sh}"
: "${BSKY_SYNTH_APP_PASSWORD:?Set BSKY_SYNTH_APP_PASSWORD before running deploy.sh}"

# ── Parse flags ────────────────────────────────────────────────────────────

IMAGE_ONLY=false
for arg in "$@"; do
  case "$arg" in
    --image-only) IMAGE_ONLY=true ;;
    *) echo "Unknown argument: $arg" && exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNTHESIS_DIR="$(dirname "${SCRIPT_DIR}")"

# ── Ensure required CLI extensions ────────────────────────────────────────

echo "Checking Azure CLI extensions..."
az extension add --name containerapp --upgrade --only-show-errors 2>/dev/null || true

# ==========================================================================
# INFRASTRUCTURE (skipped with --image-only)
# ==========================================================================

if [ "$IMAGE_ONLY" = false ]; then

  # ── Resource group ────────────────────────────────────────────────────────
  echo ""
  echo "[1/6] Resource group: ${RESOURCE_GROUP}"
  az group create \
    --name "${RESOURCE_GROUP}" \
    --location "${LOCATION}" \
    --output none

  # ── Azure Container Registry ──────────────────────────────────────────────
  echo ""
  echo "[2/6] Azure Container Registry: ${ACR_NAME}"
  az acr create \
    --resource-group "${RESOURCE_GROUP}" \
    --name "${ACR_NAME}" \
    --sku Basic \
    --output none

  # ── Storage account and file share ────────────────────────────────────────
  echo ""
  echo "[3/6] Storage account: ${STORAGE_ACCOUNT}"
  az storage account create \
    --resource-group "${RESOURCE_GROUP}" \
    --name "${STORAGE_ACCOUNT}" \
    --location "${LOCATION}" \
    --sku Standard_LRS \
    --kind StorageV2 \
    --output none

  STORAGE_KEY=$(az storage account keys list \
    --resource-group "${RESOURCE_GROUP}" \
    --account-name "${STORAGE_ACCOUNT}" \
    --query "[0].value" \
    --output tsv)

  echo "  Creating file share: ${FILE_SHARE}"
  az storage share create \
    --account-name "${STORAGE_ACCOUNT}" \
    --account-key "${STORAGE_KEY}" \
    --name "${FILE_SHARE}" \
    --output none

  # ── Container Apps environment ────────────────────────────────────────────
  echo ""
  echo "[4/6] Container Apps environment: ${ENVIRONMENT}"
  az containerapp env create \
    --resource-group "${RESOURCE_GROUP}" \
    --name "${ENVIRONMENT}" \
    --location "${LOCATION}" \
    --output none

  echo "  Registering Azure File Share with environment..."
  az containerapp env storage set \
    --resource-group "${RESOURCE_GROUP}" \
    --name "${ENVIRONMENT}" \
    --storage-name synthesis-data \
    --azure-file-account-name "${STORAGE_ACCOUNT}" \
    --azure-file-account-key "${STORAGE_KEY}" \
    --azure-file-share-name "${FILE_SHARE}" \
    --access-mode ReadWrite \
    --output none

fi  # end infrastructure block

# ==========================================================================
# IMAGE BUILD AND PUSH
# ==========================================================================

ACR_LOGIN_SERVER=$(az acr show \
  --name "${ACR_NAME}" \
  --query loginServer \
  --output tsv)

ACR_USERNAME=$(az acr credential show \
  --name "${ACR_NAME}" \
  --query username \
  --output tsv)

ACR_PASSWORD=$(az acr credential show \
  --name "${ACR_NAME}" \
  --query "passwords[0].value" \
  --output tsv)

FULL_IMAGE="${ACR_LOGIN_SERVER}/${IMAGE_NAME}:${IMAGE_TAG}"

echo ""
echo "[5/6] Building and pushing image: ${FULL_IMAGE}"
echo "  Source: ${SYNTHESIS_DIR}"
# az acr build sends the build context to ACR and builds in the cloud —
# no local Docker daemon required.
az acr build \
  --registry "${ACR_NAME}" \
  --image "${IMAGE_NAME}:${IMAGE_TAG}" \
  "${SYNTHESIS_DIR}"

# ==========================================================================
# CONTAINER APPS JOB — deployed via az rest PUT (bypasses CLI extension entirely)
# ==========================================================================
# Previous approaches (--yaml, az deployment group create) were intercepted by
# the containerapp CLI extension, which mangled secretRef values and stripped
# volume mounts. az rest sends JSON directly to the ARM API with no extension
# in the middle.

echo ""
echo "[6/6] Container Apps Job: ${JOB_NAME}"

SUBSCRIPTION=$(az account show --query id --output tsv)

ENV_RESOURCE_ID=$(az containerapp env show \
  --name "${ENVIRONMENT}" \
  --resource-group "${RESOURCE_GROUP}" \
  --query id \
  --output tsv)

# Build the job resource body using Python so JSON serialisation is correct
# and secret values with special characters are handled safely.
JOB_BODY_FILE="/tmp/job-body-$$.json"
trap 'rm -f "${JOB_BODY_FILE}"' EXIT

export LOCATION ENV_RESOURCE_ID FULL_IMAGE CRON_EXPRESSION REPLICA_TIMEOUT
export ACR_LOGIN_SERVER ACR_USERNAME ACR_PASSWORD
export ANTHROPIC_API_KEY BSKY_SYNTH_HANDLE BSKY_SYNTH_APP_PASSWORD

python3 - > "${JOB_BODY_FILE}" << 'PYEOF'
import json, os

print(json.dumps({
    "location": os.environ["LOCATION"],
    "identity": {"type": "SystemAssigned"},
    "properties": {
        "environmentId": os.environ["ENV_RESOURCE_ID"],
        "configuration": {
            "triggerType": "Schedule",
            "scheduleTriggerConfig": {
                "cronExpression": os.environ["CRON_EXPRESSION"],
                "parallelism": 1,
                "replicaCompletionCount": 1
            },
            "replicaTimeout": int(os.environ["REPLICA_TIMEOUT"]),
            "secrets": [
                {"name": "anthropic-api-key",      "value": os.environ["ANTHROPIC_API_KEY"]},
                {"name": "bsky-synth-handle",       "value": os.environ["BSKY_SYNTH_HANDLE"]},
                {"name": "bsky-synth-app-password", "value": os.environ["BSKY_SYNTH_APP_PASSWORD"]},
                {"name": "acr-password",            "value": os.environ["ACR_PASSWORD"]},
            ],
            "registries": [{
                "server":            os.environ["ACR_LOGIN_SERVER"],
                "username":          os.environ["ACR_USERNAME"],
                "passwordSecretRef": "acr-password"
            }]
        },
        "template": {
            "containers": [{
                "name":  "synthesis",
                "image": os.environ["FULL_IMAGE"],
                "resources": {"cpu": 0.5, "memory": "1Gi"},
                "env": [
                    {"name": "ANTHROPIC_API_KEY",       "secretRef": "anthropic-api-key"},
                    {"name": "BSKY_SYNTH_HANDLE",       "secretRef": "bsky-synth-handle"},
                    {"name": "BSKY_SYNTH_APP_PASSWORD", "secretRef": "bsky-synth-app-password"}
                ],
                "volumeMounts": [{"volumeName": "synthesis-data", "mountPath": "/data"}]
            }],
            "volumes": [{
                "name":        "synthesis-data",
                "storageType": "AzureFile",
                "storageName": "synthesis-data"
            }]
        }
    }
}, indent=2))
PYEOF

az rest \
  --method PUT \
  --url "https://management.azure.com/subscriptions/${SUBSCRIPTION}/resourceGroups/${RESOURCE_GROUP}/providers/Microsoft.App/jobs/${JOB_NAME}?api-version=2024-03-01" \
  --body "@${JOB_BODY_FILE}" \
  --output none

# ==========================================================================
# Summary
# ==========================================================================

echo ""
echo "=========================================================="
echo "Deployment complete"
echo "  Job:       ${JOB_NAME}"
echo "  Env:       ${ENVIRONMENT}"
echo "  Image:     ${FULL_IMAGE}"
echo "  Schedule:  ${CRON_EXPRESSION} UTC"
echo "  Data:      ${STORAGE_ACCOUNT}/${FILE_SHARE} → /data"
echo "=========================================================="
echo ""
echo "Trigger a manual run:"
echo "  az containerapp job start \\"
echo "    --name ${JOB_NAME} \\"
echo "    --resource-group ${RESOURCE_GROUP}"
echo ""
echo "Watch execution logs:"
echo "  az containerapp job execution list \\"
echo "    --name ${JOB_NAME} \\"
echo "    --resource-group ${RESOURCE_GROUP} \\"
echo "    --output table"
echo ""
echo "Stream logs for the most recent execution:"
echo "  EXEC=\$(az containerapp job execution list \\"
echo "    --name ${JOB_NAME} \\"
echo "    --resource-group ${RESOURCE_GROUP} \\"
echo "    --query '[0].name' --output tsv)"
echo "  az containerapp job logs show \\"
echo "    --name ${JOB_NAME} \\"
echo "    --resource-group ${RESOURCE_GROUP} \\"
echo "    --execution \"\${EXEC}\""
