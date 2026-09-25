"""Settings seeding.

The operator's ``$DSH_HOME/settings.yaml`` is parsed with the DSH-bundled
``js-yaml`` parser, mutated in memory, re-emitted with stable key ordering,
and written atomically with mode ``0600``. The path is then quoted with a
single-quoted YAML scalar for the patch file (so provider routes with
unusual characters never break the profile loader). The patch file itself
is a small, fixed-shape YAML document owned by this skill.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .errors import WebBackendError
from .protocol import _yaml_safe_dump, _yaml_safe_load, atomic_private_write


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text()
    except OSError as error:
        raise WebBackendError(f"cannot read operator settings: {error}") from error


def _load_or_empty(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        loaded = _yaml_safe_load(text)
    except ValueError as error:
        raise WebBackendError(
            f"operator settings are not valid YAML: {error}"
        ) from error
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise WebBackendError("operator settings must be a YAML mapping")
    return loaded


def _quote_for_patch(path: Path) -> str:
    """Single-quote a path for YAML scalar use (handles arbitrary chars)."""
    text = str(path)
    return "'" + text.replace("'", "''") + "'"


def seed_settings(
    *,
    state_dir: Path,
    user_home: Path,
    provider: str,
    model: str,
) -> tuple[Path, Path]:
    """Write a scout-owned settings + profile-patch pair under ``state_dir``.

    Returns ``(settings_path, patch_path)``. Both files are mode ``0600`` and
    the directory is mode ``0700``. The operator's settings file is read but
    never written. This private local snapshot can contain sensitive settings;
    it must not be printed, committed, or sent to the model.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    try:
        import os as _os

        _os.chmod(state_dir, 0o700)
    except OSError:
        pass

    settings_path = state_dir / "scout-settings.yaml"
    patch_path = state_dir / "web-patch.yml"

    text = _read_text(user_home / "settings.yaml")
    parsed = _load_or_empty(text)
    parsed["agent-default-model"] = {"provider": provider, "model": model}

    try:
        body = _yaml_safe_dump(parsed)
    except ValueError as error:
        raise WebBackendError(f"cannot serialize settings: {error}") from error

    atomic_private_write(settings_path, body + "\n")

    quoted = _quote_for_patch(settings_path)
    patch_text = f"- id: settings\n  config:\n    path: {quoted}\n"
    atomic_private_write(patch_path, patch_text)

    return settings_path, patch_path
