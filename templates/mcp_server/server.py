"""Minimal Claire MCP server template.

Copy this directory, rename it, and fill in every ``FILL IN`` block.
Claire launches the file as a stdio subprocess and speaks the Model
Context Protocol to it over stdin/stdout, so:

* stdout carries protocol frames only. Never ``print()`` to stdout —
  send diagnostics to stderr (Claire discards the child's stderr) or to
  a log file.
* the server is long-lived: Claire keeps one session open for the
  daemon's lifetime, so module-level state survives across tool calls.

Run it directly to check that it starts:

    python server.py

Do NOT add ``from __future__ import annotations`` to this file. FastMCP
inspects tool parameter annotations at import time, and with postponed
evaluation they arrive as strings, which fails with
``TypeError: issubclass() arg 1 must be a class``. Keep annotations
evaluated (plain Python 3.11+ builtin generics are still fine).
"""

from mcp.server.fastmcp import FastMCP

# FILL IN 1 — the server name.
#
# Keep this identical to the key you use under "mcps" in config.json, so
# a tool call and a log line name the same thing. Claire namespaces every
# discovered tool as "<server>__<tool>", so a tool called "echo" on a
# server called "example" reaches the model as "example__echo".
SERVER_NAME = "example"

mcp = FastMCP(SERVER_NAME)


# FILL IN 2 — one tool. This is what Claire actually discovers and calls.
#
# The signature is the tool's input schema: type the parameters, give
# each one a docstring line, and keep them JSON-representable. Return a
# string (or a JSON-serialisable object); Claire passes the flattened
# text of the result back to the model as the tool's reply. Raise an
# exception to report failure — the model sees the error text.
@mcp.tool()
def echo(text: str) -> str:
    """Return the input unchanged, prefixed with the server name.

    Args:
        text: The text to echo back.
    """
    return f"{SERVER_NAME} heard: {text}"


# FILL IN 3 — one resource. A resource is read-only data the server
# exposes by URI.
#
# Claire speaks protocol 2025-06-18, which negotiates resources, but its
# client currently discovers and calls tools only: a resource declared
# here is reachable from any MCP client, not yet from a spoken reply.
# Keep the definition so the server is complete and future-proof.
@mcp.resource("example://greeting")
def greeting() -> str:
    """A static greeting served at example://greeting."""
    return "Hello from the example MCP server."


# FILL IN 4 — one prompt. A prompt is a reusable instruction template
# the server offers by name.
#
# Same caveat as resources: negotiated by the protocol, not consumed by
# Claire's client today. Declare it for completeness and for other MCP
# clients.
@mcp.prompt()
def summarise(text: str) -> str:
    """Ask the model to summarise `text` in one sentence."""
    return f"Summarise the following in one sentence:\n\n{text}"


if __name__ == "__main__":
    # stdio is FastMCP's default transport and the only one Claire speaks.
    mcp.run()
