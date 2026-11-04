from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import settings


class FinOpsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings.db_path
        self._init()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _init(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS finops_runs(
                    id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    next_link TEXT,
                    page_count INTEGER NOT NULL DEFAULT 0,
                    user_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS finops_users(
                    run_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    upn TEXT,
                    display_name TEXT,
                    department TEXT,
                    cost_center TEXT,
                    groups_json TEXT NOT NULL,
                    usage_as_of TEXT,
                    error TEXT,
                    membership_error TEXT,
                    PRIMARY KEY(run_id, user_id),
                    FOREIGN KEY(run_id) REFERENCES finops_runs(id)
                );
                CREATE TABLE IF NOT EXISTS finops_services(
                    run_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    service_id TEXT NOT NULL,
                    consumed REAL,
                    remaining REAL,
                    effective_capacity REAL,
                    per_user_limit REAL,
                    period_limit REAL,
                    is_consumption_allowed INTEGER,
                    access_decision_reason TEXT,
                    source_modified TEXT,
                    PRIMARY KEY(run_id, user_id, service_id),
                    FOREIGN KEY(run_id, user_id) REFERENCES finops_users(run_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_finops_runs_started
                    ON finops_runs(started_at);
                CREATE INDEX IF NOT EXISTS idx_finops_services_run_service
                    ON finops_services(run_id, service_id);
                """
            )
            run_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(finops_runs)")
            }
            if "error" not in run_columns:
                connection.execute("ALTER TABLE finops_runs ADD COLUMN error TEXT")
            user_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(finops_users)")
            }
            if "membership_error" not in user_columns:
                connection.execute(
                    "ALTER TABLE finops_users ADD COLUMN membership_error TEXT"
                )

    def start_or_resume_run(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM finops_runs WHERE status='in_progress' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row:
                return dict(row)

            run_id = str(uuid.uuid4())
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                "INSERT INTO finops_runs(id, started_at, status) VALUES(?, ?, 'in_progress')",
                (run_id, now),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM finops_runs WHERE id=?", (run_id,)
                ).fetchone()
            )

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM finops_runs WHERE id=?", (run_id,)
            ).fetchone()
        if not row:
            raise KeyError(f"FinOps snapshot {run_id} not found")
        return dict(row)

    def fail_run(self, run_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE finops_runs SET status='failed', completed_at=?, next_link=NULL, error=?
                WHERE id=? AND status='in_progress'
                """,
                (datetime.now(timezone.utc).isoformat(), error, run_id),
            )

    def save_page(
        self,
        run_id: str,
        users: list[dict[str, Any]],
        next_link: str | None,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            run = connection.execute(
                "SELECT * FROM finops_runs WHERE id=?", (run_id,)
            ).fetchone()
            if not run:
                raise KeyError(f"FinOps snapshot {run_id} not found")
            if run["status"] != "in_progress":
                raise ValueError(f"FinOps snapshot is {run['status']}, not in progress")

            for user in users:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO finops_users
                    (run_id, user_id, upn, display_name, department, cost_center,
                     groups_json, usage_as_of, error, membership_error)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        user["id"],
                        user.get("userPrincipalName"),
                        user.get("displayName"),
                        user.get("department"),
                        user.get("costCenter"),
                        json.dumps(user.get("groups", [])),
                        user.get("usage_as_of"),
                        user.get("error"),
                        user.get("membership_error"),
                    ),
                )
                connection.execute(
                    "DELETE FROM finops_services WHERE run_id=? AND user_id=?",
                    (run_id, user["id"]),
                )
                for service in user.get("services", []):
                    connection.execute(
                        """
                        INSERT INTO finops_services
                        (run_id, user_id, service_id, consumed, remaining,
                         effective_capacity, per_user_limit, period_limit,
                         is_consumption_allowed, access_decision_reason, source_modified)
                        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            user["id"],
                            service["serviceId"],
                            service.get("consumed"),
                            service.get("remaining"),
                            service.get("effective_capacity"),
                            service.get("per_user_limit"),
                            service.get("period_limit"),
                            None if service.get("is_consumption_allowed") is None
                            else int(service["is_consumption_allowed"]),
                            service.get("access_decision_reason"),
                            service.get("source_modified"),
                        ),
                    )

            page_count = run["page_count"] + 1
            user_count = run["user_count"] + len(users)
            failed_count = run["failed_count"] + sum(
                bool(user.get("error") or user.get("membership_error")) for user in users
            )
            status = "in_progress" if next_link else (
                "completed_with_errors" if failed_count else "completed"
            )
            completed_at = datetime.now(timezone.utc).isoformat() if not next_link else None
            connection.execute(
                """
                UPDATE finops_runs SET next_link=?, page_count=?, user_count=?,
                    failed_count=?, status=?, completed_at=? WHERE id=?
                """,
                (
                    next_link,
                    page_count,
                    user_count,
                    failed_count,
                    status,
                    completed_at,
                    run_id,
                ),
            )
        return self.get_run(run_id)

    def snapshot_rows(self, run_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        run = self.get_run(run_id)
        with self._connect() as connection:
            users = connection.execute(
                "SELECT * FROM finops_users WHERE run_id=? ORDER BY upn",
                (run_id,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in users:
                user = dict(row)
                user["groups"] = json.loads(user.pop("groups_json"))
                services = connection.execute(
                    """
                    SELECT service_id, consumed, remaining, effective_capacity,
                           per_user_limit, period_limit, is_consumption_allowed,
                           access_decision_reason, source_modified
                    FROM finops_services WHERE run_id=? AND user_id=?
                    ORDER BY service_id
                    """,
                    (run_id, user["user_id"]),
                ).fetchall()
                user["services"] = [dict(service) for service in services]
                result.append(user)
        return run, result

    def completed_runs(self, days: int = 90) -> list[str]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM finops_runs
                WHERE status IN ('completed', 'completed_with_errors') AND started_at >= ?
                ORDER BY started_at
                """,
                (cutoff,),
            ).fetchall()
        return [row["id"] for row in rows]

    def period_runs(self, start_at: str, end_at: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, started_at FROM finops_runs
                WHERE status IN ('completed', 'completed_with_errors')
                    AND started_at <= ?
                ORDER BY started_at
                """,
                (end_at,),
            ).fetchall()
        baseline = [
            row for row in rows if row["started_at"] < start_at
        ]
        in_period = [
            row for row in rows if row["started_at"] >= start_at
        ]
        selected = ([baseline[-1]] if baseline else []) + in_period
        return [row["id"] for row in selected]


finops_store = FinOpsStore()


def parse_period_boundary(value: str, *, end_of_day: bool = False) -> datetime:
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            boundary = datetime.combine(day, time.min, tzinfo=timezone.utc)
            return boundary + timedelta(days=1) - timedelta(microseconds=1) if end_of_day else boundary
        boundary = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as error:
        raise ValueError("Dates must be ISO 8601 dates (YYYY-MM-DD) or timezone-aware timestamps.") from error
    if boundary.tzinfo is None:
        raise ValueError("Timestamp dates must include a timezone.")
    return boundary.astimezone(timezone.utc)


def _dimension_keys(user: dict[str, Any], dimension: str) -> list[tuple[str, str]]:
    if dimension == "tenant":
        return [("tenant", "Tenant")]
    if dimension == "user":
        value = user["upn"] or user["user_id"]
        return [(value, user["display_name"] or value)]
    if dimension == "department":
        value = user["department"] or "Unassigned"
        return [(value, value)]
    if dimension == "cost_center":
        value = user["cost_center"] or "Unassigned"
        return [(value, value)]
    if user["membership_error"]:
        return []
    return [
        (
            group.get("id") or group.get("displayName"),
            group.get("displayName") or group.get("id"),
        )
        for group in user["groups"]
        if group.get("id") or group.get("displayName")
    ] or [("No direct group", "No direct group")]


def build_period_report(
    snapshot_ids: list[str],
    snapshots: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
    *,
    start_date: str,
    end_date: str,
    dimension: str,
    dimension_value: str | None,
    service_id: str | None,
    top_n: int,
) -> dict[str, Any]:
    if dimension not in {"tenant", "user", "department", "cost_center", "group"}:
        raise ValueError("dimension must be tenant, user, department, cost_center, or group")
    if dimension_value is not None and dimension not in {
        "user",
        "department",
        "cost_center",
        "group",
    }:
        raise ValueError(
            "dimension_value can only be used with user, department, cost_center, or group."
        )
    requested_start = parse_period_boundary(start_date)
    requested_end = parse_period_boundary(end_date, end_of_day=True)
    if requested_start > requested_end:
        raise ValueError("start_date must be on or before end_date.")

    ordered = sorted(
        (snapshots[snapshot_id] for snapshot_id in snapshot_ids),
        key=lambda item: item[0]["started_at"],
    )
    in_period = [
        item for item in ordered
        if requested_start <= datetime.fromisoformat(item[0]["started_at"]) <= requested_end
    ]
    baseline = [
        item for item in ordered
        if datetime.fromisoformat(item[0]["started_at"]) < requested_start
    ]
    if not baseline or not in_period:
        return {
            "status": "insufficient_history",
            "dimension": dimension,
            "dimension_value": dimension_value,
            "service_id": service_id,
            "requested_period": {"start_date": start_date, "end_date": end_date},
            "snapshots_available": len(in_period),
            "message": (
                "A completed baseline snapshot before the requested period and at least "
                "one completed snapshot within it are required."
            ),
        }

    samples = [baseline[-1], *in_period]
    totals: dict[str, dict[str, Any]] = {}
    service_totals: dict[str, float] = {}
    daily_totals: dict[str, dict[str, Any]] = {}
    observed_users: set[str] = set()
    observed_pairs: set[tuple[str, str]] = set()
    counter_resets = 0
    skipped_user_samples = 0
    skipped_service_deltas = 0
    skipped_group_attributions = 0
    interval_reports: list[dict[str, Any]] = []
    latest_source_data: str | None = None

    for (previous_run, previous_users), (current_run, current_users) in zip(
        samples, samples[1:], strict=False
    ):
        previous_by_id = {user["user_id"]: user for user in previous_users}
        current_by_id = {user["user_id"]: user for user in current_users}
        skipped_user_samples += sum(
            not user["error"] and user["user_id"] not in current_by_id
            for user in previous_users
        )
        interval_total = 0.0
        interval_services: dict[str, float] = {}
        interval_users: set[str] = set()

        for user in current_users:
            if user["error"]:
                skipped_user_samples += 1
                continue
            previous_user = previous_by_id.get(user["user_id"])
            if previous_user is None or previous_user["error"]:
                skipped_user_samples += 1
                continue

            keys = _dimension_keys(user, dimension)
            if dimension == "group" and user["membership_error"]:
                skipped_group_attributions += 1
                keys = []
            if dimension_value is not None and not any(
                dimension_value.casefold() in {key.casefold(), name.casefold()}
                for key, name in keys
                if key is not None and name is not None
            ):
                continue
            previous_services = {
                service["service_id"]: service
                for service in previous_user["services"]
            }
            for service in user["services"]:
                current_service_id = service["service_id"]
                if service_id and current_service_id != service_id:
                    continue
                previous_service = previous_services.get(current_service_id)
                current_value = service.get("consumed")
                previous_value = (
                    previous_service.get("consumed") if previous_service else None
                )
                if current_value is None or previous_value is None:
                    skipped_service_deltas += 1
                    continue

                delta = current_value - previous_value
                if delta < 0:
                    counter_resets += 1
                    delta = current_value
                if delta == 0:
                    continue

                interval_total += delta
                interval_services[current_service_id] = (
                    interval_services.get(current_service_id, 0.0) + delta
                )
                service_totals[current_service_id] = (
                    service_totals.get(current_service_id, 0.0) + delta
                )
                interval_users.add(user["user_id"])
                observed_users.add(user["user_id"])
                observed_pairs.add((user["user_id"], current_service_id))

                for key, name in keys:
                    if key is None:
                        continue
                    allocation = totals.setdefault(
                        key,
                        {"name": name, "credits": 0.0, "users": set(), "services": {}},
                    )
                    allocation["credits"] += delta
                    allocation["users"].add(user["user_id"])
                    allocation["services"][current_service_id] = (
                        allocation["services"].get(current_service_id, 0.0) + delta
                    )

                day = current_run["started_at"][:10]
                daily = daily_totals.setdefault(
                    day, {"date": day, "consumed_credits": 0.0, "services": {}}
                )
                daily["consumed_credits"] += delta
                daily["services"][current_service_id] = (
                    daily["services"].get(current_service_id, 0.0) + delta
                )

                source_modified = service.get("source_modified")
                if source_modified and (
                    latest_source_data is None or source_modified > latest_source_data
                ):
                    latest_source_data = source_modified

            current_service_ids = {
                service["service_id"] for service in user["services"]
            }
            skipped_service_deltas += sum(
                (service_id is None or previous_service_id == service_id)
                and previous_service_id not in current_service_ids
                for previous_service_id in previous_services
            )

        interval_reports.append(
            {
                "from_snapshot_id": previous_run["id"],
                "from_captured_at": previous_run["started_at"],
                "to_snapshot_id": current_run["id"],
                "to_captured_at": current_run["started_at"],
                "observed_credit_increase": round(interval_total, 6),
                "users_with_observed_increase": len(interval_users),
                "services": [
                    {"service_id": key, "observed_credit_increase": round(value, 6)}
                    for key, value in sorted(interval_services.items())
                ],
            }
        )

    allocations = [
        {
            "id": key if dimension == "group" else None,
            "name": value["name"],
            "user_count": len(value["users"]),
            "observed_credit_increase": round(value["credits"], 6),
            "services": [
                {"service_id": key, "observed_credit_increase": round(amount, 6)}
                for key, amount in sorted(value["services"].items())
            ],
        }
        for key, value in totals.items()
    ]
    allocations.sort(key=lambda row: row["observed_credit_increase"], reverse=True)

    source_dates = [
        service.get("source_modified")
        for _, users in samples
        for user in users
        for service in user["services"]
        if service.get("source_modified")
    ]
    quality_warnings = []
    if counter_resets:
        quality_warnings.append(
            "A reported balance decreased between snapshots. Its current value was counted "
            "as post-reset usage; this assumes a reporting-period reset and may not equal "
            "actual usage if Microsoft corrected the source balance."
        )
    if skipped_user_samples or skipped_service_deltas:
        quality_warnings.append(
            "Some user/service deltas were excluded because snapshots had collection errors, "
            "users/services were missing, or consumed values were unavailable."
        )
    if skipped_group_attributions:
        quality_warnings.append(
            "Some group allocations were excluded because group membership could not be read."
        )
    if dimension == "group":
        quality_warnings.append(
            "Group allocations use direct memberships and can overlap; group totals are non-additive."
        )

    return {
        "status": "complete_with_quality_warnings" if quality_warnings else "complete",
        "dimension": dimension,
        "dimension_value": dimension_value,
        "service_id": service_id,
        "units": "copilotCredit",
        "requested_period": {"start_date": start_date, "end_date": end_date},
        "observed_period": {
            "start_snapshot_at": samples[0][0]["started_at"],
            "end_snapshot_at": samples[-1][0]["started_at"],
            "source_data_as_of": max(source_dates, default=None),
        },
        "baseline_snapshot_id": samples[0][0]["id"],
        "ending_snapshot_id": samples[-1][0]["id"],
        "snapshots_compared": len(samples),
        "observed_credit_increase": round(sum(service_totals.values()), 6),
        "users_with_observed_increase": len(observed_users),
        "user_service_pairs_with_observed_increase": len(observed_pairs),
        "services": [
            {"service_id": key, "observed_credit_increase": round(value, 6)}
            for key, value in sorted(
                service_totals.items(), key=lambda item: item[1], reverse=True
            )
        ],
        "allocations": allocations[: min(max(top_n, 1), 100)],
        "daily_observed_increases": [
            {
                **row,
                "consumed_credits": round(row["consumed_credits"], 6),
                "services": [
                    {"service_id": key, "consumed_credits": round(value, 6)}
                    for key, value in sorted(row["services"].items())
                ],
            }
            for _, row in sorted(daily_totals.items())
        ],
        "intervals": interval_reports,
        "data_quality": {
            "counter_resets": counter_resets,
            "skipped_user_samples": skipped_user_samples,
            "skipped_service_deltas": skipped_service_deltas,
            "skipped_group_attributions": skipped_group_attributions,
        },
        "warnings": [
            "This is an estimate of increases observed between stored balance snapshots, not a Microsoft usage ledger or invoice report.",
            "Only changes between the selected captures are measured; usage before the first capture or after the last capture is not included.",
            "Daily rows assign an observed increase to the ending snapshot's UTC date; an interval can span multiple days.",
            *quality_warnings,
        ],
    }


def _quantity(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_snapshot_report(
    run: dict[str, Any],
    users: list[dict[str, Any]],
    top_n: int = 25,
    service_id: str | None = None,
    near_limit_percent: float = 80.0,
) -> dict[str, Any]:
    if not 0 <= near_limit_percent <= 100:
        raise ValueError("near_limit_percent must be between 0 and 100")

    by_service: dict[str, dict[str, Any]] = {}
    user_rows: list[dict[str, Any]] = []
    latest_source: str | None = None
    for user in users:
        consumed_total = 0.0
        services: list[dict[str, Any]] = []
        for service in user["services"]:
            service_key = service["service_id"]
            if service_id and service_key != service_id:
                continue
            consumed = service.get("consumed")
            if consumed is not None:
                consumed_total += consumed
            aggregate = by_service.setdefault(
                service_key,
                {"service_id": service_key, "consumed_credits": 0.0, "users_with_usage": 0},
            )
            if consumed is not None:
                aggregate["consumed_credits"] += consumed
                if consumed > 0:
                    aggregate["users_with_usage"] += 1
            source_modified = service.get("source_modified")
            if source_modified and (latest_source is None or source_modified > latest_source):
                latest_source = source_modified
            capacity = service.get("effective_capacity")
            if capacity is None:
                capacity = service.get("per_user_limit")
            percent = (
                consumed / capacity * 100
                if consumed is not None and capacity is not None and capacity > 0
                else None
            )
            services.append(
                {
                    "service_id": service_key,
                    "consumed_credits": consumed,
                    "remaining_credits": service.get("remaining"),
                    "effective_capacity_credits": capacity,
                    "consumption_percent": round(percent, 2) if percent is not None else None,
                    "is_consumption_allowed": service.get("is_consumption_allowed"),
                    "access_decision_reason": service.get("access_decision_reason"),
                }
            )
        user_rows.append(
            {
                "user_id": user["user_id"],
                "user_principal_name": user["upn"],
                "display_name": user["display_name"],
                "department": user["department"],
                "cost_center": user["cost_center"],
                "consumed_credits_across_services": round(consumed_total, 6),
                "service_count": len(services),
                "services_near_limit": sum(
                    service["consumption_percent"] is not None
                    and service["consumption_percent"] >= 80
                    for service in services
                ),
                "error": user["error"],
                "membership_error": user["membership_error"],
                "services": services,
            }
        )

    user_rows.sort(key=lambda item: item["consumed_credits_across_services"], reverse=True)
    near_limit_users = [
        {
            **user,
            "services": [
                service
                for service in user["services"]
                if service["consumption_percent"] is not None
                and service["consumption_percent"] >= near_limit_percent
            ],
        }
        for user in user_rows
        if not user["error"]
        and any(
            service["consumption_percent"] is not None
            and service["consumption_percent"] >= near_limit_percent
            for service in user["services"]
        )
    ]
    near_limit_users.sort(
        key=lambda user: max(
            service["consumption_percent"] for service in user["services"]
        ),
        reverse=True,
    )
    users_with_scope_data = sum(
        not user["error"]
        and any(
            service_id is None or service["service_id"] == service_id
            for service in user["services"]
        )
        for user in users
    )
    services = sorted(
        by_service.values(),
        key=lambda item: item["consumed_credits"],
        reverse=True,
    )
    return {
        "snapshot_id": run["id"],
        "status": run["status"],
        "captured_at": run["started_at"],
        "completed_at": run["completed_at"],
        "users_processed": run["user_count"],
        "users_with_errors": run["failed_count"],
        "users_with_usable_data": sum(not user["error"] for user in users),
        "users_with_scope_data": users_with_scope_data,
        "latest_source_data_modified": latest_source,
        "scope_service_id": service_id,
        "near_limit_percent": near_limit_percent,
        "users_near_limit_count": len(near_limit_users),
        "users_near_limit": near_limit_users[: min(max(top_n, 1), 100)],
        "reported_consumption_credits": round(
            sum(row["consumed_credits_across_services"] for row in user_rows), 6
        ),
        "reported_consumption_credits_across_services": round(
            sum(row["consumed_credits_across_services"] for row in user_rows), 6
        ),
        "services": services,
        "top_users": user_rows[: min(max(top_n, 1), 100)],
        "limitations": [
            "Credit totals aggregate service-level usage; this API does not provide rated currency.",
            "Consumption data can be stale; inspect latest_source_data_modified before decisions.",
            "This snapshot contains Entra users visible to the delegated account and may contain per-user collection errors.",
        ],
    }


def build_chargeback(
    run: dict[str, Any],
    users: list[dict[str, Any]],
    dimension: str,
    service_id: str | None = None,
) -> dict[str, Any]:
    if dimension not in {"user", "department", "cost_center", "group"}:
        raise ValueError("dimension must be user, department, cost_center, or group")

    totals: dict[str, dict[str, Any]] = {}
    for user in users:
        if user["error"]:
            continue
        if dimension == "user":
            value = user["upn"] or user["user_id"]
            keys = [(value, value)]
        elif dimension == "department":
            value = user["department"] or "Unassigned"
            keys = [(value, value)]
        elif dimension == "cost_center":
            value = user["cost_center"] or "Unassigned"
            keys = [(value, value)]
        else:
            if user["membership_error"]:
                continue
            keys = [
                (group.get("id") or group.get("displayName"), group.get("displayName") or group.get("id"))
                for group in user["groups"]
                if group.get("id") or group.get("displayName")
            ] or [("No direct group", "No direct group")]

        selected_services = [
            service for service in user["services"]
            if service_id is None or service["service_id"] == service_id
        ]
        if service_id is not None and not selected_services:
            continue
        service_totals = {
            service["service_id"]: (service.get("consumed") or 0.0)
            for service in selected_services
        }
        for key, name in keys:
            aggregate = totals.setdefault(
                key,
                {
                    "id": key if dimension == "group" else None,
                    "name": name,
                    "consumed_credits": 0.0,
                    "users": set(),
                    "services": {},
                },
            )
            aggregate["users"].add(user["user_id"])
            for service_id, consumed in service_totals.items():
                aggregate["services"][service_id] = (
                    aggregate["services"].get(service_id, 0.0) + consumed
                )
                aggregate["consumed_credits"] += consumed

    rows = [
        {
            "id": aggregate["id"],
            "name": aggregate["name"],
            "user_count": len(aggregate["users"]),
            "consumed_credits_across_services": round(aggregate["consumed_credits"], 6),
            "services": [
                {"service_id": service_id, "consumed_credits": round(consumed, 6)}
                for service_id, consumed in sorted(aggregate["services"].items())
            ],
        }
        for aggregate in totals.values()
    ]
    rows.sort(key=lambda item: item["consumed_credits_across_services"], reverse=True)
    return {
        "snapshot_id": run["id"],
        "dimension": dimension,
        "service_id": service_id,
        "units": "copilotCredit",
        "allocations": rows,
        "limitations": [
            "This is credit consumption allocation, not monetary chargeback.",
            "Group allocations use direct memberships; a user in multiple groups is counted in each group, so group totals are non-additive.",
            "Users with service-balance errors are excluded. Group allocations also exclude users whose group-membership query failed.",
            "When filtered by service, only users with a matching service-balance row are included.",
        ],
    }


def build_outlier_report(
    run: dict[str, Any],
    users: list[dict[str, Any]],
    service_id: str | None = None,
) -> dict[str, Any]:
    sample: list[tuple[dict[str, Any], float]] = []
    excluded_count = 0
    users_without_service_data = 0
    for user in users:
        if user.get("error"):
            excluded_count += 1
            continue
        selected_services = [
            service for service in user["services"]
            if service_id is None or service["service_id"] == service_id
        ]
        values = [
            service["consumed"]
            for service in selected_services
            if service.get("consumed") is not None
        ]
        if values:
            sample.append((user, sum(values)))
        else:
            users_without_service_data += 1

    result: dict[str, Any] = {
        "snapshot_id": run["id"],
        "service_id": service_id,
        "units": "copilotCredit",
        "method": "upper Tukey fence (Q3 + 1.5 * IQR)",
        "sample_size": len(sample),
        "users_excluded_due_to_collection_error": excluded_count,
        "users_without_matching_service_data": users_without_service_data,
    }
    if len(sample) < 4:
        return {
            **result,
            "status": "insufficient_data",
            "minimum_sample_size": 4,
            "outliers": [],
            "message": "At least four users with valid consumption data are required.",
        }

    values = sorted(credits for _, credits in sample)

    def percentile(fraction: float) -> float:
        position = (len(values) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        weight = position - lower
        return values[lower] * (1 - weight) + values[upper] * weight

    q1 = percentile(0.25)
    q3 = percentile(0.75)
    iqr = q3 - q1
    upper_fence = q3 + 1.5 * iqr
    outliers = [
        {
            "user_id": user["user_id"],
            "user_principal_name": user["upn"],
            "display_name": user["display_name"],
            "department": user["department"],
            "consumed_credits": round(credits, 6),
        }
        for user, credits in sample
        if credits > upper_fence
    ]
    outliers.sort(key=lambda item: item["consumed_credits"], reverse=True)
    return {
        **result,
        "status": "outliers_found" if outliers else "no_outliers",
        "q1_credits": round(q1, 6),
        "q3_credits": round(q3, 6),
        "iqr_credits": round(iqr, 6),
        "upper_fence_credits": round(upper_fence, 6),
        "outliers": outliers,
        "limitations": [
            "This is a statistical high-consumption signal, not a spending-limit or policy violation.",
            "Users with collection errors are excluded; results depend on the snapshot's user coverage.",
        ],
    }


def build_forecast(
    snapshot_ids: list[str],
    snapshots: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]],
    *,
    dimension: str,
    dimension_value: str | None,
    service_id: str | None,
    horizon_days: int,
) -> dict[str, Any]:
    if dimension not in {"tenant", "department", "cost_center", "group"}:
        raise ValueError("dimension must be tenant, department, cost_center, or group")
    if dimension != "tenant" and not dimension_value:
        raise ValueError("dimension_value is required for department, cost_center, and group forecasts")
    samples: list[dict[str, Any]] = []
    for snapshot_id in snapshot_ids:
        run, users = snapshots[snapshot_id]
        total = 0.0
        matched_users: set[str] = set()
        user_count = 0
        for user in users:
            if user["error"]:
                continue
            if dimension == "department":
                matches = (user["department"] or "Unassigned") == dimension_value
            elif dimension == "cost_center":
                matches = (user["cost_center"] or "Unassigned") == dimension_value
            elif dimension == "group":
                matches = not user["membership_error"] and dimension_value in {
                    group.get("id")
                    for group in user["groups"]
                }
            else:
                matches = True
            if not matches:
                continue
            matched_users.add(user["user_id"])
            user_count += 1
            for service in user["services"]:
                if service_id and service["service_id"] != service_id:
                    continue
                if service.get("consumed") is not None:
                    total += service["consumed"]
        samples.append(
            {
                "captured_at": run["started_at"],
                "credits": total,
                "users": len(matched_users),
                "source_data_as_of": max(
                    (
                        service["source_modified"]
                        for user in users
                        for service in user["services"]
                        if service.get("source_modified")
                    ),
                    default=None,
                ),
            }
        )

    samples = sorted(samples, key=lambda sample: sample["captured_at"])
    distinct_days = {sample["captured_at"][:10] for sample in samples}
    if len(distinct_days) < 3:
        return {
            "status": "insufficient_history",
            "dimension": dimension,
            "dimension_value": dimension_value,
            "service_id": service_id,
            "samples": samples,
            "message": "At least three snapshots on distinct days are required.",
        }

    times = [
        datetime.fromisoformat(sample["captured_at"]).timestamp() / 86400
        for sample in samples
    ]
    span_days = times[-1] - times[0]
    if span_days < 7:
        return {
            "status": "insufficient_history",
            "dimension": dimension,
            "dimension_value": dimension_value,
            "service_id": service_id,
            "samples": samples,
            "message": "Snapshots must span at least seven days before estimating a trend.",
        }
    if len({sample["credits"] for sample in samples}) < 2:
        return {
            "status": "no_observed_change",
            "dimension": dimension,
            "dimension_value": dimension_value,
            "service_id": service_id,
            "samples": samples,
            "message": "Reported consumption did not change across snapshots; no trend is inferred.",
        }

    mean_x = sum(times) / len(times)
    mean_y = sum(sample["credits"] for sample in samples) / len(samples)
    denominator = sum((x - mean_x) ** 2 for x in times)
    slope = sum(
        (x - mean_x) * (sample["credits"] - mean_y)
        for x, sample in zip(times, samples, strict=True)
    ) / denominator
    daily_rate = max(slope, 0.0)
    return {
        "status": "estimated",
        "dimension": dimension,
        "dimension_value": dimension_value,
        "service_id": service_id,
        "units": "copilotCredit",
        "sample_count": len(samples),
        "span_days": round(span_days, 2),
        "latest_reported_credits": round(samples[-1]["credits"], 6),
        "estimated_daily_change_credits": round(daily_rate, 6),
        "projected_credits_after_horizon": round(
            samples[-1]["credits"] + daily_rate * horizon_days, 6
        ),
        "horizon_days": horizon_days,
        "samples": samples,
        "warnings": [
            "Linear extrapolation of reported credits only; this is not a dollar forecast.",
            "Graph data may be stale or may reset at a reporting-period boundary; review source_data_as_of on every sample.",
            "Changing user coverage between snapshots can affect tenant and department totals.",
            "Group forecasts can overlap because users may belong to multiple groups.",
        ],
    }
