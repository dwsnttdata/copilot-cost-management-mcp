from copilot_cost_management.config import settings
from copilot_cost_management.server import web_mcp

if settings.auth_mode.lower().strip() != "obo":
    raise RuntimeError("The Azure Web App requires AUTH_MODE=obo.")

app = web_mcp.streamable_http_app()
