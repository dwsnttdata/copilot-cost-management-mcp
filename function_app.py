import azure.functions as func

from copilot_cost_management.config import settings
from copilot_cost_management.http_auth import EntraOBOAuthMiddleware
from copilot_cost_management.server import mcp

if settings.auth_mode.lower().strip() != "obo":
    raise RuntimeError("The Azure Functions host requires AUTH_MODE=obo.")

app = func.AsgiFunctionApp(
    app=EntraOBOAuthMiddleware(mcp.streamable_http_app()),
    http_auth_level=func.AuthLevel.ANONYMOUS,
)
