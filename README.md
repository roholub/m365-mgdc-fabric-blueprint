# 📊 M365 → Fabric: A Cross-Tenant MGDC Ingestion Blueprint

> **Public reference implementation** for ingesting Microsoft 365 organizational data into Microsoft Fabric using **Microsoft Graph Data Connect (MGDC)** with a **direct Fabric Lakehouse destination**.

This blueprint is for organizations that need to:
- Centralize **multi-tenant M365 telemetry** (collaboration, calendar, files, Copilot activity) into a single analytics hub.
- Replace **manual PBIX exports** or one-off scripts with a **governed, repeatable pipeline**.
- Combine M365 data with non-Microsoft data (e.g., HRIS, Google Workspace, Okta) via Fabric **shortcuts**.

> 💡 **Scope of this blueprint**: This repo is built as a **single-tenant POC pattern** — perfect for validating the architecture, getting Copilot data flowing, and seeing the full pipeline work end-to-end. If you eventually want to operationalize this across multiple tenants, that's a different conversation (and a future companion repo). For now, focus on getting it working for one tenant first.

---

## 🧠 First, the foundational question: MGDC vs. Microsoft Graph API

These are two different products that both expose Microsoft 365 data.

| | **Microsoft Graph API** | **Microsoft Graph Data Connect (MGDC)** |
|---|---|---|
| **Purpose** | Per-record, real-time CRUD against M365 data | Bulk, scheduled, large-scale data egress for analytics/ML |
| **Pattern** | REST API call per object | Pipeline copy of millions of rows at a time |
| **Throttling** | Heavy per-app throttling, batching required | No throttling — designed for scale |
| **Consent model** | App-level admin consent | **Per-dataset + per-column** admin consent with a tenant approval workflow |
| **Output** | JSON response in your app | Lands as Parquet/JSON in Azure Data Lake Gen2, Azure SQL, or **Microsoft Fabric Lakehouse** |
| **Identity** | Delegated or app permissions | Application identity (service principal) only |
| **Best for** | "Get John's last 50 emails" | "Export every user's mailbox activity for the last 6 months" |

📚 Official docs:
- [Microsoft Graph Data Connect FAQ](https://learn.microsoft.com/en-us/graph/data-connect-faq)
- [MGDC overview](https://learn.microsoft.com/en-us/graph/data-connect-concept-overview)

---

## 🏗️ Architecture

```
┌────────────────────┐         ┌────────────────────┐
│  M365 Tenant(s)    │         │  External Sources  │
│  Users, Mail,      │         │  HRIS, Workspace,  │
│  Calendar, Teams,  │         │  Okta, etc.        │
│  SharePoint,       │         └─────────┬──────────┘
│  OneDrive          │                   │
└─────────┬──────────┘                   │ (Fabric Shortcut)
          │                              │
          │ (MGDC Copy Activity)         │
          ▼                              ▼
┌──────────────────────────────────────────────────┐
│           Microsoft Fabric Lakehouse              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐        │
│  │ Bronze   │→ │ Silver   │→ │ Gold     │        │
│  └──────────┘  └──────────┘  └──────────┘        │
└──────────────┬───────────────────────────────────┘
               │
               ▼
   ┌─────────────────────────────┐
   │ Power BI / Data Agents /    │
   │ Fabric MCP / Notebooks      │
   └─────────────────────────────┘
```

---

## 📦 Datasets used in this blueprint

All MGDC datasets are described at the official catalog: [Datasets, regions, and sinks supported by MGDC](https://learn.microsoft.com/en-us/graph/data-connect-datasets).

### Identity & Org Hierarchy
| Dataset | Stability | Purpose |
|---|---|---|
| `BasicDataSet_v0.User_v1` | **GA** | Full user profile (displayName, jobTitle, department, mail, etc.) |
| `BasicDataSet_v0.Manager_v0` | Preview | Direct manager assignment per user |
| `BasicDataSet_v0.DirectReport_v0` | Preview | Inverse — who reports to whom |

### Activity Aggregates (pre-aggregated by Microsoft)
| Dataset | Purpose |
|---|---|
| `OutlookMailActivity_v0` | Emails sent/received per user per day |
| `OutlookMeetingActivity_v0` | Meetings organized/attended (incl. ad-hoc vs. scheduled, recurring vs. one-time) |
| `TeamsChannelActivity_v0` | Channel message counts per user |
| `TeamsConversationActivity_v0` | Chat message counts per user |

### Raw Mail/Calendar (if you need full content)
| Dataset | Purpose |
|---|---|
| `BasicDataSet_v0.Message_v1` | Mailbox messages (metadata + body) |
| `BasicDataSet_v0.CalendarView_v1` | Calendar events |

### SharePoint & OneDrive
> ⚠️ Important: OneDrive is technically a **type of SharePoint site** (`WebTemplate = SPSPERS`). The SharePoint datasets include OneDrive data — distinguish them via `RootWeb.WebTemplate` in the Sites dataset.

| Dataset | Purpose |
|---|---|
| `SharePointSites_v1` | Site catalog (storage used, file count, archive state, last access) |
| `SharePointFiles_v1` | File-level catalog |
| `SharePointFileActions_v1` | File activity events (view, edit, upload, delete) |
| `SharePointPermissions_v1` | Sharing permissions (incl. internal vs. external) |
| `OneDriveSyncHealth_v1` | OneDrive client sync health |

### Teams
| Dataset | Purpose |
|---|---|
| `TeamsCallRecords_v1` | Teams call/meeting activity records |
| `TeamsChannelDetails_v0` | Channel catalog |

---

## ⚠️ The Copilot data gap

If your schema requires **Microsoft 365 Copilot usage counts** (chat, Teams, Word, Excel, PPT, Outlook, Loop, OneNote, etc.), **MGDC does not currently expose these as a dataset**.

Use one of the following instead:

### Option A — Microsoft 365 Copilot Usage Reports (admin-friendly)
- Surfaces via M365 Admin Center and the Graph **Reports API**
- Endpoint: `getMicrosoft365CopilotUsageUserDetail`
- Pre-aggregated by user and app

### Option B — Microsoft Graph `aiInteractionHistory` (granular)
- Endpoint: `GET /copilot/users/{id}/interactionHistory/getAllEnterpriseInteractions`
- Required permission: `AiEnterpriseInteraction.Read.All` (application)
- Requires Microsoft 365 Copilot license per user being queried
- Returns the actual prompts, responses, resources accessed
- 📚 [aiInteractionHistory: getAllEnterpriseInteractions](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/interaction-export/aiinteractionhistory-getallenterpriseinteractions)

> 💡 **Recommended pattern**: Run MGDC for the 90% of data it covers, then add a small Fabric Notebook activity that calls the Graph `aiInteractionHistory` API for the Copilot subset and lands it in the same Bronze layer. **This repo includes that notebook** — see [`notebooks/`](notebooks/) and the [deployment guide](docs/DEPLOYMENT.md).

---

## 📁 Repository contents

```
m365-mgdc-fabric-blueprint/
├── README.md                    # This blueprint
├── LICENSE                      # MIT
├── CONTRIBUTING.md              # How to contribute
├── docs/
│   ├── architecture.md          # Hybrid MGDC + Graph API architecture deep-dive
│   └── DEPLOYMENT.md            # Step-by-step Copilot notebook deployment guide
└── notebooks/
    ├── copilot_interaction_history_ingestion.ipynb   # ✅ Fabric-ready — import straight into a workspace
    └── copilot_interaction_history_ingestion.py      # 📖 Explained companion — same logic, heavily commented
```

**Two notebooks, one job.** Both ingest Copilot interaction history into `mgdc.bronze_copilot_interactions` using the **same parameter contract** (`tenant_id`, `client_id`, `key_vault_url`, `copilot_secret_name`, `lookback_days`):

- **`.ipynb`** — the Fabric-ready version. Use **+ New → Import notebook** and it lands as runnable cells immediately.
- **`.py`** — the same pipeline written as a heavily-commented script, ideal if you're newer to PySpark or want to read through each step before running it.

---

## 🚀 Setup steps

### Prerequisites
- An Azure subscription in the **same region** as the M365 tenant
- **A Fabric workspace and Lakehouse** (capacity-backed, not trial-only for production)
- A user with **Global Admin** role in the M365 tenant
- ⚠️ **You will need a second Global Admin** (see Gotcha #1)

### Step 1 — Create the Entra App Registration
1. Entra ID → App registrations → **New registration**
2. Name: `MGDC-Fabric-Ingestion` (or your convention)
3. Supported account types: **Single tenant**
4. No redirect URI needed
5. Save the **Application (client) ID** and **Tenant ID**
6. Create a **client secret** under *Certificates & secrets* — save the value immediately

### Step 2 — Enable MGDC at the tenant level
- M365 Admin Center → **Settings → Org settings → Security & Privacy → Microsoft Graph Data Connect**
- Toggle **Allow Microsoft Graph Data Connect to access data in this organization** → **ON**

### Step 3 — Register the app for MGDC
- Azure Portal → **Microsoft Graph Data Connect → + New registration**
- **Destination type**: Fabric Lakehouse
- **Storage Account URI**: paste your Fabric Lakehouse URI in the form:
  `https://api.fabric.microsoft.com/v1/workspaces/{workspaceId}/items/{lakehouseId}`
- **Publish Type**: Single-Tenant
- **Compute Type**: MicrosoftFabric
- **Activity Type**: CopyActivity
- **Datasets**: select each dataset you need (e.g., `User_v1`, `OutlookMailActivity_v0`, etc.)
- **Columns**: ⚠️ Click **Select All** for each dataset (see Gotcha #2)
- **Scope**: All users in the Microsoft 365 organization
- Click **Create**

### Step 4 — Approve the registration
- Have your **second Global Admin** sign into M365 Admin Center
- Navigate to **Settings → Org settings → Microsoft Graph Data Connect applications**
- Approve the pending registration
- ⏱️ Wait 2–5 minutes for propagation

### Step 5 — Build the Fabric pipeline

This is where MGDC and the Copilot notebook come together. We'll build one pipeline that handles both, then put it on a schedule. 🎯

#### 5a — Add the MGDC Copy activities

1. In your Fabric workspace, create a new **Data Pipeline** — call it something like `pl_m365_ingestion`.
2. Add a **Copy data** activity per dataset (one activity per dataset — see Gotcha #4).
3. **Source** for each Copy activity:
   - Connection: New → **Microsoft 365** → authenticate with Service Principal (App ID + Tenant ID + Client Secret from Step 1)
   - Table: select the MGDC dataset (e.g., `BasicDataSet_v0.User_v1`)
4. **Destination** for each Copy activity:
   - Workspace → Lakehouse → your destination Lakehouse
   - Root folder: **Tables**
   - Table name: `bronze_<dataset_name>` (e.g., `bronze_mgdc_user_v1`)
   - Table action: **Overwrite** for first runs; switch to merge/append for incremental later
5. **Mapping**: Click *Import schemas* — verify all expected columns appear.

> 💡 The Copy activities can run **in parallel** — they don't depend on each other. Drop them on the canvas without any dependency arrows between them.

#### 5b — Add the Copilot Notebook activity

This is what fills the Copilot gap (see the "⚠️ The Copilot data gap" section above.).

1. First, **import the notebook** into your workspace:
   - In Fabric → **+ New item → Import notebook → Upload**
   - Select `notebooks/copilot_interaction_history_ingestion.ipynb` from this repo
   - Attach your Bronze Lakehouse as the default Lakehouse (top-left panel of the notebook)
2. Back in your pipeline, drag a **Notebook** activity onto the canvas.
3. **Settings**:
   - Workspace: your current workspace
   - Notebook: select `copilot_interaction_history_ingestion`
   - Base parameters: pass `tenant_id`, `client_id`, `key_vault_url`, `copilot_secret_name`, `lookback_days` — these override the defaults baked into the notebook's Cell 3.
4. **Wire the dependency**: draw a success arrow from `Copy_MGDC_User_v1` → Notebook activity. The notebook reads the user list from `mgdc.bronze_mgdc_user_v1`, so it needs that table to be fresh before it runs.

> 💡 The other MGDC Copy activities (Mail, Teams, etc.) don't need to gate the notebook. Only `User_v1` is a hard dependency.

#### 5c — Put it on a schedule

This is the whole point of using a Fabric pipeline instead of running the notebook manually. 🕐

1. Pipeline canvas → **Schedule** (top toolbar) → **+ New schedule**
2. **Recommended starting cadence**:
   - **Frequency**: Daily
   - **Time**: 2:00 AM in the tenant's time zone (low-traffic window for MGDC + Graph)
   - **Time zone**: pick your org's primary time zone
3. **Pipeline parameter overrides** (optional but useful):
   - `lookback_days = 1` — daily incremental
   - For a weekly reconciliation run, create a *second* schedule on Sundays with `lookback_days = 7`
4. **Save** the schedule. Fabric handles the rest — retries, failure notifications, run history, all of it.

> 💡 The notebook's MERGE on `interaction_id` makes re-runs idempotent. You can re-run the same window without creating duplicates, which is why daily + weekly reconciliation works cleanly.

#### 5d — Optional first manual run

Before relying on the schedule, do a **manual trigger** of the pipeline (top toolbar → **Run**) so you can:
- Confirm all the connections resolve
- Watch the validation banners in the notebook output
- Sanity-check row counts in the Lakehouse
- Catch any permission or networking issues *before* a scheduled 2 AM run silently fails

If anything trips, the validation banners in the notebook (Cell 3 onwards) will tell you exactly what to fix.

### Step 6 — Validate the data
Once your pipeline succeeds, query the Lakehouse SQL endpoint:

```sql
SELECT COUNT(*) AS row_count, COUNT(DISTINCT displayName) AS distinct_users
FROM mgdc.bronze_mgdc_user_v1;
```

> ⚠️ Note the `mgdc.` schema prefix — see Gotcha #5.


## ⏰ Operational considerations

Once the pipeline is on a schedule, here's what to keep an eye on.

### Monitoring
- **Fabric Monitoring hub** → filter by pipeline name → see every run's status, duration, and failure logs
- The notebook's **Cell 17 summary** is your at-a-glance health report check it after each run if helpful
- Set up **Activator** alerts on failed runs if you want push notifications

### Schedule patterns that work in practice

| Schedule | Mode | `lookback_days` | Why |
|---|---|---|---|
| Daily (Mon–Sat) at 2:00 AM | Incremental | `1` | Catches yesterday's activity quickly |
| Weekly (Sunday) at 1:00 AM | Reconciliation | `7` | Picks up any late-arriving data |
| Monthly (1st) at midnight | Deep reconciliation | `35` | Catches anything the weekly missed |

The MERGE logic on `interaction_id` makes overlapping windows safe — no duplicates, just upserts.
---

## 🐛 Gotchas (lessons learned the hard way)

### #1 — You need TWO Global Admins
The recommended MGDC setup uses **separation of duties**: one admin creates and registers the app, a *different* admin approves it. If you only have one Global Admin, the approval flow may stall or appear missing.

### #2 — "Scope: All" ≠ "All columns"
On the MGDC Datasets page, **Scope** is the *row* filter ("all users vs. specific groups"). **Columns** is a separate selection. If you don't explicitly check every column you need, the runtime will fail with:

```
UserErrorOffice365DataLoaderError: Invalid requested columns.
The following requested columns were not consented to for data...
```

**Always** click *Select All* on the Columns picker for every dataset during registration.

### #3 — Any registration edit invalidates approval
If you change anything about the registration after approval (columns, scope, datasets), the existing admin consent becomes **stale**. The pipeline will fail with:

```
ConsentNotFound
```

**Fix**: Re-approve in M365 Admin Center → Org settings → Microsoft Graph Data Connect applications **every time** you edit the registration.

### #4 — One dataset per Copy activity

The Fabric Copy Data activity handles **one MGDC dataset per activity**. To ingest multiple datasets, add multiple parallel Copy activities to the same pipeline. They can run concurrently — no dependency arrows needed.

> 💡 **Scaling note**: If you're running this across many tenants or wiring up dozens of datasets, the visual pipeline gets unwieldy fast. At that point, consider **Apache Airflow inside Microsoft Fabric** as the orchestrator — same MGDC + Notebook pattern, just driven from a config-as-code DAG instead of a Fabric pipeline canvas.

### #5 — Lakehouse SQL endpoint uses schema namespaces
Tables created via Copy activity land under a schema (often `mgdc`, not `dbo`). Always qualify queries:

```sql
-- ❌ Wrong
SELECT * FROM bronze_mgdc_user_v1;

-- ✅ Right
SELECT * FROM mgdc.bronze_mgdc_user_v1;
```

### #6 — User_v1 profile fields are tenant-dependent
User profile fields in MGDC's `User_v1` dataset are tenant-dependent. Some tenants may have many null values in columns like `aboutMe`, `city`, `companyName` because the source M365 profile isn't filled out. Validate using consistently-populated columns like `displayName`, `mail`, `id`.

### #7 — Manager_v0 is still preview
The `Manager_v0` dataset hasn't graduated to GA. Schema may change. Use it for POCs but plan for a v1 upgrade.

### #8 — Region alignment matters
Your Azure resource group, Fabric capacity, and M365 tenant region should align. Cross-region MGDC pipelines either fail or hit egress costs. Check [MGDC supported regions](https://learn.microsoft.com/en-us/graph/data-connect-datasets) before provisioning.

### #9 — SharePoint datasets are pre-aggregated and 2 days delayed
SharePoint Sites/Files/Permissions are pre-collected daily in M365 and arrive in MGDC **48 hours after the fact**. Other MGDC datasets are collected on-demand and are more current. Plan your refresh cadence accordingly.

### #10 — OneDrive ≠ a separate dataset
OneDrive data is **inside the SharePoint datasets** — distinguish OneDrive sites via `RootWeb.WebTemplate = "SPSPERS"` (or `WebTemplateId = 21`).

---

## 🔗 Reference links

### Official Microsoft docs
- [MGDC overview](https://learn.microsoft.com/en-us/graph/data-connect-concept-overview)
- [MGDC dataset catalog](https://learn.microsoft.com/en-us/graph/data-connect-datasets)
- [MGDC FAQ](https://learn.microsoft.com/en-us/graph/data-connect-faq)
- [aiInteractionHistory API (Copilot data)](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/interaction-export/aiinteractionhistory-getallenterpriseinteractions)
- [Fabric Lakehouse overview](https://learn.microsoft.com/en-us/fabric/data-engineering/lakehouse-overview)

### Community resources
- [MGDC for SharePoint blog (Jose Barreto)](https://techcommunity.microsoft.com/category/content_management/blog/microsoft_graph_data_connect_for_sharepo)
- [PnP MGDC SharePoint governance samples](https://github.com/pnp/mgdc-spo-governance)

---

## 📝 License

MIT — fork it, adapt it, share it.

## 🙏 Disclaimer

This blueprint is **not an official Microsoft project**. It reflects hands-on lessons learned from real implementations and is provided as a reference only. Microsoft documentation is the source of truth for all product behavior.
