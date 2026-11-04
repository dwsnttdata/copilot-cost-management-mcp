from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    tenant_id: str
    client_id: str
    client_secret: str | None = None
    auth_mode: str = "client_credentials"
    graph_base_url: str = "https://graph.microsoft.com/beta"
    mcp_transport: str = "stdio"
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8000
    approval_ttl_minutes: int = 30
    finops_max_users_per_snapshot: int = Field(default=5000, gt=0, le=100000)
    audit_db_path: str = "./copilot_cost_management.db"
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def db_path(self) -> Path:
        return Path(self.audit_db_path).resolve()

settings = Settings()
