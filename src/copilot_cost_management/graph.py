from __future__ import annotations
import asyncio
from typing import Any
from urllib.parse import urlparse
import httpx
from .auth import token_provider
from .config import settings

class GraphError(RuntimeError):
    pass

class GraphClient:
    async def request(self, method: str, path: str, *, delegated: bool = True,
                      json: dict[str, Any] | None = None, etag: str | None = None) -> Any:
        token = await asyncio.to_thread(token_provider.get_token, require_delegated=delegated)
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if json is not None:
            headers["Content-Type"] = "application/json"
        if etag:
            headers["If-Match"] = etag
        if path.startswith("https://"):
            url = path
            if urlparse(url).netloc.lower() != urlparse(settings.graph_base_url).netloc.lower():
                raise ValueError("Graph continuation links must use the configured Microsoft Graph host.")
        else:
            url = f"{settings.graph_base_url.rstrip('/')}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=45.0) as client:
            for attempt in range(4):
                response = await client.request(method, url, headers=headers, json=json)
                retry_after = response.headers.get("Retry-After")
                if (
                    method.upper() != "GET"
                    or response.status_code not in (429, 503, 504)
                    or attempt == 3
                ):
                    break
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
                if delay > 30:
                    break
                await asyncio.sleep(delay)
        if response.status_code >= 400:
            try:
                detail = response.json()
            except Exception:
                detail = response.text
            retry = response.headers.get("Retry-After")
            raise GraphError(f"Graph {response.status_code}; retry_after={retry}; details={detail}")
        if response.status_code == 204 or not response.content:
            return {"status": "success", "http_status": response.status_code}
        data = response.json()
        if "@odata.etag" in data:
            data["_etag"] = data["@odata.etag"]
        return data

    async def all_pages(self, path: str, *, delegated: bool = True, max_pages: int = 100) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        next_path: str | None = path
        pages = 0
        while next_path and pages < max_pages:
            result = await self.request("GET", next_path, delegated=delegated)
            rows.extend(result.get("value", []))
            next_path = result.get("@odata.nextLink")
            pages += 1
        if next_path:
            raise RuntimeError(f"Microsoft Graph pagination exceeded the configured limit of {max_pages} pages.")
        return rows

graph = GraphClient()
