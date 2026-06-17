# 🚀 DEPLOYMENT.md — Copilot Interaction History Ingestion

This guide walks you through deploying the **`copilot_interaction_history_ingestion`** notebook into Microsoft Fabric so it runs alongside your MGDC pipeline and lands Microsoft 365 Copilot interaction data into your Bronze Lakehouse.

> 📓 The repo ships **two copies** of this notebook in [`notebooks/`](../notebooks/): the Fabric-ready **`.ipynb`** (import it straight into a workspace) and an explained **`.py`** companion (same logic, heavily commented). Both share the same parameter names, so this guide applies to either.

---

## 📋 Prerequisites

Before you deploy, confirm the following:

### 1. Entra App Permissions

Your existing Entra app (the one used for MGDC) needs an **additional** Graph permission for the Copilot endpoint.

1. Open **Entra ID → App registrations → [your MGDC app] → API permissions**
2. Click **Add a permission → Microsoft Graph → Application permissions**
3. Search for and add: **`AiEnterpriseInteraction.Read.All`**
4. Click **Grant admin consent for [your tenant]** ✅
5. Verify the status shows **Granted** with a green checkmark

> ⚠️ Admin consent is a SEPARATE step from the MGDC tenant approval. Both are required.

### 2. MGDC Pipeline Already Running

The notebook reads the user list from `mgdc.bronze_mgdc_user_v1`. Confirm this table exists and is being refreshed by your MGDC Copy activity. Validate with:

```sql
SELECT COUNT(*) AS total_users, COUNT(mail) AS users_with_mail
FROM mgdc.bronze_mgdc_user_v1;
```

### 3. Microsoft 365 Copilot Licenses

The Graph `aiInteractionHistory` API only returns data for users with active **Microsoft 365 Copilot** licenses. Users without licenses will return HTTP 403 (gracefully skipped by the notebook).

---

## 🔐 Storing the Client Secret in Azure Key Vault

**Never** hardcode the client secret into the notebook. Use Azure Key Vault.

### Step 1 — Create or reuse a Key Vault

```bash
az keyvault create \
  --name kv-fabric-mgdc \
  --resource-group MyRGinUSEast \
  --location eastus
```

### Step 2 — Add the client secret

```bash
az keyvault secret set \
  --vault-name kv-fabric-mgdc \
  --name copilot-graph-client-secret \
  --value "<your-client-secret-value>"
```

### Step 3 — Grant Fabric workspace identity access

1. In Azure Portal, navigate to your Key Vault → **Access control (IAM)**
2. Click **Add role assignment** → **Key Vault Secrets User**
3. Assign to the **Fabric workspace managed identity** (or your service principal if you don't use workspace identity)

### Step 4 — Note the Key Vault URL

The notebook reads the secret directly from the vault **URL** you pass as `key_vault_url`. Use the full vault URI:

```
https://kv-fabric-mgdc.vault.azure.net/
```

(You can copy this from the Key Vault **Overview** page — it's the *Vault URI* field.)

---

## 📓 Adding the Notebook to Fabric

### Option A — Import the Fabric-ready notebook (recommended)

1. In your Fabric workspace, click **+ New → Notebook**
2. Click the **⋯ menu → Import notebook**
3. Upload `notebooks/copilot_interaction_history_ingestion.ipynb`
4. The cells appear ready to run — no conversion needed

### Option B — Use the explained `.py` companion

1. Create a new notebook (or use **Import notebook** on the `.py`)
2. Open `notebooks/copilot_interaction_history_ingestion.py` — each section is separated by a `# COMMAND ----------` marker
3. Fabric converts those markers into cells on import; or paste each section into its own cell manually

### Configure the notebook

1. **Attach your Lakehouse**: click the Lakehouse picker in the left rail and select your Bronze Lakehouse
2. **Set default language**: PySpark
3. **Verify cell parameters**: the first cell defines parameters — confirm names match what you'll pass from the pipeline

---

## 🔌 Wiring into a Fabric Pipeline

The notebook should run **after** the MGDC `Copy_MGDC_User_v1` activity completes, so the user list is fresh.

### Add the Notebook activity

1. Open your existing MGDC pipeline (e.g., `Pipeline_1` in the destination workspace)
2. Drag a **Notebook** activity onto the canvas
3. Connect the success output (green arrow) of `Copy_MGDC_User_v1` to the new Notebook activity
4. Name it: `Ingest_Copilot_Interactions`

### Configure the activity

In the **Settings** tab:
- **Workspace**: current workspace
- **Notebook**: select `copilot_interaction_history_ingestion`

In the **Base parameters** section, add the following. **Names must match the variable names in the notebook's parameters cell exactly** (they are lowercase):

| Name | Type | Value |
|---|---|---|
| `tenant_id` | string | your Entra tenant ID |
| `client_id` | string | your Entra app client ID |
| `key_vault_url` | string | `https://kv-fabric-mgdc.vault.azure.net/` |
| `copilot_secret_name` | string | `copilot-graph-client-secret` |
| `lookback_days` | int | `7` for a routine incremental run; `30`+ for the first backfill |

> 💡 The Bronze Lakehouse is selected by attaching it to the notebook (see **Configure the notebook** above), not via a parameter. `source_user_table`, `target_schema`, and `target_table` default to `mgdc.bronze_mgdc_user_v1`, `mgdc`, and `bronze_copilot_interactions` — override them only if your names differ.

### Save and validate

Click **Save** → **Validate** to confirm the pipeline has no errors.

---

## ⏰ Scheduling Daily Runs

1. In your pipeline, click **Schedule** in the top bar
2. Set:
   - **Frequency**: Daily
   - **Time**: 2:00 AM local time (off-peak)
   - **Time zone**: your business time zone
3. Click **Apply**

### Recommended schedule pattern

| Day | Mode | Parameter |
|---|---|---|
| Mon–Sat | Incremental | `lookback_days=2` (small window catches the last day or two of interactions) |
| Sunday | Reconciliation | `lookback_days=35` (re-scans a wider window to catch any late-arriving or missed rows) |

The `MERGE` on `interaction_id` makes the reconciliation run idempotent — it upserts missing rows without creating duplicates. You can implement this with two scheduled triggers on the same pipeline, each passing a different `lookback_days` value.

---

## 🔍 Monitoring and Troubleshooting

### Monitor pipeline runs

- **Fabric Monitor hub → Pipeline runs** shows execution history, duration, and row counts
- Click into a run to see the notebook's printed output (summary stats at the end)

### Common errors and fixes

| Error | Likely Cause | Fix |
|---|---|---|
| **HTTP 401 Unauthorized** | Bad client secret, expired secret, or wrong tenant ID | Verify secret in Key Vault, check expiration in Entra, confirm `tenant_id` parameter |
| **HTTP 403 Forbidden** per-user | User has no Copilot license | Expected — notebook skips and logs. Verify count of skipped users is reasonable |
| **HTTP 403 Forbidden** on first call | Missing `AiEnterpriseInteraction.Read.All` permission or admin consent | Re-check Entra app permissions, re-grant admin consent |
| **HTTP 429 Too Many Requests** | Throttling | The `.py` companion has built-in exponential backoff (`max_retries`, `initial_backoff_s`). If persistent, raise `initial_backoff_s` or `max_retries` |
| **Notebook timeout** | Too many users / too wide a window in one run | Lower `lookback_days`, or split the run across multiple triggers |
| **Schema mismatch on merge** | Graph API added new fields | Update the row mapping and the `target_struct` schema in the notebook to include the new columns |

---

## 💰 Cost Expectations

### Graph API
- ✅ **No per-call charges** for the Graph aiInteractionHistory endpoint
- ⚠️ Throttling is the real cost — slower runs = more compute hours

### Fabric Compute
- The notebook runs on Spark, billed against your Fabric capacity
- **Estimate**: for 10,000 licensed Copilot users with ~30 interactions/day each:
  - Run time: ~15–25 minutes on a small Spark pool
  - Capacity units consumed: ~5–8 CU-hours per daily run
- For 1,000 users: ~3–5 minutes, ~1 CU-hour

### Storage
- Delta table growth: ~0.5–2 KB per interaction row
- 10K users × 30 interactions × 30 days = 9M rows ≈ 5–15 GB/month
- OneLake storage is billed separately at standard rates

---

## ✅ Validation Queries

After your first successful run, validate the data with these SQL queries against the Lakehouse SQL endpoint:

### Row count and freshness

```sql
SELECT
    COUNT(*) AS total_interactions,
    COUNT(DISTINCT user_id) AS distinct_users,
    MIN(created_date_time) AS earliest,
    MAX(created_date_time) AS latest,
    MAX(ingested_timestamp) AS last_run
FROM mgdc.bronze_copilot_interactions;
```

### Top users by Copilot usage

```sql
SELECT
    user_principal_name,
    COUNT(*) AS interaction_count,
    COUNT(DISTINCT DATE(created_date_time)) AS active_days
FROM mgdc.bronze_copilot_interactions
WHERE created_date_time >= CURRENT_DATE - 30
GROUP BY user_principal_name
ORDER BY interaction_count DESC
LIMIT 25;
```

### App breakdown (Word vs Excel vs Teams vs Chat)

```sql
SELECT
    app_class,
    COUNT(*) AS interactions,
    COUNT(DISTINCT user_id) AS unique_users
FROM mgdc.bronze_copilot_interactions
WHERE created_date_time >= CURRENT_DATE - 30
GROUP BY app_class
ORDER BY interactions DESC;
```

### Daily volume trend

```sql
SELECT
    DATE(created_date_time) AS day,
    COUNT(*) AS interactions,
    COUNT(DISTINCT user_id) AS active_users
FROM mgdc.bronze_copilot_interactions
WHERE created_date_time >= CURRENT_DATE - 30
GROUP BY DATE(created_date_time)
ORDER BY day;
```

---

## 🛡️ Production Hardening Checklist

Before you call this "production-ready":

- [ ] **Secret rotation**: schedule client secret rotation every 6–12 months. Update Key Vault, the Entra app secret expires gracefully.
- [ ] **Alerting on failures**: configure a Fabric pipeline alert (or use Azure Monitor) to email/Teams-notify on failed runs.
- [ ] **Dead-letter logging**: capture per-user failures into a `failed_users` Delta table for retry analysis.
- [ ] **Backfill plan**: documented procedure to re-run for a historical window if data is lost (set `lookback_days` to N).
- [ ] **Schema evolution**: add a unit test that fails fast if the Graph API response schema changes unexpectedly.
- [ ] **Lineage documentation**: track which downstream Power BI reports or Data Agents depend on `bronze_copilot_interactions` so you can communicate during incidents.
- [ ] **Cost monitoring**: track CU consumption weekly to spot unexpected growth.
- [ ] **Privacy review**: confirm with your privacy/compliance team that storing Copilot prompt content is acceptable per your data classification policy. The notebook stores `body_content` — you may want to redact or hash it for production.

---

## 🆘 Getting Help

- [Microsoft Graph aiInteractionHistory docs](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/interaction-export/aiinteractionhistory-getallenterpriseinteractions)
- [Microsoft Graph throttling guidance](https://learn.microsoft.com/en-us/graph/throttling)
- [Fabric Notebooks docs](https://learn.microsoft.com/en-us/fabric/data-engineering/how-to-use-notebook)
- [Azure Key Vault references in Fabric](https://learn.microsoft.com/en-us/fabric/data-engineering/notebook-utilities)
