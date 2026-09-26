"""Select an existing Node runtime without loading shell profiles or installing it."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import MutableMapping
from pathlib import Path


class NodeRuntimeError(RuntimeError):
    """A safe, actionable prerequisite failure, never containing child output."""


_PROBE = (
    "const u=require('node:util');"
    "process.exit(Number(process.versions.node.split('.')[0])>=22"
    " && typeof u.parseEnv==='function' ? 0 : 1)"
)
_ERROR = (
    "DSH Scout requires Node 22+ with util.parseEnv. "
    "Set DSH_SCOUT_NODE to an existing absolute bin/node executable, "
    "or expose it on PATH or under NVM_DIR (default ~/.nvm)."
)


def _compatible(node: Path, env: MutableMapping[str, str]) -> bool:
    try:
        result = subprocess.run(
            [str(node), "-e", _PROBE],
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _nvm_candidates(env: MutableMapping[str, str]) -> list[Path]:
    home = Path(env.get("HOME", str(Path.home())))
    root = Path(env.get("NVM_DIR", str(home / ".nvm"))) / "versions/node"
    versions: list[tuple[tuple[int, ...], Path]] = []
    for directory in root.glob("v*"):
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", directory.name)
        if match:
            versions.append((tuple(map(int, match.groups())), directory / "bin/node"))
    return [node for _, node in sorted(versions, reverse=True)]


def select_node(env: MutableMapping[str, str]) -> Path:
    override = env.get("DSH_SCOUT_NODE")
    if override is not None:
        node = Path(override)
        if node.is_absolute() and node.name == "node" and _compatible(node, env):
            return node
        raise NodeRuntimeError(_ERROR + " The explicit DSH_SCOUT_NODE is invalid.")
    current = shutil.which("node", path=env.get("PATH", os.defpath))
    candidates = ([Path(current).absolute()] if current else []) + _nvm_candidates(env)
    for node in candidates:
        if _compatible(node, env):
            return node.absolute()
    raise NodeRuntimeError(_ERROR)


def configure_node(env: MutableMapping[str, str]) -> None:
    """Propagate the selected binary to env-node DSH and all Node helpers."""
    node = select_node(env)
    env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", os.defpath)
