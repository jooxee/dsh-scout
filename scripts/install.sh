#!/usr/bin/env bash
set -euo pipefail

force=false
if [[ "${1:-}" == "--force" ]]; then
  force=true
  shift
fi
if (($#)); then
  printf 'Usage: %s [--force]\n' "$0" >&2
  exit 2
fi

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
codex_home="${CODEX_HOME:-$HOME/.codex}"
destination="$codex_home/skills/dsh-scout"

if [[ -e "$destination/SKILL.md" && "$force" != true ]]; then
  printf 'dsh-scout is already installed at %s; pass --force to replace it.\n' "$destination" >&2
  exit 17
fi

mkdir -p "$destination/agents" "$destination/scripts"
install -m 0644 "$repo_root/SKILL.md" "$destination/SKILL.md"
install -m 0644 "$repo_root/agents/openai.yaml" "$destination/agents/openai.yaml"
install -m 0755 "$repo_root/scripts/run-dsh-agent.sh" "$destination/scripts/run-dsh-agent.sh"
install -m 0755 "$repo_root/scripts/run_dsh_session.py" "$destination/scripts/run_dsh_session.py"
mkdir -p "$destination/scripts/dsh_web"
for module in "$repo_root/scripts/dsh_web/"*.py; do
    install -m 0755 "$module" "$destination/scripts/dsh_web/$(basename "$module")"
done
install -m 0755 "$repo_root/scripts/watch_dsh_events.py" "$destination/scripts/watch_dsh_events.py"

printf 'Installed dsh-scout at %s\n' "$destination"
