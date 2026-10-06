"""The Claire web panel — a small, loopback-only window onto Claire's state.

One server-rendered page, standard library only (``http.server`` plus a tiny
HTML renderer): no Flask, no JavaScript framework, no build step.  It shows the
selected provider and model, which providers have a key stored (**masked**: last
four characters plus a fingerprint), ``CLAIRE.md``'s bytes against its 6000-byte
budget with the same loud framing the prompt uses, today's calendar, the
self-check report, the tail of the action journal, and a selector form that
changes the provider/model selection.

The contract, and the security invariants the tests assert, live in
``panel.spec.md`` next to :mod:`claire.panel.server`.  The short version:

* **No raw key can be rendered.**  Masked renderings come from
  :meth:`claire.platform.keys.KeyStore.render`; the panel never calls
  ``KeyStore.get`` (the codebase's one raw-value accessor), and the only
  credential-bearing field on a page must be empty or mask-shaped or
  :class:`PanelSecretError` is raised.  There is no key field on the page, and a
  POST carrying one is refused.
* **Loopback only.**  :data:`DEFAULT_BIND_HOST` is ``127.0.0.1``;
  :func:`guard_bind_host` refuses anything else without an explicit opt-in, which
  logs that the panel would be plain HTTP.
* **The selector write is protected.**  ``POST /select`` requires ``Origin`` and
  ``Host`` to agree on a loopback origin, so a page the owner is merely visiting
  cannot drive it.
* **No file paths are served.**  Every route is an exact string, so ``..``
  traversal is refused by name and reads nothing.
* **State is injected**, so every route is testable without a socket:

  >>> from claire.panel import PanelRequest, dispatch
  >>> class Demo(PanelState):
  ...     def provider_model(self): return ProviderModel(provider="ollama", model="m")
  >>> dispatch(Demo(), PanelRequest(path="/")).status
  200

Start it with ``python -m claire.panel.server`` and open
``http://127.0.0.1:8765/``.
"""

from claire.panel.server import (
    ACTION_LOG_ENV_VAR,
    DEFAULT_BIND_HOST,
    DEFAULT_PORT,
    FORBIDDEN_KEY_FIELDS,
    GET_ROUTES,
    LOOPBACK_HOSTS,
    MAX_BODY_BYTES,
    PANEL_VERSION,
    PROVIDER_KEY_NAMES,
    ROUTE_HEALTH,
    ROUTE_INDEX,
    ROUTE_SELF_CHECK,
    ROUTE_SELECT,
    ActionRecord,
    ActionsStatus,
    CalendarEvent,
    CalendarStatus,
    ConfigCredential,
    KeyStatus,
    PanelBadRequest,
    PanelBindError,
    PanelError,
    PanelHandler,
    PanelMethodNotAllowed,
    PanelOriginError,
    PanelRequest,
    PanelResponse,
    PanelRouteNotFound,
    PanelSecretError,
    PanelServer,
    PanelState,
    PanelStateError,
    PanelTooLarge,
    PanelTraversalError,
    PanelWriteError,
    ProviderModel,
    RealPanelState,
    RouterStatus,
    SelfCheckStatus,
    SelfCheckView,
    WriteResult,
    check_write_origin,
    create_handler,
    create_server,
    dispatch,
    guard_bind_host,
    is_loopback_host,
    is_mask_shaped,
    main,
    parse_form,
    parse_target,
    render_error_page,
    render_health,
    render_index,
    render_self_check_text,
    serve,
)

__all__ = [
    "ACTION_LOG_ENV_VAR",
    "DEFAULT_BIND_HOST",
    "DEFAULT_PORT",
    "FORBIDDEN_KEY_FIELDS",
    "GET_ROUTES",
    "LOOPBACK_HOSTS",
    "MAX_BODY_BYTES",
    "PANEL_VERSION",
    "PROVIDER_KEY_NAMES",
    "ROUTE_HEALTH",
    "ROUTE_INDEX",
    "ROUTE_SELF_CHECK",
    "ROUTE_SELECT",
    "ActionRecord",
    "ActionsStatus",
    "CalendarEvent",
    "CalendarStatus",
    "ConfigCredential",
    "KeyStatus",
    "PanelBadRequest",
    "PanelBindError",
    "PanelError",
    "PanelHandler",
    "PanelMethodNotAllowed",
    "PanelOriginError",
    "PanelRequest",
    "PanelResponse",
    "PanelRouteNotFound",
    "PanelSecretError",
    "PanelServer",
    "PanelState",
    "PanelStateError",
    "PanelTooLarge",
    "PanelTraversalError",
    "PanelWriteError",
    "ProviderModel",
    "RealPanelState",
    "RouterStatus",
    "SelfCheckStatus",
    "SelfCheckView",
    "WriteResult",
    "check_write_origin",
    "create_handler",
    "create_server",
    "dispatch",
    "guard_bind_host",
    "is_loopback_host",
    "is_mask_shaped",
    "main",
    "parse_form",
    "parse_target",
    "render_error_page",
    "render_health",
    "render_index",
    "render_self_check_text",
    "serve",
]
