#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: run-dsh-agent.sh --mode write|read --cwd DIR --session-key KEY --prompt-file FILE [--rotate] [--show-ui-url] [--backend web|sdk]

Modes:
  write  Full project access; reuses one persistent DSH writer process.
  read   Project roots are mounted read-only; reuses one persistent DSH reader process.

Turns have no execution-time limit. Keep the launcher attached until a terminal
event; closing it cancels the active turn.

Backend (DSH_SCOUT_BACKEND, default web):
  web   Scout-managed ordinary DSH web runtime; UI events stream live to the
        controller-owned UI. The runtime URL is announced on the prompt response
        (and on demand via --show-ui-url).
  sdk   Legacy DSH SDK process (no live UI). Explicit opt-in.
EOF
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$script_dir/run_dsh_session.py" "$@"
