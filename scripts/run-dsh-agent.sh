#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: run-dsh-agent.sh --mode write|read --cwd DIR --session-key KEY --prompt-file FILE [--rotate] [--timeout-seconds N]

Modes:
  write  Full project access; reuses one persistent DSH writer process.
  read   Project roots are mounted read-only; reuses one persistent DSH reader process.
EOF
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$script_dir/run_dsh_session.py" "$@"
