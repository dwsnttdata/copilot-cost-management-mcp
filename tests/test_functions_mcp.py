import base64
import json
import unittest
from unittest.mock import AsyncMock, patch

from copilot_cost_management.auth import graph_access_token
from copilot_cost_management.server import (
    _get_user_access_token,
    _get_access_token_scopes,
    azure_mcp_app,
)

FUNCTIONS = azure_mcp_app.get_functions()


def _access_token(scopes):
    payload = base64.urlsafe_b64encode(
        json.dumps({"scp": scopes}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return ".".join(("header", payload, "signature"))


class FunctionsMCPRegistrationTests(unittest.IsolatedAsyncioTestCase):
    def test_graph_tools_are_registered_as_mcp_triggers(self):
        functions = {
            function.get_function_name(): function
            for function in FUNCTIONS
        }

        self.assertIn("get_spending_policy", functions)
        self.assertNotIn("start_delegated_login", functions)
        self.assertEqual(len(functions), 39)

        definition = json.loads(functions["get_spending_policy"].get_function_json())
        trigger = definition["bindings"][0]
        self.assertEqual(trigger["type"], "mcpToolTrigger")
        self.assertEqual(trigger["toolName"], "get_spending_policy")
        self.assertEqual(
            json.loads(trigger["toolProperties"]),
            [
                {
                    "propertyName": "policy_id",
                    "propertyType": "string",
                    "description": "",
                    "isArray": False,
                    "isRequired": True,
                }
            ],
        )

    def test_user_token_is_read_from_easy_auth_context_headers(self):
        self.assertEqual(
            _get_user_access_token(
                {
                    "transport": {
                        "properties": {
                            "headers": {
                                "X-MS-TOKEN-AAD-ACCESS-TOKEN": "easy-auth-token",
                            }
                        }
                    }
                }
            ),
            "easy-auth-token",
        )

    def test_authorization_header_is_supported_as_fallback(self):
        self.assertEqual(
            _get_user_access_token(
                {
                    "transport": {
                        "properties": {
                            "headers": {"authorization": "Bearer user-token"}
                        }
                    }
                }
            ),
            "user-token",
        )

    def test_missing_user_token_fails_explicitly(self):
        with self.assertRaisesRegex(PermissionError, "Microsoft Entra authentication headers"):
            _get_user_access_token({})

    def test_reads_requested_scope_from_signed_in_user_token(self):
        self.assertEqual(
            _get_access_token_scopes(_access_token("Mcp.Access other.scope")),
            {"Mcp.Access", "other.scope"},
        )

    async def test_native_trigger_passes_headers_and_uses_graph_obo_token(self):
        function = next(
            item
            for item in FUNCTIONS
            if item.get_function_name() == "get_spending_policy"
        )
        handler = function.get_user_function()
        result = {"id": "policy-1", "displayName": "Test"}
        context = {
            "arguments": {"policy_id": "policy-1"},
            "transport": {
                "properties": {
                    "headers": {
                        "X-MS-TOKEN-AAD-ACCESS-TOKEN": _access_token("Mcp.Access"),
                    }
                }
            },
        }

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
            response = await handler(json.dumps(context))

        exchange.assert_called_once_with(_access_token("Mcp.Access"))
        self.assertEqual(response, json.dumps(result))
        self.assertIsNone(graph_access_token.get())

    async def test_native_trigger_rejects_token_without_mcp_scope(self):
        function = next(
            item
            for item in FUNCTIONS
            if item.get_function_name() == "get_spending_policy"
        )
        handler = function.get_user_function()
        context = {
            "arguments": {"policy_id": "policy-1"},
            "transport": {
                "properties": {
                    "headers": {
                        "X-MS-TOKEN-AAD-ACCESS-TOKEN": _access_token("openid"),
                    }
                }
            },
        }

        with patch(
            "copilot_cost_management.server.token_provider.exchange_for_graph_token",
            return_value="graph-token",
        ) as exchange:
            with self.assertRaisesRegex(PermissionError, "Mcp.Access"):
                await handler(json.dumps(context))
        exchange.assert_not_called()


if __name__ == "__main__":
    unittest.main()
