from __future__ import annotations
from contextvars import ContextVar
from threading import Lock, Thread
import msal
from .config import settings

graph_access_token: ContextVar[str | None] = ContextVar("graph_access_token", default=None)

DELEGATED_SCOPES = [
    "https://graph.microsoft.com/CopilotCostManagement.Read.All",
    "https://graph.microsoft.com/CopilotCostManagement-Policy.Read.All",
    "https://graph.microsoft.com/CopilotCostManagement-Policy.ReadWrite.All",
    "https://graph.microsoft.com/CopilotCostManagement-Assignment.Read.All",
    "https://graph.microsoft.com/CopilotCostManagement-Assignment.ReadWrite.All",
    "https://graph.microsoft.com/CopilotCostManagement-UserData.Read.All",
    "https://graph.microsoft.com/User.Read.All",
    "https://graph.microsoft.com/Group.Read.All",
]

class TokenProvider:
    def __init__(self) -> None:
        self.authority = f"https://login.microsoftonline.com/{settings.tenant_id}"
        self._cache = msal.SerializableTokenCache()
        self._login_lock = Lock()
        self._obo_lock = Lock()
        self._login_thread: Thread | None = None
        self._login_status: dict[str, str | int | None] = {"status": "not_started"}

    def _public_client(self) -> msal.PublicClientApplication:
        return msal.PublicClientApplication(
            settings.client_id, authority=self.authority, token_cache=self._cache
        )

    def start_delegated_login(self) -> dict[str, str | int | None]:
        if settings.auth_mode.lower().strip() != "interactive":
            raise PermissionError(
                "Browser sign-in is only available in local interactive mode. "
                "The hosted OBO mode uses the signed-in user's MCP request."
            )

        with self._login_lock:
            if self._login_status["status"] == "pending":
                return dict(self._login_status)

            app = self._public_client()
            accounts = app.get_accounts()
            if accounts:
                result = app.acquire_token_silent(DELEGATED_SCOPES, account=accounts[0])
                if result and "access_token" in result:
                    self._login_status = {"status": "authenticated"}
                    return dict(self._login_status)

            self._login_status = {
                "status": "pending",
                "message": "A Microsoft sign-in page is opening in your browser.",
            }
            self._login_thread = Thread(
                target=self._complete_interactive_login, args=(app,), daemon=True
            )
            self._login_thread.start()
            return dict(self._login_status)

    def _complete_interactive_login(self, app: msal.PublicClientApplication) -> None:
        try:
            result = app.acquire_token_interactive(
                scopes=DELEGATED_SCOPES,
                prompt="select_account",
            )
            if "access_token" not in result:
                raise RuntimeError(result.get("error_description", str(result)))
        except Exception as error:
            with self._login_lock:
                self._login_status = {"status": "failed", "error": str(error)}
        else:
            with self._login_lock:
                self._login_status = {"status": "authenticated"}

    def delegated_login_status(self) -> dict[str, str | int | None]:
        with self._login_lock:
            return dict(self._login_status)

    def exchange_for_graph_token(self, user_assertion: str) -> str:
        if not settings.client_secret:
            raise RuntimeError("CLIENT_SECRET is required for on-behalf-of authentication.")

        app = msal.ConfidentialClientApplication(
            settings.client_id,
            authority=self.authority,
            client_credential=settings.client_secret,
            token_cache=self._cache,
        )
        with self._obo_lock:
            result = app.acquire_token_on_behalf_of(
                user_assertion=user_assertion,
                scopes=["https://graph.microsoft.com/.default"],
            )
        if "access_token" not in result:
            error = result.get("error", "unknown_error")
            description = result.get("error_description", "Microsoft Entra rejected the token exchange.")
            if error == "invalid_grant":
                raise DelegatedAuthenticationError(error, description)
            raise RuntimeError(
                f"Microsoft Entra on-behalf-of token exchange failed ({error}): {description}"
            )
        return result["access_token"]

    def get_token(self, require_delegated: bool = False) -> str:
        mode = settings.auth_mode.lower().strip()
        if require_delegated and mode not in ("interactive", "obo"):
            raise PermissionError(
                "This operation currently requires delegated Microsoft Graph permissions. "
                "Use AUTH_MODE=interactive locally or AUTH_MODE=obo in the hosted service, "
                "grant delegated Microsoft Graph permissions, and sign in with an authorized work account."
            )
        if mode == "obo":
            token = graph_access_token.get()
            if not token:
                raise PermissionError(
                    "A signed-in user's Microsoft Graph token is available only during an authenticated MCP request."
                )
            return token
        if mode == "client_credentials":
            if not settings.client_secret:
                raise ValueError("CLIENT_SECRET is required for client_credentials authentication.")
            app = msal.ConfidentialClientApplication(
                settings.client_id, authority=self.authority,
                client_credential=settings.client_secret, token_cache=self._cache,
            )
            result = app.acquire_token_silent(["https://graph.microsoft.com/.default"], account=None)
            result = result or app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        elif mode == "interactive":
            with self._login_lock:
                status = self._login_status["status"]
                if status == "pending":
                    raise PermissionError(
                        "Delegated sign-in is pending. Complete sign-in in the browser, "
                        "then retry this operation."
                    )
                if status == "failed":
                    raise PermissionError(
                        "Delegated sign-in failed. Call start_delegated_login to try again."
                    )
                app = self._public_client()
                accounts = app.get_accounts()
                result = (
                    app.acquire_token_silent(DELEGATED_SCOPES, account=accounts[0])
                    if accounts else None
                )
                if not result or "access_token" not in result:
                    raise PermissionError(
                        "Delegated sign-in is required. Call start_delegated_login first."
                    )
        else:
            raise ValueError("AUTH_MODE must be client_credentials or interactive.")
        if "access_token" not in result:
            raise RuntimeError(result.get("error_description", str(result)))
        return result["access_token"]


class DelegatedAuthenticationError(PermissionError):
    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code

token_provider = TokenProvider()
