import base64
import importlib
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

from copilot_cost_management.auth import graph_access_token
from copilot_cost_management.server import (
    _get_access_token_scopes,
    _get_user_access_token,
    _web_mcp_handler,
    get_spending_policy,
    web_mcp,
)


def _access_token(scopes):
    payload = base64.urlsafe_b64encode(
        json.dumps({"scp": scopes}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return ".".join(("header", payload, "signature"))


def _context(headers):
    return SimpleNamespace(
        request_context=SimpleNamespace(
            request=SimpleNamespace(headers=headers),
        )
    )


class WebAppMCPTests(unittest.IsolatedAsyncioTestCase):
    def test_graph_tools_are_registered_for_web_app(self):
        names = {tool.name for tool in web_mcp._tool_manager.list_tools()}

        self.assertEqual(len(names), 39)
        self.assertIn("get_spending_policy", names)
        self.assertNotIn("start_delegated_login", names)
        self.assertNotIn("start_daily_finops_snapshot_schedule", names)

    def test_user_token_is_read_from_easy_auth_headers(self):
        token = _access_token("Mcp.Access")
        self.assertEqual(
            _get_user_access_token(
                _context({"X-MS-TOKEN-AAD-ACCESS-TOKEN": token})
            ),
            token,
        )

    def test_authorization_header_is_supported_as_fallback(self):
        self.assertEqual(
            _get_user_access_token(
                _context({"authorization": "Bearer user-token"})
            ),
            "user-token",
        )

    def test_authorization_bearer_takes_precedence_over_easy_auth_token(self):
        bearer = _access_token("Mcp.Access")
        easy_auth = _access_token("User.Read")
        self.assertEqual(
            _get_user_access_token(
                _context(
                    {
                        "authorization": f"Bearer {bearer}",
                        "X-MS-TOKEN-AAD-ACCESS-TOKEN": easy_auth,
                    }
                )
            ),
            bearer,
        )

    def test_missing_user_token_fails_explicitly(self):
        with self.assertRaisesRegex(PermissionError, "token store"):
            _get_user_access_token(_context({}))

    def test_web_app_passes_authenticated_request_to_mcp_tool(self):
        from copilot_cost_management import server

        with patch.object(server.settings, "auth_mode", "obo"):
            app_module = importlib.import_module("web_app")

        self.assertIn("/mcp", {route.path for route in app_module.app.routes})
        user_token = _access_token("Mcp.Access")
        with patch(
            "copilot_cost_management.server.token_provider.exchange_for_graph_token",
            return_value="graph-token",
        ) as exchange, patch(
            "copilot_cost_management.server.graph.request",
            new=AsyncMock(return_value={"id": "policy-1"}),
        ):
            with TestClient(app_module.app) as client:
                headers = {
                    "Accept": "application/json, text/event-stream",
                    "Authorization": f"Bearer {user_token}",
                    "X-MS-TOKEN-AAD-ACCESS-TOKEN": user_token,
                }
                initialized = client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": "2025-03-26",
                            "capabilities": {},
                            "clientInfo": {"name": "test", "version": "1.0"},
                        },
                    },
                )
                self.assertEqual(initialized.status_code, 200, initialized.text)
                client.post(
                    "/mcp",
                    headers=headers,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
                response = client.post(
                    "/mcp",
                    headers=headers,
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": "get_spending_policy",
                            "arguments": {"policy_id": "policy-1"},
                        },
                    },
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(exchange.call_count, 1, response.text)
        exchange.assert_called_once_with(user_token)
        self.assertIn("policy-1", response.text)

    def test_reads_requested_scope_from_signed_in_user_token(self):
        self.assertEqual(
            _get_access_token_scopes(_access_token("Mcp.Access other.scope")),
            {"Mcp.Access", "other.scope"},
        )

    async def test_tool_uses_obo_graph_token_from_http_request(self):
        handler = _web_mcp_handler(get_spending_policy)
        user_token = _access_token("Mcp.Access")
        result = {"id": "policy-1", "displayName": "Test"}
        context = _context(
            {"X-MS-TOKEN-AAD-ACCESS-TOKEN": user_token}
        )

        async def graph_response(method, path):
            self.assertEqual(graph_access_token.get(), "graph-token")
            self.assertEqual(method, "GET")
            self.assertEqual(path, "copilot/costManagement/spendingPolicies/policy-1")
            return result

        with patch(
            "copilot_cost_management.server.token_provider.exchange_for_graph_token",
            return_value="graph-token",
        ) as exchange, patch(
            "copilot_cost_management.server.graph.request",
            new=AsyncMock(side_effect=graph_response),
        ):
            response = await handler(policy_id="policy-1", ctx=context)

        exchange.assert_called_once_with(user_token)
        self.assertEqual(response, json.dumps(result))
        self.assertIsNone(graph_access_token.get())

    async def test_tool_rejects_token_without_mcp_scope_before_obo(self):
        handler = _web_mcp_handler(get_spending_policy)
        context = _context(
            {
                "X-MS-TOKEN-AAD-ACCESS-TOKEN": _access_token("openid"),
            }
        )

        with patch(
            "copilot_cost_management.server.token_provider.exchange_for_graph_token",
            return_value="graph-token",
        ) as exchange:
            with self.assertRaisesRegex(PermissionError, "Mcp.Access"):
                await handler(policy_id="policy-1", ctx=context)
        exchange.assert_not_called()


if __name__ == "__main__":
    unittest.main()
