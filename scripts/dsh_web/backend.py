"""High-level facade tying runtime lifecycle, RPC, and the turn driver."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .driver import TurnDriver
from .errors import WebBackendError
from .rpc import HostClient, exchange_cookie
from .runtime import RuntimeProcess


class WebBackend:
    def __init__(
        self,
        *,
        state_dir: Path,
        mode: str,
        cwd: Path,
        user_home: Path,
        provider: str,
        model: str,
        host: str = "127.0.0.1",
        port: int | None = None,
        startup_timeout: float = 60.0,
        socket_timeout: float = 15.0,
        confirm_window: float = 8.0,
        env: dict[str, str] | None = None,
        command_factory: Callable[[list[str], Path, dict[str, str]], Any] | None = None,
    ) -> None:
        self.mode = mode
        self.cwd = cwd
        self.state_dir = state_dir
        self.runtime = RuntimeProcess(
            state_dir=state_dir,
            mode=mode,
            cwd=cwd,
            user_home=user_home,
            provider=provider,
            model=model,
            host=host,
            port=port,
            startup_timeout=startup_timeout,
            env=env,
            command_factory=command_factory,
        )
        self._socket_timeout = socket_timeout
        self._confirm_window = confirm_window
        self.client: HostClient | None = None
        self.driver: TurnDriver | None = None
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    def start(self) -> None:
        try:
            self.runtime.start()
            self._generation = self.runtime.generation
            jar = exchange_cookie(
                self.runtime.origin or "",
                self.runtime.launch_path or "/",
                self.runtime.token,
                timeout=self._socket_timeout,
            )
            self.client = HostClient(
                self.runtime.origin or "",
                jar,
                timeout=self._socket_timeout,
                token=self.runtime.token,
            )
        except Exception:
            self.runtime.terminate()
            raise

    def origin(self) -> str | None:
        return self.runtime.origin

    def public_url(self) -> str | None:
        """Return the operator-visible launch URL with the per-activation token.

        Callers must NOT persist, log, or include the token in error details.
        """
        return self.runtime.url

    def make_driver(self, session_key: str) -> TurnDriver:
        if self.client is None:
            raise WebBackendError("backend not started")
        driver = TurnDriver(
            runtime=self.runtime,
            client=self.client,
            session_key=session_key,
            cwd=self.cwd,
            confirm_window=self._confirm_window,
            confirm_unconfirmed=self._terminate_unconfirmed,
        )
        self.driver = driver
        return driver

    def _terminate_unconfirmed(self) -> None:
        self.runtime._terminate()

    def terminate(self) -> None:
        self.runtime.terminate()
        self.client = None
        self.driver = None
