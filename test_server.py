from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import server


class LangflowServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_version_tool_routes_to_langflow_version_endpoint(self) -> None:
        with patch.object(
            server.client,
            "request_json",
            AsyncMock(return_value={"status_code": 200, "data": {"version": "1.0.0"}}),
        ) as mocked:
            content, structured = await server.mcp.call_tool("get_version", {})

        self.assertEqual(structured["data"]["version"], "1.0.0")
        self.assertEqual(content[0].text.strip(), '{\n  "status_code": 200,\n  "data": {\n    "version": "1.0.0"\n  }\n}')
        mocked.assert_awaited_once_with("GET", "/api/v1/version")

    async def test_run_flow_stream_uses_stream_request_and_passes_global_variables(self) -> None:
        with patch.object(
            server.client,
            "request_stream",
            AsyncMock(return_value={"status_code": 200, "data": ["event: message"]}),
        ) as mocked_stream, patch.object(
            server.client,
            "request_json",
            AsyncMock(),
        ) as mocked_json:
            _, structured = await server.mcp.call_tool(
                "run_flow",
                {
                    "flow_id_or_name": "demo-flow",
                    "payload": {"input_value": "hello"},
                    "stream": True,
                    "global_variables": {"tenant-id": "acme"},
                },
            )

        self.assertEqual(structured["status_code"], 200)
        mocked_json.assert_not_called()
        mocked_stream.assert_awaited_once_with(
            "POST",
            "/api/v1/run/demo-flow",
            params={"stream": "true"},
            json_body={"input_value": "hello"},
            global_variables={"tenant-id": "acme"},
        )

    async def test_list_flows_maps_project_id_alias_to_folder_id_only(self) -> None:
        with patch.object(
            server.client,
            "request_json",
            AsyncMock(return_value={"status_code": 200, "data": []}),
        ) as mocked:
            await server.list_flows(project_id="proj-123", get_all=False, page=2, size=10)

        mocked.assert_awaited_once_with(
            "GET",
            "/api/v1/flows/",
            params={
                "remove_example_flows": False,
                "components_only": False,
                "get_all": False,
                "header_flows": False,
                "page": 2,
                "size": 10,
                "folder_id": "proj-123",
            },
        )

    async def test_import_flow_json_uses_folder_alias_and_uploads_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            flow_file = Path(temp_dir) / "flow.json"
            flow_file.write_text('{"name":"Demo"}', encoding="utf-8")

            with patch.object(
                server.client,
                "upload_file",
                AsyncMock(return_value={"status_code": 200, "data": {"ok": True}}),
            ) as mocked:
                await server.import_flow_json(str(flow_file), project_id="proj-456")

        mocked.assert_awaited_once_with(
            "/api/v1/flows/upload/",
            str(flow_file),
            params={"folder_id": "proj-456"},
        )

    async def test_upload_file_rejects_missing_source_file(self) -> None:
        with self.assertRaisesRegex(ValueError, "File not found"):
            await server.client.upload_file("/api/v2/files", "/tmp/does-not-exist.txt")

    def test_build_headers_include_auth_and_global_variables(self) -> None:
        original_settings = server.SETTINGS
        server.SETTINGS = server.Settings(
            base_url="http://localhost:7860",
            api_key="api-key-123",
            bearer_token="token-456",
            timeout_seconds=30.0,
            verify_ssl=True,
        )
        try:
            headers = server._build_headers(global_variables={"tenant-id": "acme"})
        finally:
            server.SETTINGS = original_settings

        self.assertEqual(headers["x-api-key"], "api-key-123")
        self.assertEqual(headers["Authorization"], "Bearer token-456")
        self.assertEqual(headers["X-LANGFLOW-GLOBAL-VAR-TENANT-ID"], "acme")


class LangflowServerSyncTests(unittest.TestCase):
    def test_main_uses_stdio_transport_when_flag_is_set(self) -> None:
        with patch("server.parse_args") as mocked_parse_args, patch.object(server.mcp, "run") as mocked_run:
            mocked_parse_args.return_value = type("Args", (), {"stdio": True})()

            result = server.main()

        self.assertEqual(result, 0)
        mocked_run.assert_called_once_with(transport="stdio")


if __name__ == "__main__":
    unittest.main()
