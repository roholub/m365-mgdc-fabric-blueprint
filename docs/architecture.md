# 🏗️ Hybrid MGDC + Graph API Architecture for M365 Telemetry

This document explains **why** and **how** we combine Microsoft Graph Data Connect (MGDC) with the Microsoft Graph API to land a complete picture of Microsoft 365 user activity — including Microsoft 365 Copilot interactions — into a Microsoft Fabric Lakehouse.

---

## 🤔 Why a Hybrid Architecture?

Neither MGDC nor the Microsoft Graph API can deliver the full picture alone:

| Capability | MGDC | Graph API |
|---|---|---|
| Bulk user profile, mail, calendar, Teams, SharePoint data | ✅ | ❌ (throttled) |
| Pre-aggregated activity counts (emails sent, meetings attended) | ✅ | ❌ |
| **Microsoft 365 Copilot interaction history** | ❌ | ✅ |
| Real-time per-user queries | ❌ | ✅ |
| Pipeline-native integration with Fabric | ✅ (Copy activity) | ⚠️ (custom code) |
| Per-column admin consent | ✅ | ❌ (app-level only) |

**The gap**: MGDC doesn't expose Copilot interaction data as a dataset. The only way to get prompt/response telemetry today is the **`aiInteractionHistory`** endpoint on Microsoft Graph.

**The solution**: use MGDC for the 90% of bulk M365 data it covers, and a small Fabric Notebook activity to fan-out Graph API calls for the Copilot subset. Both land in the **same Bronze Lakehouse** so downstream Silver/Gold processing treats them as one unified dataset.

---

## 🗺️ Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                       Microsoft 365 Tenant                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐             │
│  │  Users   │  │   Mail   │  │  Teams   │  │ Copilot  │             │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘             │
│       │             │             │              │                    │
└───────┼─────────────┼─────────────┼──────────────┼────────────────────┘
        │             │             │              │
        │  MGDC Copy Activities     │              │  Graph API
        │  (bulk, scheduled)        │              │  /copilot/users/{id}/
        ▼             ▼             ▼              ▼  interactionHistory/
┌─────────────────────────────────────────────────────────────────────┐
│                    Microsoft Fabric Pipeline                          │
│                                                                       │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │ Copy User_v1     │  │ Copy MailActivity│  │ Copy Teams...    │  │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘  │
│           │                     │                     │              │
│           ▼                     ▼                     ▼              │
│  ┌────────────────────────────────────────────────────────────────┐│
│  │           Bronze Lakehouse (one schema, many tables)            ││
│  │  bronze_mgdc_user_v1                                            ││
│  │  bronze_outlook_mail_activity_v0                                ││
│  │  bronze_teams_channel_activity_v0                               ││
│  │  bronze_copilot_interactions       ◄── (notebook writes here)   ││
│  └────────────────────────────────────────────────────────────────┘│
│           ▲                                                          │
│           │                                                          │
│           │ Reads user list                                          │
│           │                                                          │
│  ┌────────┴─────────────────────────┐                               │
│  │  Notebook: Copilot Interaction   │                               │
│  │  • Auth via Service Principal    │                               │
│  │  • Loop users from User_v1       │                               │
│  │  • Call Graph API per user       │                               │
│  │  • Handle pagination + throttling│                               │
│  │  • Flatten + write Delta         │                               │
│  └──────────────────────────────────┘                               │
└─────────────────────────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────────────────────────────────────────────────────┐
│           Silver Layer (cleansing, dedup, conformed dimensions)      │
└────────────────────────────────┬────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│        Gold Layer (business metrics, aggregations, semantic model)   │
└────────────────────────────────┬────────────────────────────────────┘
                                 ▼
              Power BI │ Data Agents │ Fabric MCP │ Excel
```

---

## 🔐 Trust Boundary and Identity Flow

A **single Service Principal** (your Entra app) is used end-to-end, but it operates with different permission scopes at different steps:

| Step | Identity | Permission(s) | Approval source |
|---|---|---|---|
| MGDC Copy activities | Service Principal | Per-dataset, per-column MGDC consent | M365 Admin Center → MGDC applications |
| Graph API call to `aiInteractionHistory` | Same Service Principal | `AiEnterpriseInteraction.Read.All` (Application) | Entra ID → API permissions → Grant admin consent |
| Reading from Lakehouse | Fabric workspace identity OR Service Principal | OneLake item permissions | Fabric workspace role assignment |
| Writing Delta tables | Notebook execution identity | Workspace Contributor or higher | Fabric workspace role assignment |
| Reading client secret | Notebook execution identity | Key Vault Secrets User | Key Vault RBAC |

> 💡 **Key insight**: the same Entra app object provides identity for *both* the MGDC pipeline and the Graph API notebook. You don't need separate apps — just separate permission grants.

---

## 🔄 End-to-End Data Flow (Step by Step)

### Step 1 — MGDC fans out the bulk pull
At 2:00 AM, the Fabric pipeline triggers. Multiple Copy activities run in parallel, each extracting one MGDC dataset (User_v1, OutlookMailActivity_v0, TeamsChannelActivity_v0, etc.) directly into Bronze Lakehouse tables. Run time: typically 5–30 minutes for a 10K-user org.

### Step 2 — Notebook activity starts after User_v1 succeeds
Once `Copy_MGDC_User_v1` writes the refreshed user list, the dependent Notebook activity starts. It reads the user list and prepares to call Graph API per user.

### Step 3 — Token acquisition
The notebook pulls the client secret from Azure Key Vault, requests an OAuth2 token from the Microsoft identity platform (`/oauth2/v2.0/token` with scope `https://graph.microsoft.com/.default`), and caches it for the duration of the run. Tokens are valid for ~1 hour.

### Step 4 — Fan-out + throttle-aware iteration
The notebook iterates through the user list. For each user:
- Call `GET /v1.0/copilot/users/{id}/interactionHistory/getAllEnterpriseInteractions`
- Follow `@odata.nextLink` until pagination ends
- On HTTP 429, honor `Retry-After` header and exponentially back off
- On HTTP 403/404, log and skip (user has no Copilot license)
- Append flattened rows to an in-memory buffer

### Step 5 — Batched Delta write
Every N users (or at end of run), flush the buffer to the Delta table `bronze_copilot_interactions` using **MERGE** on `interaction_id` to avoid duplicates on incremental runs.

---

## 🔁 Incremental vs Full Refresh Patterns

| Pattern | Pros | Cons | When to use |
|---|---|---|---|
| **Full refresh** (re-fetch all interactions) | Simplest logic, self-healing on missed runs | Expensive at scale, high API call volume | Initial backfill, monthly reconciliation |
| **Incremental** (only last N days) | Fast, low API load | Risk of missing late-arriving data, requires watermark logic | Daily production runs |
| **Hybrid** (daily incremental + weekly reconciliation) | Balances speed and completeness | Slightly more complex pipeline | **Recommended for production** ✅ |

### Recommended schedule

| Schedule | Mode | `lookback_days` |
|---|---|---|
| Daily (Mon–Sat) at 2:00 AM | Incremental | `1` |
| Weekly (Sunday) at 1:00 AM | Reconciliation | `7` |
| Monthly (1st of month) at 12:00 AM | Deep reconciliation | `35` |

The MERGE logic on `interaction_id` ensures reconciliation runs don't create duplicates — they just upsert any missing rows.

---

## 📋 Schema Reference: `bronze_copilot_interactions`

| Column | Type | Source | Description |
|---|---|---|---|
| `interaction_id` | string | `id` | Unique interaction identifier (used as merge key) |
| `user_id` | string | URL path param | Microsoft Graph user object ID |
| `user_principal_name` | string | joined from User_v1 | User's UPN (e.g., `jane.doe@contoso.com`) |
| `created_date_time` | timestamp | `createdDateTime` | When the interaction happened |
| `app_class` | string | `appClass` | Which Copilot surface (e.g., `Word`, `Excel`, `Teams`, `BizChat`) |
| `body_content` | string | `body.content` | The prompt or response text |
| `body_content_type` | string | `body.contentType` | `text` or `html` |
| `conversation_id` | string | `conversationId` | Groups related interactions in a chat thread |
| `etag` | string | `etag` | Version tag |
| `interaction_type` | string | `interactionType` | `userPrompt`, `aiResponse`, etc. |
| `locale` | string | `locale` | User's locale |
| `mentioned_resources_json` | string | `mentions[]` | JSON array of files/sites/people mentioned in the prompt |
| `requested_resources_json` | string | `requestedResources[]` | JSON array of resources Copilot accessed to respond |
| `ingested_timestamp` | timestamp | runtime | When the row was written by the notebook (audit) |

> 💡 The JSON-as-string columns (`mentioned_resources_json`, `requested_resources_json`) preserve nested structure for downstream Silver-layer flattening without breaking Delta schema evolution.

---

## 🔮 Future Enhancements

### Silver Layer
- **Dedup** on `(user_id, interaction_id)` — defensive even though MERGE should prevent dupes
- **Join with `bronze_mgdc_user_v1`** to enrich with department, job_title, location, manager
- **PII redaction** of `body_content` if required by privacy policy (e.g., hash, tokenize, or drop content for compliance)
- **Flatten** `mentioned_resources_json` and `requested_resources_json` into separate fact tables

### Gold Layer
- **Daily Copilot usage per user** (standard per-user aggregation schema):
  - `copilot_total_interactions`
  - `copilot_chat_count`, `copilot_teams_count`, `copilot_word_count`, `copilot_excel_count`, `copilot_ppt_count`, `copilot_outlook_count`, `copilot_loop_count`, `copilot_onenote_count`, `copilot_other_count`
- **Department-level adoption** dashboards
- **Top-content-grounded responses** to understand which SharePoint sites/files are driving Copilot value
- **Manager rollups** using the MGDC `Manager_v0` dataset

### Semantic Model
- Build a Fabric **semantic model** combining:
  - `gold_user_daily_activity` (combining MGDC activity + Copilot counts)
  - `dim_user` (from User_v1 + HRIS shortcut)
  - `dim_date`
- Expose to **Power BI**, **Fabric Data Agent**, and **M365 Copilot Cowork** for natural language queries

### Cross-Tenant Federation
- If you have multiple M365 tenants, deploy this architecture in a **hub-and-spoke** model: one Fabric "hub" workspace pulls from each spoke tenant's MGDC + Graph API endpoint into a unified Lakehouse using Service Principals scoped per tenant.

---

## 📚 References

- [Microsoft Graph Data Connect overview](https://learn.microsoft.com/en-us/graph/data-connect-concept-overview)
- [aiInteractionHistory resource type](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/interaction-export/resources/aiinteractionhistory)
- [Microsoft Graph throttling guidance](https://learn.microsoft.com/en-us/graph/throttling)
- [Fabric Lakehouse medallion architecture](https://learn.microsoft.com/en-us/fabric/onelake/onelake-medallion-lakehouse-architecture)
- [Fabric Notebooks utilities](https://learn.microsoft.com/en-us/fabric/data-engineering/notebook-utilities)
