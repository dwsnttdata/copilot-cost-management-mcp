from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .auth import DelegatedAuthenticationError, graph_access_token, token_provider
from .config import settings

logger = logging.getLogger(__name__)
TokenExchange = Callable[[str], str]


class EntraOBOAuthMiddleware:
    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        token_exchange: TokenExchange = token_provider.exchange_for_graph_token,
        required_scope: str = settings.mcp_api_scope,
    ) -> None:
        self.app = app
        self.token_exchange = token_exchange
        self.required_scope = required_scope

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Any],
        send: Callable[..., Any],
    ) -> None:
        if scope["type"] != "http" or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return

        authorization = next(
            (value for name, value in scope.get("headers", []) if name.lower() == b"authorization"),
            b"",
        )
        scheme, _, assertion = authorization.partition(b" ")
        if scheme.lower() != b"bearer" or not assertion:
            await self._respond(send, 401, "A Microsoft Entra bearer token is required.")
            return

        try:
            graph_token = await asyncio.to_thread(
                self.token_exchange,
                assertion.decode("ascii"),
            )
            if self.required_scope not in self._scopes_from_assertion(assertion.decode("ascii")):
                await self._respond(send, 403, f"The token must include the '{self.required_scope}' delegated scope.")
                return
        except DelegatedAuthenticationError as error:
            logger.info("Microsoft Entra rejected the delegated user token (%s).", error.error_code)
            await self._respond(send, 401, "The Microsoft Entra bearer token is invalid or expired.")
            return
        except (UnicodeDecodeError, ValueError):
            await self._respond(send, 401, "The Microsoft Entra bearer token is invalid or expired.")
            return
        except Exception:
            logger.exception("On-behalf-of token exchange failed.")
            await self._respond(send, 500, "The MCP server couldn't obtain a delegated Microsoft Graph token.")
            return

        token = graph_access_token.set(graph_token)
        try:
            await self.app(scope, receive, send)
        finally:
            graph_access_token.reset(token)

    @staticmethod
    def _scopes_from_assertion(assertion: str) -> set[str]:
        parts = assertion.split(".")
        if len(parts) != 3:
            raise ValueError("The bearer token is not a JWT.")
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        scopes = claims.get("scp")
        if not isinstance(scopes, str):
            return set()
        return set(scopes.split())

    @staticmethod
    async def _respond(send: Callable[..., Any], status: int, message: str) -> None:
        body = json.dumps({"error": message}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
