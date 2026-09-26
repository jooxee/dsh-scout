#!/usr/bin/env bash
# Portable installer for the dsh-scout Agent Skill package.
#
# Modes:
#   no arguments            Install into the Codex default location.
#   --host HOST             Install into the preset location for HOST.
#   --destination PATH      Install into PATH (whose basename must be dsh-scout).
#   --force                 Overwrite an existing installation.
#   -h | --help | help      Print usage and exit.
#
# --host and --destination are mutually exclusive. Errors and conflicts are
# detected before any file is written. Filesystem failures can leave a partial
# installation; correct the failure and rerun with --force.

set -euo pipefail

PROG="${0##*/}"

usage() {
  cat <<EOF
Usage: ${PROG} [--host codex|claude-code|opencode|pi|omp|cursor]
              [--destination PATH] [--force] [-h|--help|help]

Installs the dsh-scout Agent Skill package (SKILL.md, scripts, referenced docs)
into a host-specific skills directory. The destination directory basename must
be 'dsh-scout'.

Options:
  --host HOST         Preset host. One of: codex, claude-code, opencode,
                      pi, omp, cursor.
  --destination PATH  Install into PATH (basename must be dsh-scout).
                      Mutually exclusive with --host.
  --force             Replace an existing installation at the destination.
                      Without this flag, an existing installation causes
                      the command to exit with status 17.
  -h, --help, help    Print this help and exit.

If neither --host nor --destination is supplied, the Codex preset is used
(\$CODEX_HOME/skills/dsh-scout, defaulting to ~/.codex/skills/dsh-scout).
The installer never edits host settings or global host instructions.
EOF
}

die() {
  printf '%s: %s\n' "$PROG" "$*" >&2
  exit 2
}

# host_preset <name> -> echo absolute destination path or exit 2.
host_preset() {
  case "$1" in
    codex)       printf '%s\n' "${CODEX_HOME:-$HOME/.codex}/skills/dsh-scout" ;;
    claude-code) printf '%s\n' "$HOME/.claude/skills/dsh-scout" ;;
    opencode)
      local xdg="${XDG_CONFIG_HOME:-$HOME/.config}"
      printf '%s\n' "${xdg}/opencode/skills/dsh-scout"
      ;;
    pi)    printf '%s\n' "$HOME/.pi/agent/skills/dsh-scout" ;;
    omp)   printf '%s\n' "$HOME/.omp/agent/skills/dsh-scout" ;;
    cursor) printf '%s\n' "$HOME/.cursor/skills/dsh-scout" ;;
    *)     die "unknown host preset: '$1'. Expected one of: codex, claude-code, opencode, pi, omp, cursor." ;;
  esac
}

# validate_destination <path> -> echo trimmed path or exit 2.
validate_destination() {
  local candidate="${1%/}"
  [[ -n "$candidate" ]] || die "destination path is empty"
  local basename="${candidate##*/}"
  [[ "$basename" == "dsh-scout" ]] \
    || die "destination must end with basename dsh-scout, got '$basename'"
  printf '%s\n' "$candidate"
}

# require_value <flag-name> <candidate> echoes the candidate or exits 2.
require_value() {
  if [[ -z "${2-}" || "${2-}" == --* ]]; then
    die "--$1 requires a value"
  fi
  printf '%s\n' "$2"
}

# parse_flag_args <args...> sets the variables force, host, dest,
# given_host, given_dest for the caller to consult.
parse_flag_args() {
  force=false
  host=""
  dest=""
  given_host=false
  given_dest=false
  while (($#)); do
    case "$1" in
      --force)
        force=true
        shift
        ;;
      --host)
        $given_host && die "--host specified more than once"
        value="$(require_value host "${2-}")"
        shift 2
        host="$value"
        given_host=true
        ;;
      --destination)
        value="$(require_value destination "${2-}")"
        shift 2
        $given_dest && die "--destination specified more than once"
        dest="$value"
        given_dest=true
        ;;
      -h|--help|help)
        usage
        exit 0
        ;;
      -*)
        die "unknown option: '$1'. Run with --help for usage."
        ;;
      *)
        die "unexpected positional argument: '$1'. Run with --help for usage."
        ;;
    esac
  done
  if $given_host && $given_dest; then
    die "--host and --destination are mutually exclusive."
  fi
}

# check_and_prepare <dest> creates the install tree, refusing any overwrite
# without --force. No payload files are copied yet so a failed check leaves
# the destination untouched.
check_and_prepare() {
  local dest="$1"
  if [[ -e "$dest" && ! -d "$dest" ]]; then
    die "cannot install into '$dest': not a directory"
  fi
  if [[ -e "$dest" && "$force" != true ]]; then
    printf 'dsh-scout is already installed at %s; pass --force to replace it.\n' \
      "$dest" >&2
    exit 17
  fi
  mkdir -p "$dest"
}

install_payload() {
  local dest="$1" repo_root="$2" module=""
  mkdir -p "$dest/agents" "$dest/scripts/dsh_web" \
           "$dest/docs/specifications"
  install -m 0644 "$repo_root/SKILL.md" "$dest/SKILL.md"
  install -m 0644 "$repo_root/agents/openai.yaml" "$dest/agents/openai.yaml"
  install -m 0755 "$repo_root/scripts/run-dsh-agent.sh" "$dest/scripts/run-dsh-agent.sh"
  install -m 0755 "$repo_root/scripts/run_dsh_session.py" "$dest/scripts/run_dsh_session.py"
  install -m 0644 "$repo_root/scripts/node_runtime.py" "$dest/scripts/node_runtime.py"
  for module in "$repo_root/scripts/dsh_web/"*.py; do
    [[ -e "$module" ]] || continue
    install -m 0755 "$module" "$dest/scripts/dsh_web/$(basename "$module")"
  done
  install -m 0755 "$repo_root/scripts/watch_dsh_events.py" "$dest/scripts/watch_dsh_events.py"
  install -m 0755 "$repo_root/scripts/scout_status.py" "$dest/scripts/scout_status.py"
  for module in "$repo_root/docs/specifications/"*.md; do
    [[ -e "$module" ]] || continue
    install -m 0644 "$module" "$dest/docs/specifications/$(basename "$module")"
  done
  install -m 0644 "$repo_root/docs/integrations.md" "$dest/docs/integrations.md"
  install -m 0644 "$repo_root/LICENSE" "$dest/LICENSE"
}

main() {
  parse_flag_args "$@"
  local repo_root dest_raw
  repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
  if $given_host; then
    dest_raw="$(host_preset "$host")"
  elif $given_dest; then
    dest_raw="$dest"
  else
    dest_raw="$(host_preset codex)"
  fi
  local dest
  dest="$(validate_destination "$dest_raw")"
  check_and_prepare "$dest"
  install_payload "$dest" "$repo_root"
  printf 'Installed dsh-scout at %s\n' "$dest"
}

main "$@"
