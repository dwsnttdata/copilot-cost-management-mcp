# Copilot Cost Management MCP

Python MCP server for Microsoft 365 Copilot Cost Management through Microsoft Graph beta.

## API and authentication boundaries

This MCP is configured for **delegated work/school sign-in** using the system browser.
The Microsoft 365 Copilot Cost Management API is a Microsoft Graph **beta** API and
Microsoft Learn says beta APIs are not supported for production applications. The API
returns credit-denominated snapshots and named-user/service diagnostics; it does not
provide rated currency, invoices, prices, or forecasts.

## Setup

```bash
python -m venv .venv
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
```

Configure delegated sign-in in `.env`:

```env
AUTH_MODE=interactive
TENANT_ID=<your Microsoft Entra tenant ID>
CLIENT_ID=<the app registration's Application (client) ID>
```

Find both IDs on the app registration's **Overview** page in the Microsoft Entra
admin center. Under **Authentication**, add the **Mobile and desktop applications**
platform with the redirect URI `http://localhost`. Under **API permissions**, add the
delegated Microsoft Graph permissions listed below. Grant tenant admin consent if your
organization's consent policy or a permission requires it. `CLIENT_SECRET` is not needed
for delegated interactive authentication; do not add one.

Start delegated sign-in with the MCP tool `start_delegated_login`. It opens the Microsoft
sign-in page in your default browser; choose the intended work or school account and
complete any Conditional Access requirements. Check completion with
`get_delegated_login_status`, then call Graph-backed tools. The signed-in user also needs
the required Entra role for the requested operation. Sign-in runs in the background so
the MCP call returns promptly while the browser login completes.

Then run from this project directory:

```bash
python -m copilot_cost_management
```

## VS Code MCP configuration

```json
{
  "servers": {
    "copilot-cost-management": {
      "type": "stdio",
      "command": "C:\\path\\to\\.venv\\Scripts\\python.exe",
      "args": ["-m", "copilot_cost_management"],
      "cwd": "C:\\Temp\\copilot-cost-management-mcp"
    }
  }
}
```

## Azure Web App deployment

This project runs as a standard Linux Azure Web App using the MCP Streamable HTTP
transport. The MCP endpoint is:

```text
https://<web-app-name>.azurewebsites.net/mcp
```

Create a Linux Web App using Python 3.12. Set its startup command to:

```text
python -m uvicorn web_app:app --host 0.0.0.0 --port 8000
```

The Entra app registration used for the MCP API must expose the `Mcp.Access` delegated
scope and have the Microsoft Graph delegated permissions listed below, with consent
granted as required. In the Web App's **Authentication** settings, add Microsoft Entra
as the identity provider using that app registration, require authentication, and
enable the token store. For unauthenticated API requests, return HTTP 401 rather than
redirecting to a browser. Add the MCP API's Application ID URI as an allowed audience.
Configure Copilot Studio to request the full delegated scope
`<Application-ID-URI>/Mcp.Access`, using the exact Application ID URI from **Expose an
API** (if that URI already ends with `/Mcp.Access`, the full scope ends with
`/Mcp.Access/Mcp.Access`). The MCP server uses Copilot Studio's incoming `Authorization:
Bearer` token for OBO and falls back to the App Service provider token only when no
bearer token is supplied. Do not leave the MCP endpoint accessible without
authentication.

The Web App uses a confidential client for OBO. Create a client secret in the MCP API
app registration under **Certificates & secrets** and configure the same client ID and
secret in the Web App's Entra identity-provider settings. Also set the secret as the
`CLIENT_SECRET` application setting for the OBO exchange:

```text
TENANT_ID=<tenant ID>
CLIENT_ID=<Application (client) ID of the MCP API registration>
CLIENT_SECRET=<secret value for that registration>
AUTH_MODE=obo
MCP_API_SCOPE=Mcp.Access
GRAPH_BASE_URL=https://graph.microsoft.com/beta
SCM_DO_BUILD_DURING_DEPLOYMENT=true
```

Set the startup command under **Configuration → General settings → Startup Command**.
Store `CLIENT_SECRET` as a Key Vault reference when possible. Do not use
`AUTH_MODE=interactive` in Azure: it tries to open a browser on the server. Keep
`AUTH_MODE=interactive` for the local VS Code setup above. Browser-login tools and the
process-local daily snapshot scheduler are intentionally not exposed by the Web App.

For GitHub Actions deployment, create the repository variable `AZURE_WEBAPP_NAME` and
the secret `AZURE_WEBAPP_PUBLISH_PROFILE` from the Web App's **Get publish profile**
option. The workflow builds and tests the project, then deploys it to that Web App.

The current SQLite audit and FinOps stores are local files. Web App local storage
isn't a durable shared persistence layer and can be lost during redeployment or app
replacement. Before using tools that write proposals, approvals, or snapshots in
production, move these stores to durable shared storage.

## Required Microsoft Graph permissions

Read:
- CopilotCostManagement.Read.All
- CopilotCostManagement-Policy.Read.All
- CopilotCostManagement-Assignment.Read.All
- CopilotCostManagement-UserData.Read.All
- User.Read.All for user lookup and tenant-wide user enumeration
- Group.Read.All for group lookup and direct-membership chargeback

Write:
- CopilotCostManagement-Policy.ReadWrite.All
- CopilotCostManagement-Assignment.ReadWrite.All

These must be configured as **delegated** permissions for interactive sign-in, not
application permissions. Creating policies additionally requires Billing Administrator
or Global Administrator at runtime. The signed-in user and tenant consent must authorize
the operation; a configured permission alone does not grant the user an admin role.

## FinOps analytics

The Microsoft Graph Cost Management API reports **Copilot Credits only** and does not
provide rated currency or invoices. When requested, `convert_copilot_credits_to_usd`
fetches Microsoft's current [Copilot Credit P3 pricing guidance](https://learn.microsoft.com/en-us/azure/cost-management-billing/reservations/copilot-credit-p3)
and applies its published illustrative pay-as-you-go rate assumption. The
`get_finops_snapshot_report` and `get_credit_chargeback` tools can include these estimates
with `include_usd_estimate=true`; the default remains credit-only. If Microsoft no longer
publishes a recognizable rate on that page or it cannot be fetched, the estimate fails
explicitly rather than using a cached or hard-coded value.

These are **PAYG estimates, not actual charges**. Microsoft documents that pre-purchase
plans are discounted and customer billing can depend on billing method, service, region,
contract terms, and other discounts. Use the billing export or invoice for actual charged
amounts. The API also exposes service-level user balances, not task or prompt attribution,
so the MCP cannot report credits consumed by an individual task.

For tenant-wide snapshots, the MCP pages through Entra users (`User.Read.All`) and then
reads each user's service balances (`CopilotCostManagement-UserData.Read.All`) and direct
group memberships (`Group.Read.All`). This is an N+1 API workload, so collection is
resumable and bounded to 50 users per tool call (25 by default), with a configurable
5,000-user safety cap per snapshot (`FINOPS_MAX_USERS_PER_SNAPSHOT`):

1. Call `capture_tenant_finops_snapshot` once per collection cycle.
2. Repeat the tool until its status is `completed` or `completed_with_errors`.
3. Use `get_finops_snapshot_report` for tenant totals, top users, per-service consumption,
   and users approaching/exceeding their effective limits. Set `service_id` to the exact
   service ID from the report to focus on Cowork or another workload.
4. Use `get_credit_chargeback` with `user`, `department`, `cost_center`, or `group` and
   optionally `service_id` for service-specific credit allocation.
5. Use `get_finops_period_report` with inclusive UTC `start_date` and `end_date` values to
   estimate credit increases between stored snapshots. A baseline snapshot before the
   period and at least one snapshot inside it are required. The report includes observed
   intervals, capture-date buckets, service and dimension breakdowns, and reset/coverage
   warnings. Capture-date buckets assign changes to the ending snapshot's date; they are
   not transaction-level daily totals. This is a difference between reported balances, not
   an authoritative transaction ledger; collect daily snapshots for more granular monitoring.
6. Use `find_unusual_finops_consumers` to flag statistical high-consumption outliers in a
   snapshot. It uses the upper Tukey fence (Q3 + 1.5 * IQR), requires at least four users,
   and is a review signal rather than a policy-limit determination.
7. Call `forecast_finops_consumption` after collecting at least three snapshots on
   distinct days spanning at least seven days. It returns a linear credit trend only,
   with source-data freshness warnings; it is not a dollar forecast. For a group forecast,
   provide the Entra group object ID as `dimension_value`.

The delegated user must have the API's required permissions and qualifying Entra role.
Microsoft documents 403 `Authorization_RequestDenied` when the caller lacks a required
permission or directory role. An authenticated sign-in alone does not establish API access;
tenant administrators may need to grant delegated consent and assign an eligible role.
The Cost Management API targets FinOps/Global Reader and Billing Administrator for reads;
creating policies requires Billing Administrator or Global Administrator.

For automated daily capture while the MCP is running, call
`start_daily_finops_snapshot_schedule`; it takes a snapshot immediately and repeats every
24 hours. Check progress with `get_daily_finops_snapshot_schedule_status`, and stop it
with `stop_daily_finops_snapshot_schedule`. The schedule is process-local: it stops when
VS Code/MCP exits and must be started again after restart. It uses the current delegated
interactive sign-in and stores snapshots locally in the configured SQLite database
alongside the MCP's approval/audit records. Do not configure a separate unattended
scheduled process until an approved secure authentication/cache design is in place.

Direct Entra group memberships can overlap. A user in multiple groups is counted in each
group allocation, so group totals are non-additive. Department and cost-center reports
use the corresponding Entra user attributes; missing values are grouped as `Unassigned`.

## Approval pattern

Every mutation is two-stage:
1. `propose_*` stores a pending operation and returns an approval ID.
2. `approve_operation` marks it approved.
3. `execute_approved_operation` retrieves the latest policy ETag and executes it.

This local approval store is suitable for development. For production, replace it with Power Automate Approvals, Teams Adaptive Cards, ServiceNow, or another identity-bound approval system.

## Tool coverage

- Tenant credit balance
- Policy list/get/create/update/delete
- Assigned group list/add/remove
- Service setting list/get/create/update/disable
- Alert setting get/update/delete
- Recipient list/add/remove
- User and service balance diagnostics
- UPN/group lookup
- Service-level near-limit analysis and recommendations for supplied users
- Resumable tenant-wide user/service snapshots
- Snapshot progress/status reporting and per-page error visibility
- Service-filtered tenant reports with explicit users near or over limits
- Period reports with observed changes, daily buckets, and service/user/group breakdowns
- Statistical tenant-wide high-consumer outlier analysis
- Optional process-local daily snapshot schedule (immediate first run; repeats every 24 hours)
- Per-user, service, department, cost-center, and direct-group credit chargeback
- On-demand USD estimates based on a freshly fetched Microsoft Learn PAYG rate
- Snapshot-based, credit-only trend and linear forecast
- Governed proposals, approval, execution, rejection, status, and audit history

## Remaining scope boundaries

Actual charged dollars, invoice reconciliation, and actual purchase forecasts require an
external billing source. The Microsoft Learn PAYG conversion is only an estimate.
Task/prompt-level credit attribution is not exposed by the documented Cost Management API.
Per-user service balances are read individually; there is no Cost Management endpoint that
returns a tenant-wide user balance collection. Policy audience management supports all
users or selected groups, not individual-user assignment or Light/Standard/Heavy persona
changes. Trends and forecasts only become available after snapshots have been collected
over time, and reflect the freshness/period semantics of the underlying beta API.
