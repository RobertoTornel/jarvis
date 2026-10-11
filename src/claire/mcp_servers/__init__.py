"""First-party MCP servers that ship inside the ``claire`` package.

Unlike the servers built from ``templates/mcp_server`` (copied out of the
repository and run with their own dependency set), these run on the daemon's
own interpreter: ``python -m claire.mcp_servers.<name>``. Keep that import
cheap — the daemon spawns one subprocess per configured server.
"""
