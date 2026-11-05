import base64
import json
import unittest
from unittest.mock import Mock, patch

from copilot_cost_management.auth import (
    DelegatedAuthenticationError,
    TokenProvider,
    graph_access_token,
)
from copilot_cost_management.http_auth import EntraOBOAuthMiddleware


def _token(scopes: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"scp": scopes}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return ".".join(("header", payload, "signature"))


class EntraOBOAuthMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _invoke(self, middleware, headers=()):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/mcp",
                "headers": list(headers),
            },
            receive,
            send,
        )
        return sent

    async def test_rejects_missing_bearer_token(self):
        exchange = Mock()
        downstream = Mock()

        async def app(*args):
            downstream()

        sent = await self._invoke(EntraOBOAuthMiddleware(app, exchange))

        self.assertEqual(sent[0]["status"], 401)
        exchange.assert_not_called()
        downstream.assert_not_called()

    async def test_rejects_token_missing_required_scope(self):
        exchange = Mock(return_value="graph-token")
        downstream = Mock()

        async def app(*args):
            downstream()

        sent = await self._invoke(
            EntraOBOAuthMiddleware(app, exchange, "Mcp.Access"),
            [(b"authorization", ("Bearer " + _token("openid")).encode("ascii"))],
        )

        self.assertEqual(sent[0]["status"], 403)
        exchange.assert_called_once()
        downstream.assert_not_called()

    async def test_rejects_expired_or_untrusted_assertion(self):
        async def app(*args):
            self.fail("Unauthenticated request reached the MCP app.")

        def exchange(_):
            raise DelegatedAuthenticationError("invalid_grant", "invalid user assertion")

        sent = await self._invoke(
            EntraOBOAuthMiddleware(app, exchange),
            [
                (
                    b"authorization",
                    ("Bearer " + _token("openid Mcp.Access")).encode("ascii"),
                )
            ],
        )

        self.assertEqual(sent[0]["status"], 401)

    async def test_sets_and_resets_graph_token_for_authenticated_request(self):
        exchange = Mock(return_value="graph-token")
        observed_tokens = []

        async def app(scope, receive, send):
            observed_tokens.append(graph_access_token.get())
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        sent = await self._invoke(
            EntraOBOAuthMiddleware(app, exchange, "Mcp.Access"),
            [
                (
                    b"authorization",
                    ("Bearer " + _token("openid Mcp.Access")).encode("ascii"),
                )
            ],
        )

        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(observed_tokens, ["graph-token"])
        self.assertIsNone(graph_access_token.get())


class TokenProviderOBOTests(unittest.TestCase):
    def test_exchanges_assertion_for_graph_default_scope(self):
        provider = TokenProvider()
        client = Mock()
        client.acquire_token_on_behalf_of.return_value = {"access_token": "graph-token"}

        with patch("copilot_cost_management.auth.settings.client_secret", "test-secret"), \
             patch("copilot_cost_management.auth.msal.ConfidentialClientApplication", return_value=client):
            token = provider.exchange_for_graph_token("user-assertion")

        self.assertEqual(token, "graph-token")
        client.acquire_token_on_behalf_of.assert_called_once_with(
            user_assertion="user-assertion",
            scopes=["https://graph.microsoft.com/.default"],
        )


if __name__ == "__main__":
    unittest.main()
