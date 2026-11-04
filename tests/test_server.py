import unittest
import tempfile
from unittest.mock import AsyncMock, Mock, patch
from pathlib import Path

from copilot_cost_management import server
from copilot_cost_management.auth import DELEGATED_SCOPES, TokenProvider
from copilot_cost_management.finops import FinOpsStore
from copilot_cost_management.server import recommend_policy_changes


class TenantCreditBalanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_balance_request_uses_delegated_authentication(self):
        expected = {"value": []}
        with patch.object(server.graph, "request", new_callable=AsyncMock, return_value=expected) as request:
            result = await server.get_tenant_credit_balance()

        self.assertEqual(result, expected)
        request.assert_awaited_once_with(
            "GET", "copilot/costManagement/getTenantCreditBalance", delegated=True
        )

    def test_interactive_start_returns_prompt_without_waiting_for_sign_in(self):
        provider = TokenProvider()
        app = Mock()
        app.get_accounts.return_value = []
        app.acquire_token_interactive.return_value = {"access_token": "test-token"}

        with patch("copilot_cost_management.auth.settings.auth_mode", "interactive"), \
             patch.object(provider, "_public_client", return_value=app):
            result = provider.start_delegated_login()
            provider._login_thread.join(timeout=1)
            status = provider.delegated_login_status()

        self.assertEqual(result["status"], "pending")
        self.assertIn("browser", result["message"])
        self.assertEqual(status["status"], "authenticated")
        app.acquire_token_interactive.assert_called_once_with(
            scopes=DELEGATED_SCOPES,
            prompt="select_account",
        )

    async def test_policy_recommendations_use_service_balance_rows(self):
        rows = [
            {
                "serviceId": "cowork",
                "consumedQuantity": 1800,
                "effectiveCapacityQuantity": {"value": 2000, "unit": "copilotCredit"},
                "remainingQuantity": {"value": 200, "unit": "copilotCredit"},
            }
        ]
        with patch.object(server, "list_user_service_balances", new=AsyncMock(return_value=rows)):
            result = await recommend_policy_changes(["user@example.com"])

        self.assertEqual(result[0]["status"], "analyzed")
        self.assertEqual(result[0]["services"][0]["consumption_percent"], 90.0)
        self.assertIn("review usage trend", result[0]["services"][0]["recommendation"])

    async def test_tenant_snapshot_is_paged_and_resumable(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            snapshot_store = FinOpsStore(Path(temp_dir) / "snapshot.db")
            directory_user = {
                "id": "user-1",
                "userPrincipalName": "alex@example.com",
                "displayName": "Alex",
                "department": "Engineering",
                "costCenter": "CC-10",
            }
            collected_user = {
                **directory_user,
                "groups": [],
                "services": [],
                "error": None,
                "membership_error": None,
            }
            with patch.object(server, "finops_store", snapshot_store), \
                 patch.object(
                     server.graph,
                     "request",
                     new=AsyncMock(
                         side_effect=[
                             {"value": [directory_user], "@odata.nextLink": "https://graph.microsoft.com/beta/users?$skiptoken=next"},
                             {"value": []},
                         ]
                     ),
                 ) as request, \
                 patch.object(
                     server,
                     "_collect_finops_user",
                     new=AsyncMock(return_value=collected_user),
                 ):
                first = await server.capture_tenant_finops_snapshot()
                second = await server.capture_tenant_finops_snapshot()

            self.assertEqual(first["status"], "in_progress")
            self.assertTrue(first["next_page_available"])
            self.assertEqual(second["status"], "completed")
            self.assertEqual(second["users_processed"], 1)
            self.assertIn("$skiptoken=next", request.await_args_list[1].args[1])


if __name__ == "__main__":
    unittest.main()
