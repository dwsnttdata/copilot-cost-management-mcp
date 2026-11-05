from copilot_cost_management.config import settings
from copilot_cost_management.server import azure_mcp_app as app

if settings.auth_mode.lower().strip() != "obo":
    raise RuntimeError("The Azure Functions host requires AUTH_MODE=obo.")
