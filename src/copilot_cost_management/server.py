from __future__ import annotations
import asyncio
from typing import Any
from urllib.parse import quote
from mcp.server.fastmcp import FastMCP
from .auth import token_provider
from .config import settings
from .finops import (
    build_chargeback,
    build_forecast,
    build_outlier_report,
    build_period_report,
    build_snapshot_report,
    finops_store,
    parse_period_boundary,
)
from .graph import graph
from .pricing import (
    attach_usd_estimate,
    convert_credits_to_usd,
    fetch_live_copilot_credit_pricing,
)
from .scheduler import daily_snapshot_scheduler
from .store import store

mcp = FastMCP("copilot-cost-management", host=settings.mcp_host, port=settings.mcp_port)
ROOT = "copilot/costManagement"

def _policy_path(pid: str) -> str: return f"{ROOT}/spendingPolicies/{quote(pid, safe='')}"
def _proposal(action: str, payload: dict[str, Any], reason: str, requested_by: str):
    return store.propose(action, payload, reason, requested_by)

@mcp.tool()
def start_delegated_login() -> dict[str, str | int | None]:
    """Open browser-based delegated sign-in without blocking the MCP request."""
    return token_provider.start_delegated_login()

@mcp.tool()
def get_delegated_login_status() -> dict[str, str | int | None]:
    """Check whether browser-based delegated sign-in has completed."""
    return token_provider.delegated_login_status()

@mcp.tool()
async def get_tenant_credit_balance() -> dict[str, Any]:
    """Get tenant-wide Copilot Credit balances by billing method using delegated auth."""
    return await graph.request("GET", f"{ROOT}/getTenantCreditBalance", delegated=True)

@mcp.tool()
async def list_spending_policies() -> list[dict[str, Any]]:
    """List all Copilot spending policies."""
    return await graph.all_pages(f"{ROOT}/spendingPolicies")

@mcp.tool()
async def get_spending_policy(policy_id: str) -> dict[str, Any]:
    """Get one spending policy, including the current parent ETag used for safe writes."""
    return await graph.request("GET", _policy_path(policy_id))

@mcp.tool()
async def list_assigned_groups(policy_id: str) -> list[dict[str, Any]]:
    """List Entra groups assigned to a selected-groups spending policy."""
    return await graph.all_pages(f"{_policy_path(policy_id)}/microsoft.graph.selectedGroupsSpendingPolicy/assignedGroups")

@mcp.tool()
async def list_service_settings(policy_id: str) -> list[dict[str, Any]]:
    """List service/workload settings governed by a policy."""
    return await graph.all_pages(f"{_policy_path(policy_id)}/serviceSettings")

@mcp.tool()
async def get_service_setting(policy_id: str, service_id: str) -> dict[str, Any]:
    """Get one service setting from a policy."""
    return await graph.request("GET", f"{_policy_path(policy_id)}/serviceSettings/{quote(service_id, safe='')}")

@mcp.tool()
async def get_alert_setting(policy_id: str) -> dict[str, Any]:
    """Get a policy's email threshold alert configuration."""
    return await graph.request("GET", f"{_policy_path(policy_id)}/emailThresholdSettings")

@mcp.tool()
async def list_alert_recipients(policy_id: str) -> list[dict[str, Any]]:
    """List users receiving threshold alerts for a policy."""
    return await graph.all_pages(f"{_policy_path(policy_id)}/emailThresholdSettings/recipients")

@mcp.tool()
async def resolve_user(user_principal_name: str) -> dict[str, Any]:
    """Resolve an Entra user by exact UPN and return id/displayName/UPN."""
    return await graph.request("GET", f"users/{quote(user_principal_name, safe='')}?$select=id,displayName,userPrincipalName,mail")

@mcp.tool()
async def resolve_group(group_id_or_name: str) -> list[dict[str, Any]]:
    """Resolve an Entra group by object ID or exact display name."""
    try:
        return [await graph.request("GET", f"groups/{quote(group_id_or_name, safe='')}?$select=id,displayName,mail")]
    except Exception:
        name = group_id_or_name.replace("'", "''")
        result = await graph.request("GET", f"groups?$filter=displayName eq '{quote(name, safe=" ''")}'&$select=id,displayName,mail")
        return result.get("value", [])

@mcp.tool()
async def get_user_balance(user_id_or_upn: str) -> dict[str, Any]:
    """Get a named user's Copilot Credit balance and selected spending policy."""
    return await graph.request("GET", f"{ROOT}/userBalances/{quote(user_id_or_upn, safe='')}")

@mcp.tool()
async def list_user_service_balances(user_id_or_upn: str) -> list[dict[str, Any]]:
    """List a user's Copilot Credit balances by service/workload."""
    return await graph.all_pages(f"{ROOT}/userBalances/{quote(user_id_or_upn, safe='')}/serviceBalances")

@mcp.tool()
async def get_user_service_balance(user_id_or_upn: str, service_id: str) -> dict[str, Any]:
    """Get a user's balance for one Copilot service/workload."""
    return await graph.request("GET", f"{ROOT}/userBalances/{quote(user_id_or_upn, safe='')}/serviceBalances/{quote(service_id, safe='')}")

def _quantity_value(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def _effective_limit(balance: dict[str, Any]) -> float | None:
    capacity = _quantity_value(balance.get("effectiveCapacityQuantity"))
    if capacity is not None:
        return capacity
    return _quantity_value(balance.get("configuredPerUserLimit"))

async def _collect_finops_user(user: dict[str, Any]) -> dict[str, Any]:
    user_id = user.get("id")
    if not user_id:
        return {
            **user,
            "groups": [],
            "services": [],
            "error": "Microsoft Graph user row did not include an id.",
        }

    encoded_id = quote(user_id, safe="")
    service_result, group_result = await asyncio.gather(
        graph.all_pages(
            f"{ROOT}/userBalances/{encoded_id}/serviceBalances",
            max_pages=10,
        ),
        graph.all_pages(
            f"users/{encoded_id}/memberOf/microsoft.graph.group?$select=id,displayName",
            max_pages=10,
        ),
        return_exceptions=True,
    )
    service_error = (
        f"{type(service_result).__name__}: {service_result}"
        if isinstance(service_result, BaseException)
        else None
    )
    group_error = (
        f"{type(group_result).__name__}: {group_result}"
        if isinstance(group_result, BaseException)
        else None
    )
    services = []
    if not service_error:
        for row in service_result:
            services.append(
                {
                    "serviceId": row.get("serviceId"),
                    "consumed": _quantity_value(row.get("consumedQuantity")),
                    "remaining": _quantity_value(row.get("remainingQuantity")),
                    "effective_capacity": _quantity_value(row.get("effectiveCapacityQuantity")),
                    "per_user_limit": _quantity_value(row.get("configuredPerUserLimit")),
                    "period_limit": _quantity_value(row.get("configuredPeriodLimit")),
                    "is_consumption_allowed": row.get("isConsumptionAllowed"),
                    "access_decision_reason": row.get("accessDecisionReason"),
                    "source_modified": (row.get("dataStatus") or {}).get("lastModifiedDateTime"),
                }
            )
            if not services[-1]["serviceId"]:
                services.pop()

    groups = []
    if not group_error:
        groups = [
            {"id": row.get("id"), "displayName": row.get("displayName")}
            for row in group_result
            if row.get("id")
        ]
    return {
        **user,
        "groups": groups,
        "services": services,
        "error": service_error,
        "membership_error": group_error,
        "usage_as_of": max(
            (row["source_modified"] for row in services if row.get("source_modified")),
            default=None,
        ),
    }

@mcp.tool()
async def capture_tenant_finops_snapshot(page_size: int = 25) -> dict[str, Any]:
    """Capture/resume a paged tenant-wide credit snapshot. Repeat until status is completed."""
    page_size = min(max(page_size, 1), 50)
    run = finops_store.start_or_resume_run()
    if run["page_count"] == 0:
        path = (
            "users?$select=id,displayName,userPrincipalName,department,costCenter"
            f"&$top={page_size}"
        )
    else:
        path = run["next_link"]
        if not path:
            raise RuntimeError("Snapshot run has no continuation link but is still marked in progress.")

    try:
        page = await graph.request("GET", path)
    except Exception as error:
        finops_store.fail_run(run["id"], f"{type(error).__name__}: {error}"[:1000])
        raise

    directory_users = page.get("value", [])
    if run["user_count"] + len(directory_users) > settings.finops_max_users_per_snapshot:
        message = (
            "Tenant snapshot user cap exceeded "
            f"({settings.finops_max_users_per_snapshot}); raise FINOPS_MAX_USERS_PER_SNAPSHOT "
            "only after reviewing expected API volume."
        )
        finops_store.fail_run(run["id"], message)
        raise ValueError(message)
    semaphore = asyncio.Semaphore(8)

    async def collect_limited(user: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await _collect_finops_user(user)

    collected = await asyncio.gather(
        *(collect_limited(user) for user in directory_users),
        return_exceptions=True,
    )
    rows = []
    page_errors = []
    for user, result in zip(directory_users, collected, strict=True):
        if isinstance(result, BaseException):
            page_errors.append(
                {
                    "user_id": user.get("id"),
                    "user_principal_name": user.get("userPrincipalName"),
                    "error": f"{type(result).__name__}: {result}",
                }
            )
            rows.append(
                {
                    **user,
                    "groups": [],
                    "services": [],
                    "error": f"{type(result).__name__}: {result}"[:1000],
                }
            )
        else:
            rows.append(result)
            for field in ("error", "membership_error"):
                if result.get(field):
                    page_errors.append(
                        {
                            "user_id": user.get("id"),
                            "user_principal_name": user.get("userPrincipalName"),
                            "error": result[field],
                        }
                    )

    updated = finops_store.save_page(
        run["id"],
        rows,
        page.get("@odata.nextLink"),
    )
    return {
        "snapshot_id": updated["id"],
        "status": updated["status"],
        "completed_at": updated["completed_at"],
        "pages_processed": updated["page_count"],
        "users_processed": updated["user_count"],
        "users_with_errors": updated["failed_count"],
        "users_in_this_page": len(rows),
        "next_page_available": updated["status"] == "in_progress",
        "page_errors": page_errors[:20],
        "message": (
            "Call this tool again to continue the same tenant snapshot."
            if updated["status"] == "in_progress"
            else "Snapshot complete. Use get_finops_snapshot_report or get_credit_chargeback."
        ),
    }

async def _capture_full_tenant_snapshot() -> dict[str, Any]:
    result = await capture_tenant_finops_snapshot()
    while result["status"] == "in_progress":
        await asyncio.sleep(0.1)
        result = await capture_tenant_finops_snapshot()
    return result

@mcp.tool()
def start_daily_finops_snapshot_schedule() -> dict[str, Any]:
    """Start an immediate tenant snapshot and repeat daily while this MCP process stays running."""
    return daily_snapshot_scheduler.start(_capture_full_tenant_snapshot)

@mcp.tool()
def stop_daily_finops_snapshot_schedule() -> dict[str, Any]:
    """Stop the in-process daily snapshot schedule."""
    return daily_snapshot_scheduler.stop()

@mcp.tool()
def get_daily_finops_snapshot_schedule_status() -> dict[str, Any]:
    """Get status for the process-local daily snapshot schedule."""
    return daily_snapshot_scheduler.status()

@mcp.tool()
def get_finops_snapshot_status(snapshot_id: str) -> dict[str, Any]:
    """Get progress, coverage, failures, and status for a tenant snapshot run."""
    run = finops_store.get_run(snapshot_id)
    return {
        "snapshot_id": run["id"],
        "status": run["status"],
        "started_at": run["started_at"],
        "completed_at": run["completed_at"],
        "pages_processed": run["page_count"],
        "users_processed": run["user_count"],
        "users_with_errors": run["failed_count"],
        "error": run["error"],
        "next_page_available": run["status"] == "in_progress",
    }

@mcp.tool()
def get_finops_period_report(
    start_date: str,
    end_date: str,
    dimension: str = "tenant",
    dimension_value: str | None = None,
    service_id: str | None = None,
    top_n: int = 25,
) -> dict[str, Any]:
    """Report observed credit increases between stored snapshots over an inclusive UTC date range."""
    start_at = parse_period_boundary(start_date)
    end_at = parse_period_boundary(end_date, end_of_day=True)
    if start_at > end_at:
        raise ValueError("start_date must be on or before end_date.")
    snapshot_ids = finops_store.period_runs(
        start_at.isoformat(),
        end_at.isoformat(),
    )
    snapshots = {
        snapshot_id: finops_store.snapshot_rows(snapshot_id)
        for snapshot_id in snapshot_ids
    }
    report = build_period_report(
        snapshot_ids,
        snapshots,
        start_date=start_date,
        end_date=end_date,
        dimension=dimension,
        dimension_value=dimension_value,
        service_id=service_id,
        top_n=top_n,
    )
    return report

@mcp.tool()
async def get_finops_snapshot_report(
    snapshot_id: str,
    top_n: int = 25,
    service_id: str | None = None,
    near_limit_percent: float = 80.0,
    include_usd_estimate: bool = False,
) -> dict[str, Any]:
    """Report tenant consumption, optionally adding a live Microsoft PAYG USD estimate."""
    run, users = finops_store.snapshot_rows(snapshot_id)
    if run["status"] not in ("completed", "completed_with_errors"):
        raise ValueError(f"Snapshot must be complete; current status={run['status']}")
    report = build_snapshot_report(
        run,
        users,
        top_n,
        service_id=service_id,
        near_limit_percent=near_limit_percent,
    )
    if include_usd_estimate:
        pricing = await fetch_live_copilot_credit_pricing()
        rate = pricing["rate_usd_per_credit"]
        report["pricing"] = pricing
        attach_usd_estimate(report, "reported_consumption_credits", rate)
        for row in report["services"]:
            attach_usd_estimate(row, "consumed_credits", rate)
        for key in ("top_users", "users_near_limit"):
            for user in report[key]:
                attach_usd_estimate(
                    user, "consumed_credits_across_services", rate
                )
                for service in user["services"]:
                    attach_usd_estimate(service, "consumed_credits", rate)
    return report

@mcp.tool()
async def get_credit_chargeback(
    snapshot_id: str,
    dimension: str = "department",
    service_id: str | None = None,
    include_usd_estimate: bool = False,
) -> dict[str, Any]:
    """Allocate credits by user, department, cost center, or group; optionally add live PAYG USD estimates."""
    run, users = finops_store.snapshot_rows(snapshot_id)
    if run["status"] not in ("completed", "completed_with_errors"):
        raise ValueError(f"Snapshot must be complete; current status={run['status']}")
    report = build_chargeback(run, users, dimension, service_id=service_id)
    report["users_with_collection_errors"] = run["failed_count"]
    if include_usd_estimate:
        pricing = await fetch_live_copilot_credit_pricing()
        rate = pricing["rate_usd_per_credit"]
        report["pricing"] = pricing
        for allocation in report["allocations"]:
            attach_usd_estimate(
                allocation, "consumed_credits_across_services", rate
            )
            for service in allocation["services"]:
                attach_usd_estimate(service, "consumed_credits", rate)
    return report

@mcp.tool()
async def convert_copilot_credits_to_usd(credits: float) -> dict[str, Any]:
    """Convert Copilot Credits to an estimated USD amount using the live Microsoft Learn PAYG rate."""
    pricing = await fetch_live_copilot_credit_pricing()
    return convert_credits_to_usd(credits, pricing)

@mcp.tool()
def find_unusual_finops_consumers(
    snapshot_id: str,
    service_id: str | None = None,
) -> dict[str, Any]:
    """Flag statistical high-consumption outliers in a tenant snapshot (minimum four users)."""
    run, users = finops_store.snapshot_rows(snapshot_id)
    if run["status"] not in ("completed", "completed_with_errors"):
        raise ValueError(f"Snapshot must be complete; current status={run['status']}")
    return build_outlier_report(run, users, service_id)

@mcp.tool()
def forecast_finops_consumption(
    dimension: str = "tenant",
    dimension_value: str | None = None,
    service_id: str | None = None,
    horizon_days: int = 30,
    lookback_days: int = 90,
) -> dict[str, Any]:
    """Estimate a credit-only trend; use a group object ID for group dimension_value."""
    horizon_days = min(max(horizon_days, 1), 365)
    lookback_days = min(max(lookback_days, 1), 365)
    snapshot_ids = finops_store.completed_runs(lookback_days)
    snapshots = {
        snapshot_id: finops_store.snapshot_rows(snapshot_id)
        for snapshot_id in snapshot_ids
    }
    return build_forecast(
        snapshot_ids,
        snapshots,
        dimension=dimension,
        dimension_value=dimension_value,
        service_id=service_id,
        horizon_days=horizon_days,
    )

@mcp.tool()
async def analyze_users_near_limit(user_ids_or_upns: list[str], threshold_percent: float = 80.0) -> list[dict[str, Any]]:
    """Analyze named users' service-level consumption against effective per-user limits."""
    threshold = min(max(threshold_percent, 0), 100)
    output = []
    for user in user_ids_or_upns[:500]:
        balances = await list_user_service_balances(user)
        for balance in balances:
            consumed = _quantity_value(balance.get("consumedQuantity"))
            limit = _effective_limit(balance)
            percent = consumed / limit * 100 if consumed is not None and limit else None
            if percent is not None and percent >= threshold:
                output.append(
                    {
                        "user": user,
                        "service_id": balance.get("serviceId"),
                        "consumption_percent": round(percent, 2),
                        "consumed_credits": consumed,
                        "effective_limit_credits": limit,
                        "remaining_credits": _quantity_value(balance.get("remainingQuantity")),
                        "data_status": balance.get("dataStatus"),
                    }
                )
    return sorted(output, key=lambda item: item["consumption_percent"], reverse=True)

@mcp.tool()
async def recommend_policy_changes(user_ids_or_upns: list[str], near_limit_percent: float = 80,
                                   high_percent: float = 100) -> list[dict[str, Any]]:
    """Generate non-executing service-level recommendations from actual balance rows."""
    if not 0 <= near_limit_percent <= high_percent <= 100:
        raise ValueError("Thresholds must satisfy 0 <= near_limit_percent <= high_percent <= 100.")
    recommendations = []
    for user in user_ids_or_upns[:500]:
        balances = await list_user_service_balances(user)
        service_recommendations = []
        for balance in balances:
            consumed = _quantity_value(balance.get("consumedQuantity"))
            limit = _effective_limit(balance)
            if consumed is None or limit is None or limit <= 0:
                continue
            percent = consumed / limit * 100
            if percent >= high_percent:
                recommendation = (
                    "Review service-level usage, business need, and the applicable policy "
                    "before considering a limit or access change."
                )
            elif percent >= near_limit_percent:
                recommendation = (
                    "Notify the owner and review usage trend before changing a limit."
                )
            else:
                recommendation = "No immediate service-level policy change is indicated."
            service_recommendations.append(
                {
                    "service_id": balance.get("serviceId"),
                    "consumed_credits": consumed,
                    "effective_limit_credits": limit,
                    "consumption_percent": round(percent, 2),
                    "recommendation": recommendation,
                }
            )
        recommendations.append(
            {
                "user": user,
                "status": "analyzed" if service_recommendations else "no_service_balance_data",
                "services": service_recommendations,
            }
        )
    return recommendations

# All mutations are proposed first.
@mcp.tool()
def propose_create_policy(policy: dict[str, Any], reason: str, requested_by: str) -> dict[str, Any]:
    """Propose creation of an all-users or selected-groups policy. Does not execute."""
    return _proposal("create_policy", {"policy": policy}, reason, requested_by)

@mcp.tool()
def propose_update_policy(policy_id: str, changes: dict[str, Any], reason: str, requested_by: str) -> dict[str, Any]:
    """Propose changing editable spending-policy fields. Does not execute."""
    return _proposal("update_policy", {"policy_id": policy_id, "changes": changes}, reason, requested_by)

@mcp.tool()
def propose_delete_policy(policy_id: str, reason: str, requested_by: str) -> dict[str, Any]:
    """Propose deleting a spending policy. Does not execute."""
    return _proposal("delete_policy", {"policy_id": policy_id}, reason, requested_by)

@mcp.tool()
def propose_add_group(policy_id: str, group_id: str, reason: str, requested_by: str) -> dict[str, Any]:
    """Propose assigning an Entra group to a selected-groups policy."""
    return _proposal("add_group", {"policy_id": policy_id, "group_id": group_id}, reason, requested_by)

@mcp.tool()
def propose_remove_group(policy_id: str, group_id: str, reason: str, requested_by: str) -> dict[str, Any]:
    """Propose removing an assigned group. Graph prevents removal of the last audience group."""
    return _proposal("remove_group", {"policy_id": policy_id, "group_id": group_id}, reason, requested_by)

@mcp.tool()
def propose_create_service_setting(policy_id: str, setting: dict[str, Any], reason: str, requested_by: str) -> dict[str, Any]:
    """Propose adding a service setting to a policy."""
    return _proposal("create_service_setting", {"policy_id": policy_id, "setting": setting}, reason, requested_by)

@mcp.tool()
def propose_update_service_setting(policy_id: str, service_id: str, changes: dict[str, Any], reason: str, requested_by: str) -> dict[str, Any]:
    """Propose updating or disabling a service setting. Service settings are disabled, not deleted."""
    return _proposal("update_service_setting", {"policy_id": policy_id, "service_id": service_id, "changes": changes}, reason, requested_by)

@mcp.tool()
def propose_update_alert(policy_id: str, alert_setting: dict[str, Any], reason: str, requested_by: str) -> dict[str, Any]:
    """Propose creating/updating a policy email-threshold setting."""
    return _proposal("update_alert", {"policy_id": policy_id, "alert_setting": alert_setting}, reason, requested_by)

@mcp.tool()
def propose_delete_alert(policy_id: str, reason: str, requested_by: str) -> dict[str, Any]:
    """Propose deleting a policy email-threshold setting."""
    return _proposal("delete_alert", {"policy_id": policy_id}, reason, requested_by)

@mcp.tool()
def propose_add_alert_recipient(policy_id: str, user_id: str, reason: str, requested_by: str) -> dict[str, Any]:
    """Propose adding an alert recipient."""
    return _proposal("add_recipient", {"policy_id": policy_id, "user_id": user_id}, reason, requested_by)

@mcp.tool()
def propose_remove_alert_recipient(policy_id: str, user_id: str, reason: str, requested_by: str) -> dict[str, Any]:
    """Propose removing an alert recipient. Graph prevents removing the last recipient while alerting is enabled."""
    return _proposal("remove_recipient", {"policy_id": policy_id, "user_id": user_id}, reason, requested_by)

@mcp.tool()
def get_approval_status(approval_id: str) -> dict[str, Any]: return store.get(approval_id)

@mcp.tool()
def approve_operation(approval_id: str, approver: str) -> dict[str, Any]:
    """Approve a pending operation. In production, bind approver identity through an external approval system."""
    return store.approve(approval_id, approver)

@mcp.tool()
def reject_operation(approval_id: str, rejected_by: str, reason: str) -> dict[str, Any]:
    """Reject a pending or approved operation."""
    return store.reject(approval_id, rejected_by, reason)

@mcp.tool()
def get_audit_history(limit: int = 100) -> list[dict[str, Any]]: return store.history(limit)

async def _etag(policy_id: str) -> str:
    policy = await get_spending_policy(policy_id)
    etag = policy.get("_etag") or policy.get("@odata.etag")
    if not etag: raise RuntimeError("Graph did not return the parent policy ETag required for safe mutation")
    return etag

@mcp.tool()
async def execute_approved_operation(approval_id: str, executed_by: str) -> dict[str, Any]:
    """Execute one approved operation, retrieving the latest parent ETag immediately before mutation."""
    op = store.get(approval_id)
    if op["status"] != "approved": raise ValueError(f"Operation must be approved; current status={op['status']}")
    a, p = op["action"], op["payload"]
    try:
        if a == "create_policy": result = await graph.request("POST", f"{ROOT}/spendingPolicies", json=p["policy"])
        else:
            pid = p["policy_id"]; etag = await _etag(pid); base = _policy_path(pid)
            if a == "update_policy": result = await graph.request("PATCH", base, json=p["changes"], etag=etag)
            elif a == "delete_policy": result = await graph.request("DELETE", base, etag=etag)
            elif a == "add_group": result = await graph.request("POST", f"{base}/microsoft.graph.selectedGroupsSpendingPolicy/assignedGroups/$ref", json={"@odata.id": f"https://graph.microsoft.com/beta/groups/{p['group_id']}"}, etag=etag)
            elif a == "remove_group": result = await graph.request("DELETE", f"{base}/microsoft.graph.selectedGroupsSpendingPolicy/assignedGroups/{p['group_id']}/$ref", etag=etag)
            elif a == "create_service_setting": result = await graph.request("POST", f"{base}/serviceSettings", json=p["setting"], etag=etag)
            elif a == "update_service_setting": result = await graph.request("PATCH", f"{base}/serviceSettings/{p['service_id']}", json=p["changes"], etag=etag)
            elif a == "update_alert": result = await graph.request("PATCH", f"{base}/emailThresholdSettings", json=p["alert_setting"], etag=etag)
            elif a == "delete_alert": result = await graph.request("DELETE", f"{base}/emailThresholdSettings", etag=etag)
            elif a == "add_recipient": result = await graph.request("POST", f"{base}/emailThresholdSettings/recipients/$ref", json={"@odata.id": f"https://graph.microsoft.com/beta/users/{p['user_id']}"}, etag=etag)
            elif a == "remove_recipient": result = await graph.request("DELETE", f"{base}/emailThresholdSettings/recipients/{p['user_id']}/$ref", etag=etag)
            else: raise ValueError(f"Unsupported action: {a}")
        store.set_status(approval_id, "executed", executed_by, result)
        return {"approval_id": approval_id, "status": "executed", "result": result}
    except Exception as exc:
        store.set_status(approval_id, "failed", executed_by, {"error": str(exc)})
        raise

def main():
    transport = settings.mcp_transport.lower()
    if transport == "stdio": mcp.run(transport="stdio")
    elif transport in ("sse", "streamable-http"): mcp.run(transport=transport)
    else: raise ValueError("MCP_TRANSPORT must be stdio, sse, or streamable-http")

if __name__ == "__main__": main()
