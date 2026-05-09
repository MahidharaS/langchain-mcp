#!/usr/bin/env python3
from __future__ import annotations

import argparse
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP


load_dotenv()

DEFAULT_LANGFLOW_BASE_URL = "http://localhost:7860"
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 8080
VALID_TRANSPORTS = {"stdio", "streamable-http", "sse"}


JsonDict = dict[str, Any]


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str | None
    bearer_token: str | None
    timeout_seconds: float
    verify_ssl: bool


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return value


def _normalize_base_url(raw_url: str) -> str:
    url = raw_url.strip() or DEFAULT_LANGFLOW_BASE_URL
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "LANGFLOW_BASE_URL must be a full http(s) URL, for example http://localhost:7860."
        )
    return url.rstrip("/")


def load_settings() -> Settings:
    return Settings(
        base_url=_normalize_base_url(os.getenv("LANGFLOW_BASE_URL", DEFAULT_LANGFLOW_BASE_URL)),
        api_key=os.getenv("LANGFLOW_API_KEY", "").strip() or None,
        bearer_token=os.getenv("LANGFLOW_BEARER_TOKEN", "").strip() or None,
        timeout_seconds=_env_float("LANGFLOW_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
        verify_ssl=_env_flag("LANGFLOW_VERIFY_SSL", True),
    )


def _resolve_host() -> str:
    return os.getenv("MCP_HOST", DEFAULT_HTTP_HOST).strip() or DEFAULT_HTTP_HOST


def _resolve_port() -> int:
    return _env_int("PORT", _env_int("MCP_PORT", DEFAULT_HTTP_PORT))


def _resolve_transport(force_stdio: bool) -> str:
    if force_stdio:
        return "stdio"

    transport = os.getenv("MCP_TRANSPORT", "streamable-http").strip().lower()
    if transport not in VALID_TRANSPORTS:
        raise ValueError(
            f"MCP_TRANSPORT must be one of {', '.join(sorted(VALID_TRANSPORTS))}."
        )
    return transport


SETTINGS = load_settings()


mcp = FastMCP("Langflow MCP", host=_resolve_host(), port=_resolve_port())


def _header_safe_variable_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip())
    if not cleaned:
        raise ValueError("Global variable names cannot be blank.")
    return cleaned.upper()


def _build_headers(
    *,
    accept: str = "application/json",
    extra_headers: Mapping[str, str] | None = None,
    global_variables: Mapping[str, str] | None = None,
) -> dict[str, str]:
    headers = {"Accept": accept}

    if SETTINGS.api_key:
        headers["x-api-key"] = SETTINGS.api_key

    if SETTINGS.bearer_token:
        headers["Authorization"] = f"Bearer {SETTINGS.bearer_token}"

    if extra_headers:
        for key, value in extra_headers.items():
            if value is not None:
                headers[str(key)] = str(value)

    if global_variables:
        for name, value in global_variables.items():
            headers[f"X-LANGFLOW-GLOBAL-VAR-{_header_safe_variable_name(name)}"] = str(value)

    return headers


def _normalize_path(path: str) -> str:
    return path if path.startswith("/") else f"/{path}"


def _response_preview(response: httpx.Response) -> str:
    text = response.text.strip()
    if not text:
        return ""
    return text if len(text) <= 500 else f"{text[:500]}..."


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return

    preview = _response_preview(response)
    suffix = f": {preview}" if preview else ""
    raise RuntimeError(
        f"Langflow API request failed with HTTP {response.status_code} for {response.request.method} "
        f"{response.request.url}{suffix}"
    )


def _decode_response_data(response: httpx.Response) -> Any:
    if not response.content:
        return None

    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json":
        return response.json()

    try:
        return response.json()
    except ValueError:
        return response.text


def _response_envelope(response: httpx.Response, *, data: Any | None = None) -> JsonDict:
    return {
        "status_code": response.status_code,
        "data": _decode_response_data(response) if data is None else data,
    }


def _prepare_output_path(output_path: str) -> Path:
    destination = Path(output_path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _resolve_flow_folder_filter(
    project_id: str | None = None, folder_id: str | None = None
) -> str | None:
    # Langflow's docs use both project_id and folder_id terminology for the same flow container.
    return folder_id or project_id


class LangflowClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._settings.base_url,
            timeout=self._settings.timeout_seconds,
            follow_redirects=True,
            verify=self._settings.verify_ssl,
        )

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        extra_headers: Mapping[str, str] | None = None,
        global_variables: Mapping[str, str] | None = None,
    ) -> JsonDict:
        async with self._client() as client:
            response = await client.request(
                method=method.upper(),
                url=_normalize_path(path),
                params=params,
                json=json_body,
                headers=_build_headers(
                    extra_headers=extra_headers,
                    global_variables=global_variables,
                ),
            )

        _raise_for_status(response)
        return _response_envelope(response)

    async def request_stream(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        extra_headers: Mapping[str, str] | None = None,
        global_variables: Mapping[str, str] | None = None,
    ) -> JsonDict:
        async with self._client() as client:
            async with client.stream(
                method=method.upper(),
                url=_normalize_path(path),
                params=params,
                json=json_body,
                headers=_build_headers(
                    accept="application/json, text/event-stream",
                    extra_headers=extra_headers,
                    global_variables=global_variables,
                ),
            ) as response:
                _raise_for_status(response)
                lines = [line async for line in response.aiter_lines() if line]
                return {
                    "status_code": response.status_code,
                    "content_type": response.headers.get("content-type"),
                    "data": lines,
                }

    async def upload_file(
        self,
        path: str,
        file_path: str,
        *,
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> JsonDict:
        source = Path(file_path).expanduser()
        if not source.is_file():
            raise ValueError(f"File not found: {source}")

        media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        headers = _build_headers(
            extra_headers={**(extra_headers or {}), "Accept": "application/json"},
        )
        headers.pop("Content-Type", None)

        async with self._client() as client:
            with source.open("rb") as handle:
                response = await client.post(
                    _normalize_path(path),
                    params=params,
                    headers=headers,
                    files={"file": (source.name, handle, media_type)},
                )

        _raise_for_status(response)
        return _response_envelope(response)

    async def download_file(
        self,
        path: str,
        output_path: str,
        *,
        params: Mapping[str, Any] | None = None,
        extra_headers: Mapping[str, str] | None = None,
    ) -> JsonDict:
        destination = _prepare_output_path(output_path)

        async with self._client() as client:
            async with client.stream(
                "GET",
                _normalize_path(path),
                params=params,
                headers=_build_headers(
                    accept="application/octet-stream, application/zip, application/json",
                    extra_headers=extra_headers,
                ),
            ) as response:
                _raise_for_status(response)
                bytes_written = 0
                with destination.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        handle.write(chunk)
                        bytes_written += len(chunk)

                return {
                    "status_code": response.status_code,
                    "saved_to": str(destination.resolve()),
                    "bytes_written": bytes_written,
                    "content_type": response.headers.get("content-type"),
                }


client = LangflowClient(SETTINGS)


@mcp.tool()
async def get_server_health() -> JsonDict:
    """Check whether the Langflow server is up."""
    return await client.request_json("GET", "/health")


@mcp.tool()
async def get_server_health_check() -> JsonDict:
    """Run Langflow's deeper health check, including service dependencies."""
    return await client.request_json("GET", "/health_check")


@mcp.tool()
async def get_version() -> JsonDict:
    """Get the Langflow server version."""
    return await client.request_json("GET", "/api/v1/version")


@mcp.tool()
async def get_config() -> JsonDict:
    """Get Langflow server configuration exposed by the API."""
    return await client.request_json("GET", "/api/v1/config")


@mcp.tool()
async def list_available_components() -> JsonDict:
    """List all component types available in the connected Langflow server."""
    return await client.request_json("GET", "/api/v1/all")


@mcp.tool()
async def get_current_user() -> JsonDict:
    """Get the authenticated Langflow user."""
    return await client.request_json("GET", "/api/v1/users/whoami")


@mcp.tool()
async def list_users(skip: int = 0, limit: int = 25) -> JsonDict:
    """List Langflow users. Requires superuser privileges when auth is enabled."""
    return await client.request_json(
        "GET",
        "/api/v1/users/",
        params={"skip": skip, "limit": limit},
    )


@mcp.tool()
async def create_user(username: str, password: str) -> JsonDict:
    """Create a Langflow user."""
    return await client.request_json(
        "POST",
        "/api/v1/users/",
        json_body={"username": username, "password": password},
    )


@mcp.tool()
async def update_user(user_id: str, updates: JsonDict) -> JsonDict:
    """Update a Langflow user with fields such as is_active or is_superuser."""
    return await client.request_json(
        "PATCH",
        f"/api/v1/users/{user_id}",
        json_body=updates,
    )


@mcp.tool()
async def reset_user_password(user_id: str, password: str) -> JsonDict:
    """Reset a Langflow user's password."""
    return await client.request_json(
        "PATCH",
        f"/api/v1/users/{user_id}/reset-password",
        json_body={"password": password},
    )


@mcp.tool()
async def delete_user(user_id: str) -> JsonDict:
    """Delete a Langflow user."""
    return await client.request_json("DELETE", f"/api/v1/users/{user_id}")


@mcp.tool()
async def list_variables() -> JsonDict:
    """List Langflow global variables."""
    return await client.request_json("GET", "/api/v1/variables/")


@mcp.tool()
async def create_variable(variable: JsonDict) -> JsonDict:
    """Create a Langflow global variable. Pass the full request body as an object."""
    return await client.request_json("POST", "/api/v1/variables/", json_body=variable)


@mcp.tool()
async def update_variable(variable_id: str, variable: JsonDict) -> JsonDict:
    """Update a Langflow global variable. The body should include the variable id."""
    return await client.request_json(
        "PATCH",
        f"/api/v1/variables/{variable_id}",
        json_body=variable,
    )


@mcp.tool()
async def delete_variable(variable_id: str) -> JsonDict:
    """Delete a Langflow global variable."""
    return await client.request_json("DELETE", f"/api/v1/variables/{variable_id}")


@mcp.tool()
async def list_projects() -> JsonDict:
    """List Langflow projects."""
    return await client.request_json("GET", "/api/v1/projects/")


@mcp.tool()
async def create_project(project: JsonDict) -> JsonDict:
    """Create a Langflow project. Pass the full request body as an object."""
    return await client.request_json("POST", "/api/v1/projects/", json_body=project)


@mcp.tool()
async def get_project(project_id: str) -> JsonDict:
    """Get a Langflow project by id."""
    return await client.request_json("GET", f"/api/v1/projects/{project_id}")


@mcp.tool()
async def update_project(project_id: str, updates: JsonDict) -> JsonDict:
    """Update a Langflow project. Only provided fields are patched."""
    return await client.request_json(
        "PATCH",
        f"/api/v1/projects/{project_id}",
        json_body=updates,
    )


@mcp.tool()
async def delete_project(project_id: str) -> JsonDict:
    """Delete a Langflow project."""
    return await client.request_json("DELETE", f"/api/v1/projects/{project_id}")


@mcp.tool()
async def export_project_archive(project_id: str, output_path: str) -> JsonDict:
    """Download a Langflow project as a zip archive."""
    return await client.download_file(
        f"/api/v1/projects/download/{project_id}",
        output_path,
    )


@mcp.tool()
async def import_project_archive(file_path: str) -> JsonDict:
    """Import a Langflow project zip archive."""
    return await client.upload_file("/api/v1/projects/upload/", file_path)


@mcp.tool()
async def list_flows(
    remove_example_flows: bool = False,
    components_only: bool = False,
    get_all: bool = True,
    header_flows: bool = False,
    page: int = 1,
    size: int = 50,
    project_id: str | None = None,
    folder_id: str | None = None,
) -> JsonDict:
    """List Langflow flows, optionally filtered by folder. project_id is accepted as an alias."""
    params: dict[str, Any] = {
        "remove_example_flows": remove_example_flows,
        "components_only": components_only,
        "get_all": get_all,
        "header_flows": header_flows,
        "page": page,
        "size": size,
    }

    target_folder_id = _resolve_flow_folder_filter(project_id=project_id, folder_id=folder_id)
    if target_folder_id:
        params["folder_id"] = target_folder_id

    return await client.request_json("GET", "/api/v1/flows/", params=params)


@mcp.tool()
async def get_flow(flow_id: str) -> JsonDict:
    """Get a Langflow flow by id."""
    return await client.request_json("GET", f"/api/v1/flows/{flow_id}")


@mcp.tool()
async def create_flow(flow: JsonDict) -> JsonDict:
    """Create a Langflow flow. Pass the full request body as an object."""
    return await client.request_json("POST", "/api/v1/flows/", json_body=flow)


@mcp.tool()
async def create_flows_batch(flows: list[JsonDict]) -> JsonDict:
    """Create multiple Langflow flows in one request."""
    return await client.request_json(
        "POST",
        "/api/v1/flows/batch/",
        json_body={"flows": flows},
    )


@mcp.tool()
async def list_sample_flows() -> JsonDict:
    """List Langflow sample flows."""
    return await client.request_json("GET", "/api/v1/flows/basic_examples/")


@mcp.tool()
async def update_flow(flow_id: str, updates: JsonDict) -> JsonDict:
    """Update a Langflow flow. Pass only the fields you want to change."""
    return await client.request_json(
        "PATCH",
        f"/api/v1/flows/{flow_id}",
        json_body=updates,
    )


@mcp.tool()
async def delete_flow(flow_id: str) -> JsonDict:
    """Delete a Langflow flow."""
    return await client.request_json("DELETE", f"/api/v1/flows/{flow_id}")


@mcp.tool()
async def delete_multiple_flows(flow_ids: list[str]) -> JsonDict:
    """Delete multiple Langflow flows by id."""
    return await client.request_json("DELETE", "/api/v1/flows/", json_body=flow_ids)


@mcp.tool()
async def export_flows_archive(flow_ids: list[str], output_path: str) -> JsonDict:
    """Export selected Langflow flows to a zip archive."""
    destination = _prepare_output_path(output_path)

    async with client._client() as http_client:
        async with http_client.stream(
            "POST",
            "/api/v1/flows/download/",
            json=flow_ids,
            headers=_build_headers(),
        ) as response:
            _raise_for_status(response)
            bytes_written = 0
            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    handle.write(chunk)
                    bytes_written += len(chunk)

            return {
                "status_code": response.status_code,
                "saved_to": str(destination.resolve()),
                "bytes_written": bytes_written,
                "content_type": response.headers.get("content-type"),
            }


@mcp.tool()
async def import_flow_json(
    file_path: str,
    folder_id: str | None = None,
    project_id: str | None = None,
) -> JsonDict:
    """Import a Langflow flow JSON file into an existing folder. project_id is accepted as an alias."""
    target_folder_id = _resolve_flow_folder_filter(project_id=project_id, folder_id=folder_id)
    params = {"folder_id": target_folder_id} if target_folder_id else None
    return await client.upload_file("/api/v1/flows/upload/", file_path, params=params)


@mcp.tool()
async def run_flow(
    flow_id_or_name: str,
    payload: JsonDict,
    stream: bool = False,
    global_variables: dict[str, str] | None = None,
) -> JsonDict:
    """Run a Langflow flow by id or name."""
    if stream:
        return await client.request_stream(
            "POST",
            f"/api/v1/run/{flow_id_or_name}",
            params={"stream": "true"},
            json_body=payload,
            global_variables=global_variables,
        )

    return await client.request_json(
        "POST",
        f"/api/v1/run/{flow_id_or_name}",
        params={"stream": "false"},
        json_body=payload,
        global_variables=global_variables,
    )


@mcp.tool()
async def run_flow_webhook(
    flow_id_or_name: str,
    body: JsonDict | None = None,
    global_variables: dict[str, str] | None = None,
) -> JsonDict:
    """Trigger a Langflow webhook-enabled flow."""
    return await client.request_json(
        "POST",
        f"/api/v1/webhook/{flow_id_or_name}",
        json_body=body or {},
        global_variables=global_variables,
    )


@mcp.tool()
async def start_flow_build(
    flow_id: str,
    payload: JsonDict | None = None,
    event_delivery: str = "polling",
    flow_name: str | None = None,
    log_builds: bool = True,
    stop_component_id: str | None = None,
    start_component_id: str | None = None,
) -> JsonDict:
    """Start a Langflow visual-editor build job for a flow."""
    params: dict[str, Any] = {
        "event_delivery": event_delivery,
        "log_builds": log_builds,
    }
    if flow_name:
        params["flow_name"] = flow_name
    if stop_component_id:
        params["stop_component_id"] = stop_component_id
    if start_component_id:
        params["start_component_id"] = start_component_id

    return await client.request_json(
        "POST",
        f"/api/v1/build/{flow_id}/flow",
        params=params,
        json_body=payload or {},
    )


@mcp.tool()
async def get_build_events(job_id: str, event_delivery: str = "polling") -> JsonDict:
    """Get build events for a Langflow build job."""
    if event_delivery == "streaming":
        return await client.request_stream(
            "GET",
            f"/api/v1/build/{job_id}/events",
            params={"event_delivery": event_delivery},
        )

    return await client.request_json(
        "GET",
        f"/api/v1/build/{job_id}/events",
        params={"event_delivery": event_delivery},
    )


@mcp.tool()
async def cancel_build(job_id: str) -> JsonDict:
    """Cancel a Langflow build job."""
    return await client.request_json("POST", f"/api/v1/build/{job_id}/cancel")


@mcp.tool()
async def upload_user_file(file_path: str) -> JsonDict:
    """Upload a reusable user-scoped file to Langflow's v2 files API."""
    return await client.upload_file("/api/v2/files", file_path)


@mcp.tool()
async def list_user_files() -> JsonDict:
    """List reusable user-scoped files from Langflow's v2 files API."""
    return await client.request_json("GET", "/api/v2/files")


@mcp.tool()
async def download_user_file(file_id: str, output_path: str) -> JsonDict:
    """Download a reusable user-scoped file by id."""
    return await client.download_file(f"/api/v2/files/{file_id}", output_path)


@mcp.tool()
async def rename_user_file(file_id: str, new_name: str) -> JsonDict:
    """Rename a reusable user-scoped file."""
    return await client.request_json(
        "PUT",
        f"/api/v2/files/{file_id}",
        params={"name": new_name},
    )


@mcp.tool()
async def delete_user_file(file_id: str) -> JsonDict:
    """Delete a reusable user-scoped file."""
    return await client.request_json("DELETE", f"/api/v2/files/{file_id}")


@mcp.tool()
async def download_user_files_batch(file_ids: list[str], output_path: str) -> JsonDict:
    """Download multiple reusable user-scoped files as a zip archive."""
    destination = _prepare_output_path(output_path)

    async with client._client() as http_client:
        async with http_client.stream(
            "POST",
            "/api/v2/files/batch/",
            json=file_ids,
            headers=_build_headers(),
        ) as response:
            _raise_for_status(response)
            bytes_written = 0
            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes():
                    if not chunk:
                        continue
                    handle.write(chunk)
                    bytes_written += len(chunk)

            return {
                "status_code": response.status_code,
                "saved_to": str(destination.resolve()),
                "bytes_written": bytes_written,
                "content_type": response.headers.get("content-type"),
            }


@mcp.tool()
async def delete_user_files_batch(file_ids: list[str]) -> JsonDict:
    """Delete multiple reusable user-scoped files by id."""
    return await client.request_json("DELETE", "/api/v2/files/batch/", json_body=file_ids)


@mcp.tool()
async def delete_all_user_files() -> JsonDict:
    """Delete all reusable user-scoped files."""
    return await client.request_json("DELETE", "/api/v2/files")


@mcp.tool()
async def upload_flow_file(flow_id: str, file_path: str) -> JsonDict:
    """Upload a flow-scoped file through the v1 files API."""
    return await client.upload_file(f"/api/v1/files/upload/{flow_id}", file_path)


@mcp.tool()
async def list_flow_files(flow_id: str) -> JsonDict:
    """List files attached to a specific flow in the v1 files API."""
    return await client.request_json("GET", f"/api/v1/files/list/{flow_id}")


@mcp.tool()
async def download_flow_file(flow_id: str, filename: str, output_path: str) -> JsonDict:
    """Download a specific flow-scoped file."""
    return await client.download_file(
        f"/api/v1/files/download/{flow_id}/{filename}",
        output_path,
    )


@mcp.tool()
async def delete_flow_file(flow_id: str, filename: str) -> JsonDict:
    """Delete a specific flow-scoped file."""
    return await client.request_json("DELETE", f"/api/v1/files/delete/{flow_id}/{filename}")


@mcp.tool()
async def raw_api_request(
    method: str,
    path: str,
    query_params: dict[str, Any] | None = None,
    json_body: JsonDict | None = None,
    extra_headers: dict[str, str] | None = None,
    global_variables: dict[str, str] | None = None,
) -> JsonDict:
    """Call any Langflow JSON endpoint directly when a dedicated tool is not enough."""
    return await client.request_json(
        method,
        path,
        params=query_params,
        json_body=json_body,
        extra_headers=extra_headers,
        global_variables=global_variables,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Langflow MCP server")
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Run over stdio instead of the configured HTTP transport.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    transport = _resolve_transport(args.stdio)
    mcp.run(transport=transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
