"""Web backend exception hierarchy."""


class WebBackendError(Exception):
    """Base class for web backend failures that must never silently recover."""


class StartupError(WebBackendError):
    """The web runtime never produced its authenticated URL or died first."""


class AuthError(WebBackendError):
    """Remote answered 401/403 or the cookie exchange was refused."""


class ProtocolError(WebBackendError):
    """The Remote response was not a valid ``server-response`` envelope or the
    stream emitted a malformed frame."""


class TransportError(WebBackendError):
    """A network or socket failure that may or may not be retryable."""


class CancellationUnconfirmed(WebBackendError):
    """The cancel was rejected, timed out, or the transport broke before
    ``turn/end`` could be confirmed for the admitted turn."""
