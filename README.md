# Langflow MCP

This is a Python MCP server for controlling a Langflow instance over the official
Langflow HTTP API.

It follows the same pattern as your existing MCPs:

- Python entrypoint in `server.py`
- `FastMCP` tools for hosted MCP access
- `.env`-driven configuration
- simple deployment file for Render

## What it exposes

The server includes tools for:

- Langflow health, version, config, and current user lookup
- user administration
- global variables
- projects import/export and CRUD
- flows import/export and CRUD
- flow execution with `/run` and `/webhook`
- build jobs for editor-style automation
- v1 and v2 file management
- a `raw_api_request` escape hatch for any JSON endpoint not wrapped yet

## Configuration

Copy `.env.example` to `.env` and fill in the values you need.

Important variables:

- `LANGFLOW_BASE_URL`: Your Langflow server, for example `http://localhost:7860`
- `LANGFLOW_API_KEY`: Recommended auth option
- `LANGFLOW_BEARER_TOKEN`: Optional alternative to API key auth
- `LANGFLOW_TIMEOUT_SECONDS`: HTTP timeout per request
- `LANGFLOW_VERIFY_SSL`: Set to `false` if you use a self-signed cert internally
- `MCP_TRANSPORT`: `streamable-http`, `stdio`, or `sse`
- `MCP_HOST`: bind host for hosted mode
- `PORT`: bind port for hosted mode

## Run locally

Install dependencies:

```bash
pip install -r requirements.txt
```

Hosted MCP:

```bash
python server.py
```

The hosted MCP endpoint is `/mcp`. For a local server, configure MCP clients with:

```text
http://localhost:8080/mcp
```

For clients that prefer the older SSE transport, the hosted server also exposes:

```text
http://localhost:8080/sse
```

The root URL (`/`) and `/healthz` return simple JSON status responses for browser
checks and deployment health checks, but they are not MCP endpoints.

Local desktop/stdin mode:

```bash
python server.py --stdio
```

## Example MCP client config

Hosted:

```json
{
  "mcpServers": {
    "langflow-hosted": {
      "type": "streamable-http",
      "url": "https://your-hosted-service.example.com/mcp"
    }
  }
}
```

SSE-compatible clients can use:

```text
https://your-hosted-service.example.com/sse
```

Local:

```json
{
  "mcpServers": {
    "langflow-local": {
      "command": "python3",
      "args": [
        "/absolute/path/to/langflow-mcp/server.py",
        "--stdio"
      ]
    }
  }
}
```

## Example usage ideas

- list projects, create a project, and import a flow JSON into it
- upload a reusable file with `upload_user_file`, then pass its `path` into a
  flow `tweaks` object through `run_flow`
- trigger production automations with `run_flow_webhook`
- manage secrets or non-secret shared values with variables tools
- export projects and flows for backup jobs

## Verification

Basic syntax check:

```bash
python -m py_compile server.py
```

## Notes

- The Langflow API surface is based on the official docs for Langflow 1.7.x.
- The `/run` and `/webhook` endpoints are the main automation entrypoints.
- Some Langflow endpoints use `project_id` and others use `folder_id`; this MCP
  accepts either where that distinction shows up in docs.
