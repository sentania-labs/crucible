"""The client library every `crucible` command group uses (docs/client.md).

It owns what a caller of the API sees: the envelope, the error codes, the `next`
actions, the JSON schemas, the HTTP transport, and the client configuration. It never
imports the application, the adapters, or a web framework, so any later interface (an
MCP server, say) can sit on the same library without the server's dependencies.
"""
