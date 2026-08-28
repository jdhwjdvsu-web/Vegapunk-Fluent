#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PYTHON="${VEGAPUNK_PYTHON:-/home/vegapunk/.venvs/vegapunk311/bin/python}"
SPEC="${1:-$ROOT/config/fluent/mixing_elbow.optuna-demo.json}"
OUTPUT_DIR="${2:-$ROOT/runs/fluent_demo_v01}"
PORT="${FLUENT_UI_PORT:-8780}"

WINDOWS_HOST="$(ip route show default | sed -n 's/^default via \([^ ]*\).*/\1/p' | head -n 1)"
if [[ -z "$WINDOWS_HOST" || "$WINDOWS_HOST" == *[!0-9.]* ]]; then
  echo "Cannot determine the Windows host address from the WSL default route" >&2
  exit 1
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "Vegapunk Python was not found at $PYTHON" >&2
  exit 1
fi

export FLUENT_MCP_ENDPOINT="${FLUENT_MCP_ENDPOINT:-http://$WINDOWS_HOST:18000/mcp}"

echo "Starting Fluent Lab on http://0.0.0.0:$PORT"
echo "Fluent MCP endpoint: $FLUENT_MCP_ENDPOINT"
exec "$PYTHON" -m vegapunk.fluent.web \
  --host 0.0.0.0 \
  --port "$PORT" \
  --spec "$SPEC" \
  --output-dir "$OUTPUT_DIR"
