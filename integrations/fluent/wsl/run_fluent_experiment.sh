#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
PYTHON="${VEGAPUNK_PYTHON:-/home/vegapunk/.venvs/vegapunk311/bin/python}"
SPEC="${1:-$ROOT/config/fluent/mixing_elbow.example.json}"
OUTPUT_DIR="${2:-$HOME/vegapunk-fluent-run}"

WINDOWS_HOST="$(ip route show default | sed -n 's/^default via \([^ ]*\).*/\1/p' | head -n 1)"
if [[ -z "$WINDOWS_HOST" || "$WINDOWS_HOST" == *[!0-9.]* ]]; then
  echo "Cannot determine the Windows host address from the WSL default route" >&2
  exit 1
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "Vegapunk Python was not found at $PYTHON" >&2
  exit 1
fi
if [[ -z "${FLUENT_CASE_FILE:-}" ]]; then
  echo "FLUENT_CASE_FILE must contain a Windows path visible to Fluent" >&2
  exit 1
fi

exec "$PYTHON" -m vegapunk.fluent \
  --spec "$SPEC" \
  --output-dir "$OUTPUT_DIR" \
  --endpoint "http://$WINDOWS_HOST:18000/mcp" \
  --allow-remote-endpoint
