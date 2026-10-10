"""Run the phone bridge, and look after the token it authenticates with.

``claire.daemon`` starts the bridge in-process: it imports
:class:`~claire.phone.bridge.PhoneBridge` and
:func:`~claire.phone.bridge.create_server`, calls ``_start_phone_bridge``, and
injects the running voice loop's ``_phone_utterance_sink``, so a phone utterance
reaches the same conversation as speech.  This module is the separate, terminal
entry point -- the smallest one that makes the bridge usable outside the daemon,
and the way to manage the token it authenticates with:

``python -m claire.phone serve``
    Bind the bridge and serve until interrupted.  Loopback by default, exactly
    as :func:`claire.phone.bridge.resolve_bind_host` insists; binding to a
    non-loopback address needs ``--allow-public-bind`` and prints the module's
    own loud warning.

``python -m claire.phone keys list``
    Every key in the store, masked -- name, last-4 handle and fingerprint.  No
    raw value is ever printed by this command.

``python -m claire.phone keys show NAME``
    The masked rendering of one key.  ``--reveal`` prints the raw value, which
    is the only way this project will show a secret and only because the owner
    asked for it in so many words: the raw bridge token has to be typed into the
    phone once.  Never pass ``--reveal`` through a log, a job runner or a shell
    history you do not control.

``python -m claire.phone keys set NAME``
    Store a fresh 32-byte token under ``NAME`` (or the value given on the
    command line) and report it masked.  This is what to run when the store is
    lost or the token has to be rotated.

Every command that reports a token reports it masked, because a raw token in a
terminal scrollback is a raw token in a screenshot.  The exception is
``keys show --reveal``, which exists because the phone needs the value once.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from typing import Any, Optional, Sequence

from ..platform.keys import (
    KeyNotFoundError,
    KeyStore,
    KeyStoreError,
    fingerprint,
    mask,
)
from .bridge import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    HOST_ENV,
    PORT_ENV,
    TOKEN_BYTES,
    TOKEN_KEY_NAME,
    PhoneBridge,
    ReplyQueue,
    PhoneBridgeError,
    create_server,
    serve,
)

#: Commands this entry point implements.  ``bridge.py``'s error remedies name
#: these, and ``tests/test_phone_bridge.py`` parses the remedies to prove the
#: command they tell the owner to run is a command that exists.
SERVE_COMMAND = "serve"
KEYS_COMMAND = "keys"
KEY_ACTIONS = ("list", "show", "set")


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``python -m claire.phone``."""
    parser = argparse.ArgumentParser(
        prog="python -m claire.phone",
        description="Run Claire's phone bridge, and manage its token.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser(
        SERVE_COMMAND,
        help="Bind the bridge and serve until interrupted.",
        description=(
            "Bind the bridge and serve until interrupted.  Loopback by default: "
            f"pass --host to change it, and --allow-public-bind to say out loud "
            "that non-loopback plain HTTP is understood."
        ),
    )
    serve_parser.add_argument(
        "--host",
        default=None,
        help=(
            "Address to bind.  Defaults to $%s, else %s (loopback)."
            % (HOST_ENV, DEFAULT_HOST)
        ),
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to bind.  Defaults to $%s, else %d." % (PORT_ENV, DEFAULT_PORT),
    )
    serve_parser.add_argument(
        "--allow-public-bind",
        action="store_true",
        help=(
            "Permit a non-loopback bind.  Without this the module refuses "
            "before a socket exists."
        ),
    )
    serve_parser.add_argument(
        "--photo-dir",
        default=None,
        help="Where POST /see stores frames.  Defaults to the module's own.",
    )
    serve_parser.add_argument(
        "--key-store",
        default=None,
        help="Path to a key store other than the default 0600 one.",
    )
    serve_parser.add_argument(
        "--echo",
        action="store_true",
        help=(
            "Diagnostic mode: no daemon is running, so wire a sink that puts "
            "each received utterance straight back into GET /reply.  This "
            "proves the /text -> /reply round trip and is NOT a production "
            "sink -- in normal use a real sink hands the words to Claire."
        ),
    )

    keys_parser = subparsers.add_parser(
        KEYS_COMMAND,
        help="Inspect or rotate the bridge token (masked unless --reveal).",
    )
    keys_parser.add_argument("action", choices=KEY_ACTIONS)
    keys_parser.add_argument(
        "name",
        nargs="?",
        default=TOKEN_KEY_NAME,
        help="Key name.  Defaults to %s." % TOKEN_KEY_NAME,
    )
    keys_parser.add_argument(
        "value",
        nargs="?",
        default=None,
        help=(
            "Value for 'set'.  Omit it and a fresh %d-byte token is minted, "
            "which is the safe default." % TOKEN_BYTES
        ),
    )
    keys_parser.add_argument(
        "--reveal",
        action="store_true",
        help=(
            "For 'show': print the raw value.  The only deliberate way to read "
            "a secret here; everything else reports it masked."
        ),
    )
    keys_parser.add_argument(
        "--key-store",
        default=None,
        help="Path to a key store other than the default 0600 one.",
    )
    return parser


def _store(path: Optional[str]) -> KeyStore:
    return KeyStore(path) if path else KeyStore()


def _build_bridge(args: argparse.Namespace) -> PhoneBridge:
    """Wire the bridge exactly as the flags describe, and nothing more."""
    queue = ReplyQueue()
    sink = None
    if args.echo:
        def sink(utterance: dict) -> None:  # noqa: E306 - clear in local scope
            """Stand in for the daemon's utterance sink, for diagnostics only."""
            text = str(utterance.get("text", ""))
            queue.put_text("echo: %s" % text)

    return PhoneBridge(
        store=args.key_store or None,
        sink=sink,
        queue=queue,
        photo_dir=args.photo_dir,
        host=args.host,
        port=args.port,
    )


def _cmd_serve(args: argparse.Namespace) -> int:
    bridge = _build_bridge(args)
    server = create_server(
        bridge,
        host=args.host,
        port=args.port,
        allow_public_bind=args.allow_public_bind,
    )
    bound_host, bound_port = server.server_address[:2]
    # The URL, the token's masked handle and the vision verdict: enough to act
    # on, and not one character of the token itself.
    print(
        "phone bridge listening on http://%s:%d" % (bound_host, bound_port),
        flush=True,
    )
    print("token: %s" % bridge.masked_token(), flush=True)
    print("visions: %s" % (bridge.vision_status(),), flush=True)
    if args.echo:
        print(
            "diagnostic: --echo is on, so /text is returned by /reply and no "
            "daemon is involved",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("phone bridge stopping", flush=True)
    finally:
        server.server_close()
    return 0


def _cmd_keys(args: argparse.Namespace) -> int:
    store = _store(args.key_store)
    if args.action == "list":
        records = store.describe()
        if not records:
            print("no keys in %s" % store.path)
            return 0
        print("keys in %s" % store.path)
        for record in records:
            print("  %s" % record)
        return 0

    if args.action == "set":
        value = args.value if args.value is not None else secrets.token_urlsafe(TOKEN_BYTES)
        record = store.store(args.name, value)
        print("stored %s" % record)
        return 0

    # show
    try:
        value = store.get(args.name)
    except KeyNotFoundError:
        print("no key %r in %s" % (args.name, store.path), file=sys.stderr)
        return 1
    if args.reveal:
        # The one deliberate reveal in this package.  Raw value, no newline so
        # it can be piped; the caller owns the consequences.
        print(value)
    else:
        print("%s: %s#%s (store)" % (args.name, mask(value), fingerprint(value)))
        print("pass --reveal to print the raw value", file=sys.stderr)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the phone-bridge entry point.  Returns the process exit code."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == SERVE_COMMAND:
            return _cmd_serve(args)
        return _cmd_keys(args)
    except PhoneBridgeError as exc:
        # A named bridge error already says what to do; print it, do not trace.
        print("%s" % exc, file=sys.stderr)
        remedy = getattr(exc, "remedy", "")
        if remedy:
            print("remedy: %s" % remedy, file=sys.stderr)
        return 2
    except KeyStoreError as exc:
        print("%s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
