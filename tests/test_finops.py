import tempfile
import unittest
from pathlib import Path

from copilot_cost_management.finops import (
    FinOpsStore,
    build_chargeback,
    build_forecast,
    build_outlier_report,
    build_period_report,
    build_snapshot_report,
)


class FinOpsAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        database = Path(self.temp_dir.name) / "finops.db"
        self.store = FinOpsStore(database)

    def test_period_runs_include_latest_baseline_and_requested_completed_runs(self):
        run_ids = []
        for day in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"):
            run = self.store.start_or_resume_run()
            self.store.save_page(run["id"], [], None)
            with self.store._connect() as connection:
                connection.execute(
                    "UPDATE finops_runs SET started_at=? WHERE id=?",
                    (f"{day}T12:00:00+00:00", run["id"]),
                )
            run_ids.append(run["id"])

        selected = self.store.period_runs(
            "2026-10-02T00:00:00+00:00",
            "2026-10-03T23:59:59.999999+00:00",
        )

        self.assertEqual(selected, run_ids[:3])

    def test_snapshot_store_and_department_chargeback_use_service_consumption(self):
        run = self.store.start_or_resume_run()
        user = {
            "id": "user-1",
            "userPrincipalName": "alex@example.com",
            "displayName": "Alex",
            "department": "Engineering",
            "costCenter": "CC-10",
            "groups": [{"id": "group-1", "displayName": "Engineering"}],
            "services": [
                {
                    "serviceId": "cowork",
                    "consumed": 154.796961,
                    "remaining": 1845.203039,
                    "effective_capacity": 2000.0,
                    "per_user_limit": 2000.0,
                    "period_limit": 33000.0,
                    "is_consumption_allowed": True,
                    "access_decision_reason": "allowed",
                    "source_modified": "2026-10-01T00:00:00Z",
                }
            ],
        }
        run = self.store.save_page(run["id"], [user], None)
        loaded_run, users = self.store.snapshot_rows(run["id"])

        report = build_snapshot_report(loaded_run, users)
        chargeback = build_chargeback(loaded_run, users, "department")

        self.assertEqual(report["users_processed"], 1)
        self.assertAlmostEqual(report["reported_consumption_credits_across_services"], 154.796961)
        self.assertEqual(report["top_users"][0]["services_near_limit"], 0)
        self.assertEqual(chargeback["allocations"][0]["name"], "Engineering")
        self.assertAlmostEqual(
            chargeback["allocations"][0]["consumed_credits_across_services"], 154.796961
        )
        self.assertEqual(run["status"], "completed")

    def test_group_chargeback_discloses_overlapping_membership(self):
        run = {"id": "run-1", "status": "completed", "started_at": "2026-10-01T00:00:00+00:00"}
        user = {
            "user_id": "user-1",
            "upn": "alex@example.com",
            "department": None,
            "cost_center": None,
            "error": None,
            "membership_error": None,
            "groups": [
                {"id": "g1", "displayName": "Group A"},
                {"id": "g2", "displayName": "Group B"},
            ],
            "services": [{"service_id": "cowork", "consumed": 10.0}],
        }
        report = build_chargeback(run, [user], "group")

        self.assertEqual(len(report["allocations"]), 2)
        self.assertTrue(any("non-additive" in item for item in report["limitations"]))

    def test_service_report_filters_cowork_and_lists_near_limit_users(self):
        run = {
            "id": "run-1",
            "status": "completed",
            "started_at": "2026-10-01T00:00:00+00:00",
            "completed_at": "2026-10-01T00:01:00+00:00",
            "user_count": 2,
            "failed_count": 0,
        }
        users = [
            {
                "user_id": "user-1",
                "upn": "alex@example.com",
                "display_name": "Alex",
                "department": "Engineering",
                "cost_center": None,
                "error": None,
                "membership_error": None,
                "services": [
                    {
                        "service_id": "Cowork",
                        "consumed": 90.0,
                        "remaining": 10.0,
                        "effective_capacity": 100.0,
                    },
                    {"service_id": "Other", "consumed": 1000.0},
                ],
            },
            {
                "user_id": "user-2",
                "upn": "sam@example.com",
                "display_name": "Sam",
                "department": "Engineering",
                "cost_center": None,
                "error": None,
                "membership_error": None,
                "services": [
                    {
                        "service_id": "Cowork",
                        "consumed": 10.0,
                        "remaining": 90.0,
                        "effective_capacity": 100.0,
                    }
                ],
            },
        ]

        report = build_snapshot_report(
            run, users, service_id="Cowork", near_limit_percent=80
        )

        self.assertEqual(report["scope_service_id"], "Cowork")
        self.assertEqual(report["reported_consumption_credits"], 100.0)
        self.assertEqual([row["service_id"] for row in report["services"]], ["Cowork"])
        self.assertEqual(report["users_near_limit_count"], 1)
        self.assertEqual(report["users_near_limit"][0]["user_principal_name"], "alex@example.com")
        self.assertEqual(report["users_near_limit"][0]["services"][0]["consumption_percent"], 90.0)

    def test_chargeback_can_be_filtered_to_a_service(self):
        run = {
            "id": "run-1",
            "status": "completed",
            "started_at": "2026-10-01T00:00:00+00:00",
        }
        user = {
            "user_id": "user-1",
            "upn": "alex@example.com",
            "department": "Engineering",
            "cost_center": None,
            "error": None,
            "membership_error": None,
            "groups": [],
            "services": [
                {"service_id": "Cowork", "consumed": 12.0},
                {"service_id": "Other", "consumed": 88.0},
            ],
        }

        report = build_chargeback(run, [user], "department", service_id="Cowork")

        self.assertEqual(report["service_id"], "Cowork")
        self.assertEqual(
            report["allocations"][0]["consumed_credits_across_services"], 12.0
        )
        self.assertEqual(report["allocations"][0]["services"], [
            {"service_id": "Cowork", "consumed_credits": 12.0}
        ])

    def test_unusual_consumers_use_a_tenant_wide_iqr_fence(self):
        run = {
            "id": "run-1",
            "status": "completed",
            "started_at": "2026-10-01T00:00:00+00:00",
        }
        users = [
            {
                "user_id": f"user-{index}",
                "upn": f"user{index}@example.com",
                "display_name": f"User {index}",
                "department": "Engineering",
                "cost_center": None,
                "error": None,
                "services": [
                    {"service_id": "Cowork", "consumed": credits}
                ],
            }
            for index, credits in enumerate((10.0, 10.0, 10.0, 10.0, 100.0))
        ]

        report = build_outlier_report(run, users, service_id="Cowork")

        self.assertEqual(report["status"], "outliers_found")
        self.assertEqual(report["sample_size"], 5)
        self.assertEqual(len(report["outliers"]), 1)
        self.assertEqual(report["outliers"][0]["user_principal_name"], "user4@example.com")

    def test_forecast_requires_sufficient_history_and_uses_credit_units(self):
        snapshots = {}
        ids = []
        for day, consumed in ((1, 100.0), (8, 170.0), (15, 240.0)):
            snapshot_id = f"run-{day}"
            ids.append(snapshot_id)
            snapshots[snapshot_id] = (
                {
                    "id": snapshot_id,
                    "started_at": f"2026-10-{day:02d}T00:00:00+00:00",
                },
                [
                    {
                        "user_id": "user-1",
                        "upn": "alex@example.com",
                        "department": "Engineering",
                        "cost_center": "CC-10",
                        "groups": [],
                        "error": None,
                        "membership_error": None,
                        "services": [
                            {
                                "service_id": "cowork",
                                "consumed": consumed,
                                "source_modified": f"2026-10-{day:02d}T00:00:00Z",
                            }
                        ],
                    }
                ],
            )

        forecast = build_forecast(
            ids,
            snapshots,
            dimension="tenant",
            dimension_value=None,
            service_id="cowork",
            horizon_days=30,
        )

        self.assertEqual(forecast["status"], "estimated")
        self.assertEqual(forecast["units"], "copilotCredit")
        self.assertAlmostEqual(forecast["estimated_daily_change_credits"], 10.0)
        self.assertAlmostEqual(forecast["projected_credits_after_horizon"], 540.0)
        self.assertTrue(any("not a dollar forecast" in item for item in forecast["warnings"]))

    def test_period_report_diffs_snapshots_and_flags_counter_resets(self):
        snapshots = {}
        ids = []
        samples = (
            ("2026-10-01", 100.0, 20.0),
            ("2026-10-02", 120.0, 25.0),
            ("2026-10-03", 10.0, 30.0),
        )
        for day, first_user_credits, second_user_credits in samples:
            snapshot_id = f"run-{day}"
            ids.append(snapshot_id)
            snapshots[snapshot_id] = (
                {
                    "id": snapshot_id,
                    "started_at": f"{day}T12:00:00+00:00",
                    "status": "completed",
                    "user_count": 2,
                    "failed_count": 0,
                },
                [
                    {
                        "user_id": "user-1",
                        "upn": "alex@example.com",
                        "display_name": "Alex",
                        "department": "Engineering",
                        "cost_center": "CC-10",
                        "groups": [{"id": "group-1", "displayName": "Engineering"}],
                        "error": None,
                        "membership_error": None,
                        "services": [
                            {
                                "service_id": "cowork",
                                "consumed": first_user_credits,
                                "source_modified": f"{day}T00:00:00Z",
                            }
                        ],
                    },
                    {
                        "user_id": "user-2",
                        "upn": "sam@example.com",
                        "display_name": "Sam",
                        "department": "Engineering",
                        "cost_center": "CC-10",
                        "groups": [{"id": "group-1", "displayName": "Engineering"}],
                        "error": None,
                        "membership_error": None,
                        "services": [
                            {
                                "service_id": "cowork",
                                "consumed": second_user_credits,
                                "source_modified": f"{day}T00:00:00Z",
                            }
                        ],
                    },
                ],
            )

        report = build_period_report(
            ids,
            snapshots,
            start_date="2026-10-02",
            end_date="2026-10-03",
            dimension="department",
            dimension_value="Engineering",
            service_id="cowork",
            top_n=10,
        )

        self.assertEqual(report["status"], "complete_with_quality_warnings")
        self.assertEqual(report["observed_credit_increase"], 40.0)
        self.assertEqual(report["services"], [
            {"service_id": "cowork", "observed_credit_increase": 40.0}
        ])
        self.assertEqual(report["allocations"][0]["name"], "Engineering")
        self.assertEqual(report["allocations"][0]["user_count"], 2)
        self.assertEqual(report["data_quality"]["counter_resets"], 1)
        self.assertEqual(
            [row["consumed_credits"] for row in report["daily_observed_increases"]],
            [25.0, 15.0],
        )

    def test_period_report_requires_a_baseline_snapshot(self):
        snapshot_id = "run-2026-10-02"
        report = build_period_report(
            [snapshot_id],
            {
                snapshot_id: (
                    {"id": snapshot_id, "started_at": "2026-10-02T12:00:00+00:00"},
                    [],
                )
            },
            start_date="2026-10-02",
            end_date="2026-10-03",
            dimension="tenant",
            dimension_value=None,
            service_id=None,
            top_n=10,
        )

        self.assertEqual(report["status"], "insufficient_history")
        self.assertIn("baseline snapshot", report["message"])


if __name__ == "__main__":
    unittest.main()
