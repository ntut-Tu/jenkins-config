#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    cat <<'USAGE'
Usage: ./deploy.sh [--settings FILE] [--state DIR] [--secrets DIR]

Initialize missing secrets and deploy Jenkins using the existing Python CLI.
Defaults: settings.local.yaml, .state, .secrets in the project directory.
Relative option paths are resolved from the project directory.
Requires uv and Docker with Compose v2; fill settings.local.yaml first.
USAGE
    exit 0
fi

for tool in uv docker; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "Required command not found: $tool" >&2
        exit 127
    }
done

uv run --directory "$project_dir" --locked python -m jenkins_config init "$@"
exec uv run --directory "$project_dir" --locked python -m jenkins_config up "$@"
