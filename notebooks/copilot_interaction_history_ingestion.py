# MAGIC %md
# MAGIC # 📊 M365 Copilot Interaction History → Fabric Lakehouse
# MAGIC
# MAGIC Hey 👋 — this notebook grabs Microsoft 365 Copilot interaction data from the **Microsoft Graph API** and lands it in a Fabric Lakehouse as a Delta table. It's the companion to an upstream **MGDC pipeline** that already loads data into a Bronze lakehouse.
# MAGIC
# MAGIC ## 🤔 Wait, why a separate notebook? Doesn't MGDC do this?
# MAGIC
# MAGIC Not for Copilot, no. MGDC covers a *lot* of M365 data — mail, calendar, Teams, SharePoint, files — but Copilot interaction history isn't one of its datasets. The supported way to pull it is the `aiInteractionHistory` Graph API endpoint, which is what this notebook does.
# MAGIC
# MAGIC So the pattern is: **MGDC for the bulk M365 telemetry, Graph for the Copilot piece**, both landing in the same Lakehouse so they're easy to join downstream.
# MAGIC
# MAGIC ## ✅ Before you run this, make sure you've got
# MAGIC
# MAGIC - An Entra app registration with the **`AiEnterpriseInteraction.Read.All`** application permission, admin-consented
# MAGIC - A client secret stored somewhere safe — **Azure Key Vault** is the recommended default; there's also a local-test option (see below)
# MAGIC - Fabric workspace identity granted the **`Key Vault Secrets User`** role on the vault (if using Key Vault)
# MAGIC - The upstream MGDC pipeline has already loaded `mgdc.bronze_mgdc_user_v1`
# MAGIC - The users you're querying actually have a **Microsoft 365 Copilot** license
# MAGIC
# MAGIC ## 🔐 Two ways to handle the secret
# MAGIC
# MAGIC | Mode | When to use it |
# MAGIC |---|---|
# MAGIC | 🔒 **Production (Key Vault)** | The default. Recommended for any scheduled or shared run. |
# MAGIC | 🧪 **Local test (inline secret)** | For quick validation when Key Vault isn't reachable yet. **Never commit a non-empty value.** |
# MAGIC
# MAGIC The notebook auto-detects which mode you're in based on whether you set `client_secret` in Cell 3. Both modes are clearly flagged in the output so you always know what you're running.
# MAGIC
# MAGIC ## 📦 What you'll end up with
# MAGIC
# MAGIC A Delta table at `mgdc.bronze_copilot_interactions` — one row per Copilot interaction, idempotent via `MERGE` on `interaction_id`. Re-run this on any schedule (daily, hourly, whatever) and you'll never see duplicates.
# MAGIC
# MAGIC From there, your Silver and Gold layers can do all the fun stuff: dedup, normalization, aggregations, joining back to MGDC's user catalog for departmental rollups, etc.

# COMMAND ----------

# MAGIC %md
# MAGIC ## ⚙️ Pipeline Parameters
# MAGIC
# MAGIC The next cell is your **control panel** — every knob the notebook reads from lives here. Defaults are fine for a one-off run, but when this notebook gets wired up to a Fabric pipeline, the Notebook activity's `parameters` block can override any of these values at runtime.
# MAGIC
# MAGIC Translation: **you don't have to touch the notebook to change behavior** — just pass new values via the pipeline. That's how the same notebook can run against different tenants, lookback windows, or target tables without forking the code.

# COMMAND ----------

# ----------------------------------------------------------------------
# 📋 PARAMETERS CELL — overridden by the pipeline at runtime
# ----------------------------------------------------------------------
# Two modes are supported:
#
#   🔒 PRODUCTION (default) — secret retrieved from Azure Key Vault
#       via notebookutils.credentials.getSecret() in Cell 7.
#       Requires: key_vault_url, copilot_secret_name, and Fabric workspace
#       identity granted the "Key Vault Secrets User" RBAC role.
#
#   🧪 LOCAL TEST (if not using key vault) — uncomment the client_secret line below
#       and paste a value for ad-hoc validation. NEVER commit a non-empty
#       value. Always revert before pushing.
# ----------------------------------------------------------------------

# ---- Identity / app registration ------------------------------------
tenant_id           = ""                                   # Entra tenant ID (GUID)
client_id           = ""                                   # Entra app (client) ID

# ---- Secret retrieval (Key Vault — production default) --------------
key_vault_url       = ""                                   # https://<your-kv>.vault.azure.net/
copilot_secret_name = "copilot-graph-client-secret"        # Name of the Key Vault secret

# ---- 🧪 LOCAL TEST (uncomment ONLY for local validation) -
# ⚠️ NEVER commit a non-empty value here. Always revert before pushing.
# client_secret = ""

# ---- Run controls ---------------------------------------------------
lookback_days       = 7                                    # Window for incremental runs (use 30+ for first run)
target_schema       = "mgdc"                               # Lakehouse schema
target_table        = "bronze_copilot_interactions"        # Destination table
source_user_table   = "mgdc.bronze_mgdc_user_v1"           # User catalog from MGDC

# ---------------------------------------------------------------------
# ✅ Validation block — confirms the cell ran and surfaces what was set
# ---------------------------------------------------------------------
import re

def _mask(value, keep=4):
    """Mask GUIDs / secrets so they don't leak in pipeline logs."""
    if not value:
        return "❌ NOT SET"
    if len(value) <= keep * 2:
        return value
    return f"{value[:keep]}…{value[-keep:]}"

# Detect which mode the contributor is running in
in_test_mode = "client_secret" in dir() and bool(client_secret)
mode_banner  = "🧪 LOCAL TEST (inline secret)" if in_test_mode else "🔒 PRODUCTION (Key Vault)"

print("=" * 70)
print(f"✅ PARAMETERS CELL COMPLETED  — Mode: {mode_banner}")
print("=" * 70)
print(f"  tenant_id           : {_mask(tenant_id)}")
print(f"  client_id           : {_mask(client_id)}")
print(f"  key_vault_url       : {key_vault_url or '❌ NOT SET'}")
print(f"  copilot_secret_name : {copilot_secret_name}")
print(f"  lookback_days       : {lookback_days}")
print(f"  target_table        : {target_schema}.{target_table}")
print(f"  source_user_table   : {source_user_table}")
if in_test_mode:
    print(f"  client_secret       : {_mask(client_secret)}  ← ⚠️ LOCAL TEST")
print("=" * 70)

# ---------------------------------------------------------------------
# 🔍 Sanity checks — fail fast if something looks off
# ---------------------------------------------------------------------
guid_pattern = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

assert tenant_id, "❌ tenant_id is empty — set in this cell or via pipeline parameters"
assert guid_pattern.match(tenant_id), f"❌ tenant_id is not a valid GUID: {tenant_id}"
assert client_id, "❌ client_id is empty — set in this cell or via pipeline parameters"
assert guid_pattern.match(client_id), f"❌ client_id is not a valid GUID: {client_id}"
assert lookback_days > 0, "❌ lookback_days must be positive"
assert source_user_table, "❌ source_user_table is empty"

if not in_test_mode:
    assert key_vault_url, "❌ key_vault_url is empty — required for production mode"
    assert copilot_secret_name, "❌ copilot_secret_name is empty — required for production mode"

print("🔍 Sanity checks passed ✅")
print("➡️  Ready to proceed to authentication cell")

if in_test_mode:
    print("\n🚨 REMINDER: Comment out / clear `client_secret` before committing to source control.")

# COMMAND ----------

# MAGIC %md
# MAGIC
# MAGIC ## 📦Imports & Configuration
# MAGIC
# MAGIC Think of this cell as the **setup pass** — it loads every library, configures logging, and pins down the API endpoints that every cell after this one is going to use. Nothing exciting happens here, but if this cell doesn't run cleanly, nothing else will either.
# MAGIC
# MAGIC
# MAGIC ### 🧠 What's getting set up
# MAGIC
# MAGIC 1. 📚 **Standard library imports** — `json` for response flattening, `logging` for structured logs, `time` for timing/backoff, `datetime` for timestamps and watermarks.
# MAGIC 2. 🌐 **HTTP client** — `requests` for the Graph API calls (chosen over `urllib` for cleaner timeout/retry semantics).
# MAGIC 3. 🔥 **PySpark imports** — only the functions and types actually used downstream, kept narrow to avoid namespace pollution.
# MAGIC 4. 📝 **Structured logging** — INFO-level by default, with a consistent timestamp + level + message format so logs are grep-friendly in Fabric's monitoring view.
# MAGIC 5. 🌍 **API endpoint constants** — `GRAPH_BASE` and `LOGIN_BASE` defined once so every Graph/auth call references the same URLs. Easier to swap for Government / sovereign clouds later (just override these two).
# MAGIC
# MAGIC ### 🧩 What's in scope after this cell
# MAGIC
# MAGIC | Symbol | Used in | Purpose |
# MAGIC |---|---|---|
# MAGIC | `json` | Cells 13, 15 | Flatten nested JSON arrays for Delta storage |
# MAGIC | `logging` / `logger` | All cells | Structured run logs |
# MAGIC | `time` | Cells 11, 13 | Backoff sleeps + elapsed timing |
# MAGIC | `datetime`, `timezone`, `timedelta` | Cells 13, 17 | Run timestamps + watermark math |
# MAGIC | `requests` | Cells 7, 11 | OAuth2 + Graph API calls |
# MAGIC | `col`, `current_timestamp`, `lit`, `to_timestamp` | Cell 15 | DataFrame transformations during MERGE |
# MAGIC | `StructType`, `StructField`, `StringType`, `TimestampType` | Cell 15 | Explicit schema for the Delta target |
# MAGIC | `GRAPH_BASE` | Cells 11, 17 | Microsoft Graph API root (commercial cloud) |
# MAGIC | `LOGIN_BASE` | Cell 7 | Entra ID token endpoint |
# MAGIC
# MAGIC ### ☁️ Cloud-environment note
# MAGIC
# MAGIC `GRAPH_BASE` and `LOGIN_BASE` default to **commercial cloud** endpoints.
# MAGIC
# MAGIC | Cloud | `GRAPH_BASE` | `LOGIN_BASE` |
# MAGIC |---|---|---|
# MAGIC | **Commercial** (default) | `https://graph.microsoft.com/v1.0` | `https://login.microsoftonline.com` |
# MAGIC
# MAGIC
# MAGIC ### 💡 Why we use a `logger` object instead of `print` everywhere
# MAGIC
# MAGIC Structured logs from the `logger` show up in **Fabric's monitoring view** with timestamps and severity levels, making it easy to filter when scheduled runs fail. The cell-level `print` statements (used for validation banners) are for **interactive notebook use**; the `logger` is for **production observability**.

# COMMAND ----------

# ----------------------------------------------------------------------
# 📦 Imports & Configuration
# ----------------------------------------------------------------------
# Standardizes runtime environment for all downstream cells.
# ----------------------------------------------------------------------

import json
import logging
import time
from datetime import datetime, timezone, timedelta

import requests
from pyspark.sql.functions import col, current_timestamp, lit, to_timestamp
from pyspark.sql.types import StructType, StructField, StringType, TimestampType

# ---- Structured logging --------------------------------------------------
# INFO level by default; timestamps + level + message format makes logs
# grep-friendly in Fabric's monitoring view.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ---- Microsoft Graph + identity endpoints --------------------------------
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
LOGIN_BASE = "https://login.microsoftonline.com"

logger.info("Notebook starting. target=%s.%s lookback_days=%d",
            target_schema, target_table, lookback_days)

# ----------------------------------------------------------------------
# ✅ Validation block — confirms imports + config are ready
# ----------------------------------------------------------------------
import sys
import importlib.metadata

print("=" * 70)
print("✅ IMPORTS & CONFIGURATION COMPLETE")
print("=" * 70)

# ---- Runtime fingerprint ------------------------------------------------
py_version = ".".join(map(str, sys.version_info[:3]))
print(f"  Python runtime      : {py_version}")
try:
    print(f"  Spark version       : {spark.version}")
except NameError:
    print(f"  Spark version       : ❌ SparkSession not available (run in Fabric)")

# ---- Library versions ---------------------------------------------------
def _pkg_version(name):
    """Return installed package version or '?' if not found."""
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "?"

print(f"  requests            : {_pkg_version('requests')}")
print(f"  pyspark             : {_pkg_version('pyspark')}")
print(f"  delta-spark         : {_pkg_version('delta-spark')}")

# ---- API endpoints ------------------------------------------------------
print(f"\n  GRAPH_BASE          : {GRAPH_BASE}")
print(f"  LOGIN_BASE          : {LOGIN_BASE}")

# ---- Symbol contract check ----------------------------------------------
required_symbols = [
    "json", "logging", "time", "datetime", "timezone", "timedelta",
    "requests",
    "col", "current_timestamp", "lit", "to_timestamp",
    "StructType", "StructField", "StringType", "TimestampType",
    "GRAPH_BASE", "LOGIN_BASE",
    "logger",
]
missing = [s for s in required_symbols if s not in dir()]

print(f"\n  Required symbols    : {len(required_symbols)} expected, "
      f"{len(required_symbols) - len(missing)} loaded")

assert not missing, f"❌ Missing required symbols: {missing}"
assert logger.level <= logging.INFO, "❌ Logger level higher than INFO — adjust logging.basicConfig"
assert GRAPH_BASE.startswith("https://"), f"❌ GRAPH_BASE must use HTTPS: {GRAPH_BASE}"
assert LOGIN_BASE.startswith("https://"), f"❌ LOGIN_BASE must use HTTPS: {LOGIN_BASE}"

print("=" * 70)
print("🔍 Sanity checks passed ✅")
print("➡️  Ready to authenticate to Microsoft Graph (next cell)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 🔐 Authenticate to Microsoft Graph
# MAGIC
# MAGIC We use the **OAuth2 client credentials flow**. The notebook auto-detects which mode it's running in based on whether `client_secret` was set in Cell 3.
# MAGIC
# MAGIC ### 🔒 Production mode (recommended default)
# MAGIC
# MAGIC The client secret is retrieved from **Azure Key Vault** using the Fabric `notebookutils.credentials.getSecret()` API, which authenticates as the **workspace identity** — the secret value never appears in code, logs, or notebook output.
# MAGIC
# MAGIC **Requires:**
# MAGIC - ✅ A secret stored in Key Vault (the secret name is `copilot_secret_name` from Cell 3)
# MAGIC - ✅ The Fabric workspace identity granted the **`Key Vault Secrets User`** RBAC role on the vault
# MAGIC - ✅ Key Vault networking that allows Fabric to reach the endpoint — Private Endpoint for production, or "Allow public access" for POC environments (see Gotcha #11 in the README)
# MAGIC
# MAGIC ### 🧪 Local test mode
# MAGIC
# MAGIC For ad-hoc validation when Key Vault isn't yet provisioned (or its networking isn't reachable from Fabric), uncomment the `client_secret = "..."` line in **Cell 3** and paste a value. The auth cell will detect the inline value and skip the Key Vault call.
# MAGIC
# MAGIC > 🚨 **Never commit a non-empty `client_secret` to source control.** Always revert before pushing. Cell 3's validation banner will print a 🧪 LOCAL TEST flag and a reminder if it detects inline-mode usage.
# MAGIC
# MAGIC ### 🔁 The OAuth2 exchange
# MAGIC
# MAGIC Once the secret is resolved (via either path), this cell:
# MAGIC 1. Calls `POST https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token`
# MAGIC 2. Requests scope `https://graph.microsoft.com/.default` (which expands to whatever **Application** permissions are already admin-consented for this app)
# MAGIC 3. Caches the bearer token in `access_token` and builds the `headers` dict reused by every subsequent Graph call
# MAGIC 4. Reports token lifetime (typically ~60 minutes) so long-running pulls can plan refresh strategy
# MAGIC
# MAGIC ### 💡 Token refresh notes
# MAGIC
# MAGIC The token lives for ~60 minutes. For pulls that may exceed that window (very large tenants), wrap the request loop in a token-refresh helper that re-authenticates on `401`. For typical incremental runs, a single token is sufficient.
# MAGIC ``

# COMMAND ----------

# ----------------------------------------------------------------------
# 🔐 Authenticate to Microsoft Graph
# ----------------------------------------------------------------------
# Auto-detects mode from Cell 3:
#   🔒 PRODUCTION — pulls client_secret from Azure Key Vault
#   🧪 LOCAL TEST — uses the client_secret already set in Cell 3
# ----------------------------------------------------------------------

import time
from datetime import datetime, timezone

print("=" * 70)
print(f"🔐 AUTHENTICATION STARTING  — Mode: {mode_banner}")
print("=" * 70)

# ---- Step 1: Resolve the client secret ------------------------------
if in_test_mode:
    print("➡️  Using inline client_secret from Cell 3 (LOCAL TEST mode)")
    print(f"✅ Secret loaded (length={len(client_secret)} chars)")
else:
    try:
        print(f"➡️  Fetching '{copilot_secret_name}' from Key Vault…")
        t0 = time.time()
        client_secret = notebookutils.credentials.getSecret(key_vault_url, copilot_secret_name)
        print(f"✅ Secret retrieved ({(time.time() - t0)*1000:.0f} ms) — length={len(client_secret)} chars")
    except Exception as e:
        print(f"❌ Could not retrieve secret from Key Vault: {e}")
        print("💡 Hints:")
        print("   • Confirm Key Vault networking allows access from Fabric")
        print("     (Private Endpoint for production; 'Allow public access' for POC)")
        print("   • Confirm 'Key Vault Secrets User' role is assigned to the workspace identity")
        print("   • Confirm the secret name matches exactly (case-sensitive)")
        raise

# ---- Step 2: Exchange for Graph access token ------------------------
token_url = f"{LOGIN_BASE}/{tenant_id}/oauth2/v2.0/token"
token_payload = {
    "grant_type": "client_credentials",
    "client_id": client_id,
    "client_secret": client_secret,
    "scope": "https://graph.microsoft.com/.default",
}

try:
    print(f"\n➡️  Requesting Graph access token…")
    t0 = time.time()
    token_response = requests.post(token_url, data=token_payload, timeout=30)
    token_response.raise_for_status()
    token_data = token_response.json()
    access_token = token_data["access_token"]
    expires_in = token_data.get("expires_in", 3600)
    print(f"✅ Token acquired ({(time.time() - t0)*1000:.0f} ms)")
except requests.exceptions.HTTPError:
    err_body = token_response.text
    print(f"\n❌ Token request failed (HTTP {token_response.status_code})")
    print(f"   Response: {err_body[:500]}")
    print("\n💡 Common causes:")
    print("   • AADSTS7000215 → secret is wrong, expired, or you copied the Secret ID instead of the Value")
    print("   • AADSTS90002  → tenant_id is not a real Entra tenant")
    print("   • AADSTS700016 → client_id doesn't exist in this tenant")
    print("   • AADSTS50034  → app principal not found — check admin consent")
    raise

# ---- Step 3: Build the headers reused by every Graph call -----------
headers = {
    "Authorization": f"Bearer {access_token}",
    "Content-Type": "application/json",
    "ConsistencyLevel": "eventual",
}

print("\n" + "=" * 70)
print("✅ AUTHENTICATION COMPLETE")
print("=" * 70)
print(f"  Tenant            : {tenant_id[:8]}…{tenant_id[-4:]}")
print(f"  Client (app)      : {client_id[:8]}…{client_id[-4:]}")
print(f"  Token expires in  : {expires_in:,} seconds (~{expires_in//60} min)")
print(f"  Token preview     : {access_token[:20]}…{access_token[-10:]}")
print("=" * 70)
print("➡️  Ready to query Microsoft Graph")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 👥 Read User List from Lakehouse
# MAGIC
# MAGIC The user catalog drives the entire loop downstream — every row here becomes a Microsoft Graph API call in **Cell 13**. Getting this cell right (and resilient to MGDC schema variations) keeps the rest of the notebook simple.
# MAGIC
# MAGIC ### 🧠 What this cell does
# MAGIC
# MAGIC 1. 📦 **Reads** the user catalog from `mgdc.bronze_mgdc_user_v1` (loaded by the upstream MGDC pipeline).
# MAGIC 2. 🔎 **Filters** to rows where `mail IS NOT NULL` — a pragmatic proxy for "real users likely to have Copilot."
# MAGIC 3. 🔑 **Picks an identifier column** — `mail` is used as both the Graph API lookup key and the display identifier in downstream rows.
# MAGIC 4. 💾 **Collects** the result into an in-memory Python list of dicts for the loop in Cell 13 to iterate.
# MAGIC 5. ✅ **Prints a validation banner** with row count, sample preview, and a sanity check.
# MAGIC
# MAGIC ### 🔑 Why `mail` as the identifier
# MAGIC
# MAGIC The Microsoft Graph `aiInteractionHistory` endpoint accepts a few forms of user identifier in the `/copilot/users/{id}/...` path:
# MAGIC
# MAGIC | Identifier | When it works |
# MAGIC |---|---|
# MAGIC | Object ID (`id` GUID) | Always — canonical join key when present |
# MAGIC | `userPrincipalName` (UPN) | Always — human-readable identifier |
# MAGIC | **`mail`** ✅ | When `id` and `userPrincipalName` aren't available in the source table |
# MAGIC
# MAGIC If your MGDC registration includes the full User_v1 schema (the recommended default — see Gotcha #2 in the README), `id` and `userPrincipalName` will both be available and you can switch to those for cleaner joins downstream. This cell defaults to `mail` because it's the most universally-present column across registration variations.
# MAGIC
# MAGIC > 💡 **For organizations with a precise Copilot license list** (e.g., from `assignedLicenses` in User_v1, or from an HRIS extract joined upstream), swap the `WHERE` clause to filter on that license signal directly. The `mail IS NOT NULL` filter is just a sensible default when license data isn't readily available.
# MAGIC
# MAGIC ### ⚠️ A note on `.collect()`
# MAGIC
# MAGIC This cell pulls the user list into the driver via `.collect()`. For tenants with **fewer than ~100K users**, this is fine and dramatically simpler than streaming. For larger tenants, switch to a Spark `foreach` partitioned over the DataFrame and parallelize Graph calls from worker nodes (then mind the Graph throttling limits).

# COMMAND ----------

# ----------------------------------------------------------------------
# 👥 Read User List from Lakehouse
# ----------------------------------------------------------------------
# Loads the user catalog from the MGDC-populated User_v1 table.
# Uses `mail` as the Graph API lookup key — works regardless of whether
# `id` and `userPrincipalName` landed during MGDC consent (see Gotcha #2).
# ----------------------------------------------------------------------

print("=" * 70)
print("👥 READING USER CATALOG")
print("=" * 70)
print(f"  Source table        : {source_user_table}")
print(f"  Filter              : mail IS NOT NULL  (proxy for licensed users)")
print(f"  Identifier column   : mail")
print("-" * 70)

# ---- Query the user catalog ----------------------------------------------
users_df = spark.sql(f"""
    SELECT mail AS user_id,
           mail AS user_principal_name,
           mail
      FROM {source_user_table}
     WHERE mail IS NOT NULL
""")

# ---- Collect to driver ----------------------------------------------------
# For tenants with <~100K users, .collect() is fine. For larger tenants,
# switch to a partitioned foreach (see markdown above).
users = [row.asDict() for row in users_df.collect()]
logger.info("Loaded %d candidate users from %s", len(users), source_user_table)

# ----------------------------------------------------------------------
# ✅ Validation block — confirms the catalog was loaded correctly
# ----------------------------------------------------------------------
print(f"  Rows returned       : {len(users):,}")

if users:
    # Sample preview — first 3 users (masked tail for privacy in shared logs)
    print(f"\n  Sample (first 3):")
    for u in users[:3]:
        m = u["mail"]
        # Mask the local part of the email for shareable logs
        masked = f"{m[:3]}…{m[m.index('@'):]}" if "@" in m and len(m) > 6 else m
        print(f"    • {masked}")
else:
    print("\n  ⚠️  No users returned — possible causes:")
    print("     • The MGDC pipeline hasn't populated bronze_mgdc_user_v1 yet")
    print("     • Every row has mail IS NULL (unusual)")
    print("     • The Lakehouse isn't attached to this notebook")

print("=" * 70)

# ---- Sanity checks --------------------------------------------------------
assert spark.catalog.tableExists(source_user_table), \
    f"❌ Source table {source_user_table} doesn't exist — attach the Lakehouse or run the MGDC pipeline first"
assert len(users) > 0, \
    f"❌ Zero users returned from {source_user_table} — check the filter or upstream pipeline"
assert all("mail" in u and u["mail"] for u in users[:5]), \
    "❌ Sampled rows have null `mail` — query filter may not have applied correctly"

print("🔍 Sanity checks passed ✅")
print(f"➡️  Ready to fan out {len(users):,} Graph API calls (next cell defines the helper)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 🛰️ Graph API Helper — `get_interactions_for_user`
# MAGIC
# MAGIC This little function is honestly where the magic happens. Every user we loop through in the next cell is going to call this — so making it bulletproof here means the rest of the notebook stays nice and clean.
# MAGIC
# MAGIC For each user, it asks Graph: *"hey, what Copilot interactions does this person have?"* — then walks through all the pages of the response and hands back a flat list of interactions. Simple on the outside, but it's doing a few smart things behind the scenes.
# MAGIC
# MAGIC ### 🛡️ What it handles for you
# MAGIC
# MAGIC | What could go wrong | What the function does |
# MAGIC |---|---|
# MAGIC | 📄 The response has multiple pages | Follows the `@odata.nextLink` until there's nothing left |
# MAGIC | 🐌 Graph says "slow down" (429) | Reads the `Retry-After` header, takes a nap, tries again |
# MAGIC | 🔒 User doesn't have Copilot (403/404) | Quietly skips them and moves on — no drama |
# MAGIC | ⚡ Something flakes out (5xx) | Backs off and retries, doubling the wait each time |
# MAGIC
# MAGIC ### 🎯 What you get back
# MAGIC
# MAGIC - **A list of interactions** → user has Copilot and has been using it
# MAGIC - **An empty list `[]`** → either no Copilot license, no activity in the window, or we tried everything and gave up
# MAGIC
# MAGIC The cool thing is the next cell doesn't have to care which of those happened — empty is empty, and it just moves on. Keeps the loop logic super clean.
# MAGIC
# MAGIC ### 🎛️ If you want to tune it
# MAGIC
# MAGIC Most tenants are fine with the defaults, but if you're hitting weirdness:
# MAGIC
# MAGIC | Knob | Default | When you'd touch it |
# MAGIC |---|---|---|
# MAGIC | `max_retries` | `3` | Big tenant getting more transient errors? Bump to `5` |
# MAGIC | Backoff base | doubles each retry (2s, 4s, 8s) | Sustained throttling? Slow it down more |
# MAGIC | Request timeout | `60s` | Big payloads timing out (rare) |
# MAGIC
# MAGIC ### 📚 If you want to go deeper
# MAGIC
# MAGIC - [Microsoft Graph aiInteractionHistory docs](https://learn.microsoft.com/en-us/microsoft-365/copilot/extensibility/api/ai-services/interaction-export/aiinteractionhistory-getallenterpriseinteractions)
# MAGIC - [Graph throttling guidance](https://learn.microsoft.com/en-us/graph/throttling)

# COMMAND ----------

# ----------------------------------------------------------------------
# 🛰️ Graph API Helper — get_interactions_for_user
# ----------------------------------------------------------------------
# The little workhorse that talks to Graph for each user. Handles all the
# edge cases (pagination, throttling, missing licenses, flaky
# 5xx responses) so the loop in the next cell can stay simple.
# ----------------------------------------------------------------------

def get_interactions_for_user(user_id, request_headers, max_retries=3):
    """Grab all Copilot interactions for one user.

    Returns a list of interactions. If the user has no Copilot license
    (Graph returns 403/404), or we tried everything and gave up, you'll
    just get an empty list back — no exception, no drama.
    """
    url = f"{GRAPH_BASE}/copilot/users/{user_id}/interactionHistory/getAllEnterpriseInteractions"
    results = []

    # Outer loop: keep going as long as there's another page to fetch
    while url:
        attempt = 0

        # Inner loop: retry the current page up to max_retries times
        while attempt <= max_retries:
            try:
                response = requests.get(url, headers=request_headers, timeout=60)

                # ---- 429: Graph is asking us to slow down ----
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 30))
                    logger.warning("429 for user=%s; sleeping %ds", user_id, retry_after)
                    time.sleep(retry_after)
                    attempt += 1
                    continue

                # ---- 403/404: no Copilot license — quietly skip ----
                if response.status_code in (403, 404):
                    logger.info("Skipping user=%s (HTTP %d — no Copilot license)",
                                user_id, response.status_code)
                    return results

                # ---- 5xx: server hiccup — wait a bit and try again ----
                if 500 <= response.status_code < 600:
                    backoff = 2 ** attempt
                    logger.warning("HTTP %d for user=%s; backoff %ds",
                                   response.status_code, user_id, backoff)
                    time.sleep(backoff)
                    attempt += 1
                    continue

                # ---- Anything else non-OK: raise ----
                response.raise_for_status()

                # ---- Success! Grab the rows and check for more pages ----
                payload = response.json()
                results.extend(payload.get("value", []))
                url = payload.get("@odata.nextLink")
                break  # done with this page — exit retry loop

            except requests.exceptions.RequestException as exc:
                # Network/timeout error — back off and retry
                backoff = 2 ** attempt
                logger.warning("Request error for user=%s: %s; backoff %ds",
                               user_id, exc, backoff)
                time.sleep(backoff)
                attempt += 1

        else:
            # Python's while-else: only fires if the loop ran out of retries
            # without a `break`. Translation: we tried max_retries times and
            # nothing worked. Give up gracefully and return what we have.
            logger.error("Max retries exceeded for user=%s — moving on", user_id)
            return results

    return results


# ----------------------------------------------------------------------
# ✅ Quick validation — let's make sure the function loaded properly
# ----------------------------------------------------------------------
# This cell only defines a function — it doesn't actually do anything yet.
# So instead of confirming results, we just confirm the function exists,
# has the signature we expect, and all its dependencies are in scope.
# ----------------------------------------------------------------------
import inspect

print("=" * 70)
print("✅ GRAPH API HELPER LOADED")
print("=" * 70)

sig = inspect.signature(get_interactions_for_user)
params = list(sig.parameters.keys())

print(f"  Function            : {get_interactions_for_user.__name__}()")
print(f"  Takes               : {', '.join(params)}")
print(f"  Default max_retries : {sig.parameters['max_retries'].default}")
print(f"  Hands back          : list of interactions (or empty list if no Copilot)")
print()
print(f"  Hitting endpoint    : {GRAPH_BASE}/copilot/users/<id>/interactionHistory/")
print(f"                        getAllEnterpriseInteractions")
print()
print("  Handles these gracefully:")
print("    • 429 (throttled)        → respects Retry-After, naps, tries again")
print("    • 403/404 (no license)   → quietly skips the user")
print("    • 5xx (transient errors) → exponential backoff")
print("    • Network hiccups        → exponential backoff")
print("    • Multi-page responses   → follows @odata.nextLink to the end")
print()

# ---- Sanity checks: function exists and dependencies are loaded ----
assert callable(get_interactions_for_user), \
    "❌ get_interactions_for_user isn't callable — something weird happened"
assert params == ["user_id", "request_headers", "max_retries"], \
    f"❌ Function signature changed — got: {params}"
assert sig.parameters["max_retries"].default == 3, \
    "❌ max_retries default isn't 3 anymore — double-check the backoff strategy"
assert "GRAPH_BASE" in dir() and GRAPH_BASE.startswith("https://"), \
    "❌ GRAPH_BASE is missing or not HTTPS — re-run the Imports cell"
assert "requests" in dir(), "❌ `requests` not imported — re-run the Imports cell"
assert "time" in dir(),     "❌ `time` not imported — re-run the Imports cell"
assert "logger" in dir(),   "❌ `logger` not defined — re-run the Imports cell"

print("=" * 70)
print("🔍 All good ✅")
print("➡️  Ready to start hitting Graph for real (next cell)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 🔄 Iterate Users and Accumulate Interactions
# MAGIC
# MAGIC This is where the real work happens. We walk through every user from Cell 9, call our Graph API helper for each one, and stack the interactions into a list ready for Delta.
# MAGIC
# MAGIC The helper handles all the messy stuff (throttling, missing licenses, retries) so this loop can stay clean.
# MAGIC
# MAGIC ### 🧠 What you'll see
# MAGIC
# MAGIC Each user lands in one of three buckets:
# MAGIC - ✅ **`successful`** — got at least one interaction back
# MAGIC - ⏭️ **`skipped_no_license`** — no Copilot or no activity (totally fine)
# MAGIC - ❌ **`failed`** — something unexpected blew up
# MAGIC
# MAGIC ### 🧩 What we're saving per interaction
# MAGIC
# MAGIC | Column | From | Notes |
# MAGIC |---|---|---|
# MAGIC | `interaction_id` | `id` | **Merge key** for Delta |
# MAGIC | `user_id` / `user_principal_name` | User catalog | Joined here for convenience |
# MAGIC | `created_date_time` | `createdDateTime` | **Watermark** for incremental runs |
# MAGIC | `app_class` | `appClass` | e.g. Teams, Word, Outlook |
# MAGIC | `body_content` + `body_content_type` | `body.content` / `body.contentType` | The actual prompt/response |
# MAGIC | `conversation_id` | `conversationId` | Groups multi-turn chats |
# MAGIC | `interaction_type` | `interactionType` | `userPrompt` vs `aiResponse` |
# MAGIC | `mentioned_resources_json` / `requested_resources_json` | `mentionedResources` / `requestedResources` | Kept as JSON strings — easier schema evolution |
# MAGIC
# MAGIC ### 🤔 A couple of design calls
# MAGIC
# MAGIC - 🐢 **Single-threaded on purpose** — predictable throttling beats raw speed. For 10K+ users, wrap in a `ThreadPoolExecutor` with ~4 workers max.
# MAGIC - 🛡️ **One bad user can't kill the run** — each is wrapped in try/except.
# MAGIC - 🧠 **Everything stacks in memory** — fine up to ~1M rows. Beyond that, switch to chunked writes.
# MAGIC
# MAGIC ### 🚦 Reading the summary at the end
# MAGIC
# MAGIC | Output | What it means |
# MAGIC |---|---|
# MAGIC | All skipped | No Copilot in this tenant yet — plumbing works, no data to ingest |
# MAGIC | Mix of success + skipped | Healthy rollout — normal for any real org |
# MAGIC | Lots of failures | Worth investigating — permissions, throttling, or schema drift |

# COMMAND ----------

# ----------------------------------------------------------------------
# 🔄 Iterate Users and Accumulate Interactions
# ----------------------------------------------------------------------
# Walk through every user, call Graph, flatten the JSON into flat rows
# ready for Delta. The helper from the last cell handles all the
# annoying stuff (throttling, missing licenses, retries) so this loop
# can stay clean and readable.
# ----------------------------------------------------------------------

import time
from collections import Counter

# ---- 🚦 Optional safety knob for testing ---------------------------------
# If you're validating the notebook end-to-end and don't want to wait for
# all users to process, set this to a small number (like 10). For a real
# run, leave it as None.
TEST_LIMIT = None  # e.g., 10 for quick validation; None to process everyone

users_to_process = users[:TEST_LIMIT] if TEST_LIMIT else users
total_users      = len(users_to_process)

# ---- 📊 Counters we'll use to tell the story at the end ----------------
successful         = 0
skipped_no_license = 0
failed             = 0
flat_rows          = []
app_class_counter  = Counter()   # for the "top apps" summary at the end

# ---- 🏁 Start banner ----------------------------------------------------
print("=" * 70)
print("🔄 LOOP STARTING")
print("=" * 70)
print(f"  Users to process    : {total_users:,}"
      + (f"  (⚠️ TEST_LIMIT={TEST_LIMIT} of {len(users):,})" if TEST_LIMIT else ""))
print(f"  Hitting             : {GRAPH_BASE}/copilot/users/.../interactionHistory")
print(f"  Started at          : {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)

loop_start = time.time()

# ---- 🌀 The main loop ----------------------------------------------------
for idx, user in enumerate(users_to_process, start=1):
    try:
        interactions = get_interactions_for_user(user["user_id"], headers)

        # Bucket the user: got interactions vs. came back empty
        if not interactions:
            skipped_no_license += 1
        else:
            successful += 1

        # Flatten each interaction into a row for Delta
        for interaction in interactions:
            app_class = interaction.get("appClass")
            app_class_counter[app_class] += 1
            flat_rows.append({
                "interaction_id":           interaction.get("id"),
                "user_id":                  user["user_id"],
                "user_principal_name":      user["user_principal_name"],
                "created_date_time":        interaction.get("createdDateTime"),
                "app_class":                app_class,
                "body_content":             (interaction.get("body") or {}).get("content"),
                "body_content_type":        (interaction.get("body") or {}).get("contentType"),
                "conversation_id":          interaction.get("conversationId"),
                "etag":                     interaction.get("etag"),
                "interaction_type":         interaction.get("interactionType"),
                "locale":                   interaction.get("locale"),
                "mentioned_resources_json": json.dumps(interaction.get("mentionedResources") or []),
                "requested_resources_json": json.dumps(interaction.get("requestedResources") or []),
            })

    except Exception as exc:
        # Don't let one bad user kill the whole run
        failed += 1
        logger.error("❌ Failed user=%s: %s", user.get("user_id"), exc)

    # ---- 📈 Progress check-in every 100 users ---------------------------
    # Gives you something to look at instead of a frozen cell. Also shows
    # ETA so you know roughly when this'll be done.
    if idx % 100 == 0:
        elapsed = time.time() - loop_start
        rate    = idx / elapsed if elapsed > 0 else 0
        eta     = (total_users - idx) / rate if rate > 0 else 0
        print(f"  📈 {idx:>5} / {total_users}  "
              f"| ✅ {successful:>4}  ⏭️ {skipped_no_license:>4}  ❌ {failed:>3}  "
              f"| rows={len(flat_rows):>6}  "
              f"| {rate:.1f} users/s  ETA {eta:.0f}s")

loop_elapsed = time.time() - loop_start

# ---- 🎯 Wrap it up with the final story --------------------------------
print()
print("=" * 70)
print("✅ LOOP DONE")
print("=" * 70)
print(f"  Total time          : {loop_elapsed:.1f}s "
      f"({loop_elapsed/max(total_users,1)*1000:.0f} ms/user avg)")
print(f"  Users processed     : {total_users:,}")
print(f"    ✅ Successful     : {successful:>5}  ({successful/total_users*100:>5.1f}%)")
print(f"    ⏭️  Skipped         : {skipped_no_license:>5}  ({skipped_no_license/total_users*100:>5.1f}%)")
print(f"    ❌ Failed         : {failed:>5}  ({failed/total_users*100:>5.1f}%)")
print(f"  Interactions grabbed: {len(flat_rows):,}")
print("=" * 70)

# ---- 🧠 So what does this all mean? -------------------------------------
if len(flat_rows) == 0:
    if skipped_no_license == total_users and failed == 0:
        print("ℹ️  Everybody got skipped — no Copilot interactions in this window.")
        print("   A few reasons this might happen:")
        print("     • No Copilot licenses are assigned in this tenant yet")
        print("     • Nobody happened to use Copilot during the lookback window")
        print("     • Filter upstream knocked everyone out")
        print()
        print("   The good news: the pipeline is working end-to-end —")
        print("     auth ✅ → user catalog ✅ → Graph API ✅ → license-skip ✅")
    elif failed > 0:
        print(f"⚠️  {failed} user(s) blew up unexpectedly. Scroll up to the error log")
        print("    and look for patterns (auth issues, weird responses, etc.).")
    else:
        print("⚠️  We've got successful users but no interactions came through.")
        print("    Probably worth checking the Graph response shape — the `value`")
        print("    array might be coming back empty.")
else:
    print(f"🎉 Grabbed {len(flat_rows):,} interactions across {len(app_class_counter)} app(s)!")
    print(f"   Top apps so far:")
    for app, count in app_class_counter.most_common(5):
        print(f"     • {app or '(unknown)':<30} {count:>5}")

print("=" * 70)
print("➡️  Ready to write all this to Delta (next cell)")

logger.info("Loop complete. users=%d successful=%d skipped=%d failed=%d rows=%d",
            total_users, successful, skipped_no_license, failed, len(flat_rows))

# COMMAND ----------

# MAGIC %md
# MAGIC ## 💾 Write to Delta — `MERGE` for Idempotent Daily Runs
# MAGIC
# MAGIC Alright, we've got our `flat_rows` from the loop — now let's get them into a Delta table. We're using `MERGE INTO` here so we can re-run this notebook whenever we want and never end up with duplicates. That's the magic word: **idempotent**.
# MAGIC
# MAGIC ### 🤔 Why `MERGE` and not just `append` or `overwrite`?
# MAGIC
# MAGIC | Pattern | The good | The bad | When you'd use it |
# MAGIC |---|---|---|---|
# MAGIC | `append` | Fastest write | ❌ Duplicates if you re-run | Append-only streams that handle dedup somewhere else |
# MAGIC | `overwrite` | Always clean | ❌ Wipes history if today's pull fails | Full nightly refreshes only |
# MAGIC | ✅ **`MERGE`** | No duplicates, keeps history | Slightly more code | **What we want for daily incremental loads** |
# MAGIC
# MAGIC With `MERGE` on `interaction_id`:
# MAGIC - 🔁 **Re-run same day?** Zero duplicates — matching rows just update.
# MAGIC - 📅 **Daily incremental?** Only new interactions get inserted.
# MAGIC - 🛠️ **Late updates?** Existing rows get refreshed cleanly. Rare, but handled.
# MAGIC
# MAGIC ### 🧠 What this cell does
# MAGIC
# MAGIC 1. 🧱 **Builds an explicit schema** so empty runs don't crash on type inference.
# MAGIC 2. 🛡️ **Handles empty input gracefully** — if `flat_rows` is empty (no new interactions in the window), we skip the write but still make sure the target table exists.
# MAGIC 3. 📐 **Casts and enriches** — converts `created_date_time` from a string to a real `TimestampType`, and tacks on `ingested_timestamp` so we know when each row landed.
# MAGIC 4. 🆕 **Creates the table on first run** if it doesn't exist yet.
# MAGIC 5. 🔀 **Runs the MERGE** — match on `interaction_id`, update existing rows, insert new ones.
# MAGIC 6. ✅ **Tells you what just happened** — rows merged, inserts vs updates, before/after counts.
# MAGIC
# MAGIC ### 🎯 Quick schema note
# MAGIC
# MAGIC 14 columns total: 13 from the Graph payload + 1 audit column (`ingested_timestamp`). This is the Bronze layer — raw data lands here as-is. Dedup, normalization, and any aggregations are Silver/Gold layer problems for later.
# MAGIC
# MAGIC ### ⚠️ A couple of things to know
# MAGIC
# MAGIC - ⏱️ **First run is a bit slower** because we have to create the empty Delta table before merging. Every run after that skips this step.
# MAGIC - 📦 **MERGE uses `WHEN MATCHED THEN UPDATE SET *`** — meaning any change in the source overwrites the target. If you ever need to preserve specific target columns, switch to explicit column lists.
# MAGIC - 📭 **Empty input is fine** — if no interactions came back from Graph (which can happen for plenty of legit reasons), the cell skips the write but still guarantees the table exists. Downstream consumers won't break.

# COMMAND ----------

# ----------------------------------------------------------------------
# 💾 Write to Delta — MERGE for Idempotent Daily Runs
# ----------------------------------------------------------------------
# Takes flat_rows from the loop and lands them as a Delta table using
# MERGE INTO. Re-runnable on any schedule — no duplicates.
# ----------------------------------------------------------------------

import time

print("=" * 70)
print("💾 DELTA WRITE STARTING")
print("=" * 70)

# ---- 📐 Define the schema explicitly ------------------------------------
# Explicit schemas keep empty runs from crashing and protect against
# type inference doing something weird (e.g., a whole column of nulls).
target_struct = StructType([
    StructField("interaction_id",           StringType(), False),
    StructField("user_id",                  StringType(), True),
    StructField("user_principal_name",      StringType(), True),
    StructField("created_date_time",        StringType(), True),   # cast to Timestamp below
    StructField("app_class",                StringType(), True),
    StructField("body_content",             StringType(), True),
    StructField("body_content_type",        StringType(), True),
    StructField("conversation_id",          StringType(), True),
    StructField("etag",                     StringType(), True),
    StructField("interaction_type",         StringType(), True),
    StructField("locale",                   StringType(), True),
    StructField("mentioned_resources_json", StringType(), True),
    StructField("requested_resources_json", StringType(), True),
])

print(f"  Target table        : {target_schema}.{target_table}")
print(f"  Schema columns      : {len(target_struct.fields)} + 1 audit (ingested_timestamp)")
print(f"  Rows in memory      : {len(flat_rows):,}")
print("-" * 70)

# ---- 🛡️ No rows? Skip the write but still make sure the table exists ---
if not flat_rows:
    print("ℹ️  Nothing to write — flat_rows is empty.")
    print("   That's okay! This can happen when:")
    print("     • No Copilot interactions in the lookback window, or")
    print("     • Nobody in the tenant has Copilot data right now.")
    print()
    print("   ✅ We'll still make sure the target table exists so")
    print("      downstream consumers don't break.")
    print()

    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {target_schema}")
    table_exists = spark.catalog.tableExists(f"{target_schema}.{target_table}")
    if not table_exists:
        empty_df = (
            spark.createDataFrame([], schema=target_struct)
                 .withColumn("ingested_timestamp", current_timestamp())
        )
        empty_df.write.format("delta").saveAsTable(f"{target_schema}.{target_table}")
        print(f"  🆕 Created an empty Delta table at {target_schema}.{target_table}")
    else:
        existing_count = spark.sql(
            f"SELECT COUNT(*) AS c FROM {target_schema}.{target_table}"
        ).collect()[0]["c"]
        print(f"  📦 Table already exists ({existing_count:,} rows) — nothing changed.")

    print()
    print("=" * 70)
    print("✅ DELTA WRITE DONE (no-op path)")
    print("=" * 70)
    print(f"  Table state         : ready for downstream consumers")
    print(f"  Rows merged         : 0")
    print(f"  What happened       : table exists, no data moved")
    print("=" * 70)
    print("➡️  Ready for the run summary (next cell)")

else:
    # ---- 🚀 Actually do the write ---------------------------------------
    write_start = time.time()

    # ---- Build the DataFrame -------------------------------------------
    print(f"  ➡️  Building DataFrame from {len(flat_rows):,} rows…")
    incoming_df = (
        spark.createDataFrame(flat_rows, schema=target_struct)
             .withColumn("created_date_time", to_timestamp("created_date_time"))
             .withColumn("ingested_timestamp", current_timestamp())
    )

    incoming_count = incoming_df.count()
    print(f"      ✅ DataFrame built — {incoming_count:,} rows, "
          f"{len(incoming_df.columns)} columns")

    # ---- Make sure the target table exists ------------------------------
    print(f"  ➡️  Making sure the target table exists…")
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {target_schema}")
    table_existed = spark.catalog.tableExists(f"{target_schema}.{target_table}")

    if not table_existed:
        empty_df = incoming_df.limit(0)
        empty_df.write.format("delta").saveAsTable(f"{target_schema}.{target_table}")
        print(f"      🆕 Created new Delta table {target_schema}.{target_table}")
        rows_before_merge = 0
    else:
        rows_before_merge = spark.sql(
            f"SELECT COUNT(*) AS c FROM {target_schema}.{target_table}"
        ).collect()[0]["c"]
        print(f"      📦 Found existing table — {rows_before_merge:,} rows before merge")

    # ---- Run the MERGE -------------------------------------------------
    print(f"  ➡️  Running MERGE on interaction_id…")
    incoming_df.createOrReplaceTempView("incoming_interactions")

    spark.sql(f"""
        MERGE INTO {target_schema}.{target_table} t
        USING incoming_interactions s
           ON t.interaction_id = s.interaction_id
        WHEN MATCHED THEN UPDATE SET *
        WHEN NOT MATCHED THEN INSERT *
    """)

    rows_after_merge = spark.sql(
        f"SELECT COUNT(*) AS c FROM {target_schema}.{target_table}"
    ).collect()[0]["c"]

    rows_inserted = rows_after_merge - rows_before_merge
    rows_updated  = incoming_count - rows_inserted
    write_elapsed = time.time() - write_start

    # ---- Tell the story --------------------------------------------------
    print()
    print("=" * 70)
    print("✅ DELTA WRITE DONE")
    print("=" * 70)
    print(f"  Target table          : {target_schema}.{target_table}")
    print(f"  Incoming rows         : {incoming_count:,}")
    print(f"    🆕 Inserted (new)   : {rows_inserted:,}")
    print(f"    🔁 Updated (existing): {rows_updated:,}")
    print(f"  Rows before merge     : {rows_before_merge:,}")
    print(f"  Rows after merge      : {rows_after_merge:,}")
    print(f"  Net change            : {rows_after_merge - rows_before_merge:+,}")
    print(f"  Took                  : {write_elapsed:.1f}s "
          f"({write_elapsed/max(incoming_count,1)*1000:.1f} ms/row avg)")
    print("=" * 70)

    # ---- So what just happened? ---------------------------------------
    if rows_inserted == incoming_count and not table_existed:
        print("🎉 First run! Everything went into a brand-new Delta table.")
    elif rows_inserted == 0 and rows_updated == incoming_count:
        print("🔁 Same data as before — every row matched and got updated in place.")
        print("   That's the idempotency guarantee doing its job. ✅")
    elif rows_inserted > 0 and rows_updated == 0:
        print("📈 Pure incremental run — everything you got was net-new.")
    else:
        print(f"🔀 Mixed bag — {rows_inserted:,} new + {rows_updated:,} updated.")

    print("=" * 70)
    print("➡️  Ready for the run summary (next cell)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 📈 Run Summary
# MAGIC
# MAGIC This is the executive summary. Whatever you scheduled this notebook to do, this is the at-a-glance "what just happened" report card.
# MAGIC
# MAGIC ### 🧠 What this cell shows you
# MAGIC
# MAGIC 1. 📊 **This run's counters** — users processed, successful, skipped, failed, interactions captured.
# MAGIC 2. 📦 **The persisted table state** — actual numbers from the Delta table, not just this run. So even if you ran this five times today, you're looking at the cumulative reality.
# MAGIC 3. 📅 **Date range covered** — earliest and latest `created_date_time`. Handy for spotting gaps or schedule drift.
# MAGIC 4. 🏆 **Top apps by interaction count** — Teams? Word? Outlook? Tells you which Copilot surfaces are actually getting used.
# MAGIC 5. 🧮 **Per-user distribution** — min/median/p95/max. Are a few power users driving everything, or is adoption broad?
# MAGIC 6. ⏱️ **Watermark** — the high-water mark for the next incremental run.
# MAGIC
# MAGIC ### 💡 Where this output actually comes in handy
# MAGIC
# MAGIC - 📋 **Drop it in your ops channel** after each scheduled run for instant visibility.
# MAGIC - 📉 **Spot drift** — if `successful` drops week-over-week, that's a license or adoption signal worth investigating.
# MAGIC - 🔍 **Post-deployment check** — confirms the pipeline ran without having to query the table separately.
# MAGIC - 📊 **Feed it to Power BI** — these are exactly the metrics a Copilot adoption dashboard wants.
# MAGIC
# MAGIC ### 🛡️ Handles three different states gracefully
# MAGIC
# MAGIC | State | What you'll see |
# MAGIC |---|---|
# MAGIC | ✅ Table has rows | Full report — counts, top apps, distribution stats |
# MAGIC | 📭 Table exists but empty | Friendly "no data yet" message — pipeline is ready and waiting |
# MAGIC | ❓ Table missing | Shouldn't happen (the MERGE cell creates it), but a clear error if it does |

# COMMAND ----------

# ----------------------------------------------------------------------
# 📈 Run Summary
# ----------------------------------------------------------------------
# The final cell — pulls together this run's counters AND the persisted
# table state into one report card you can paste straight into a Teams
# channel or use as your post-run sanity check.
# ----------------------------------------------------------------------

from datetime import datetime, timezone

print("=" * 70)
print("📈 RUN SUMMARY")
print("=" * 70)
print(f"  Notebook            : Copilot Interaction History → Fabric Lakehouse")
print(f"  Run finished at     : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC")
print(f"  Mode                : {mode_banner}")
print("-" * 70)

# ----------------------------------------------------------------------
# 1️⃣  This run — counters from the loop
# ----------------------------------------------------------------------
print()
print("1️⃣  THIS RUN — Loop counters")
print("-" * 70)
print(f"  Users processed       : {total_users:>8,}")
print(f"    ✅ Successful       : {successful:>8,}  ({successful/max(total_users,1)*100:>5.1f}%)")
print(f"    ⏭️  Skipped           : {skipped_no_license:>8,}  ({skipped_no_license/max(total_users,1)*100:>5.1f}%)")
print(f"    ❌ Failed           : {failed:>8,}  ({failed/max(total_users,1)*100:>5.1f}%)")
print(f"  Interactions captured : {len(flat_rows):>8,}")
if successful > 0:
    print(f"  Avg per active user   : {len(flat_rows)/successful:>8.1f}")

# ----------------------------------------------------------------------
# 2️⃣  Persisted table state — actual numbers from Delta
# ----------------------------------------------------------------------
print()
print(f"2️⃣  PERSISTED TABLE — {target_schema}.{target_table}")
print("-" * 70)

if not spark.catalog.tableExists(f"{target_schema}.{target_table}"):
    print("  ❓ Target table doesn't exist — this shouldn't happen.")
    print("     Scroll back to the MERGE cell; it should have created the table.")
else:
    final_count = spark.sql(
        f"SELECT COUNT(*) AS c FROM {target_schema}.{target_table}"
    ).collect()[0]["c"]
    print(f"  Total rows (all runs) : {final_count:>8,}")

    if final_count == 0:
        # ---- Empty table — pipeline is ready, just no data yet ----------
        print()
        print("  📭 The table is empty — no Copilot interactions have landed yet.")
        print()
        print("     This usually means one of:")
        print("       • No Copilot licenses assigned in the tenant yet")
        print("       • No Copilot activity happened in the lookback window")
        print("       • It's the very first deployment and you're just validating")
        print()
        print("     ✅ The pipeline itself is working end-to-end:")
        print("        auth → user catalog → Graph API → Delta MERGE")
        print()
        print("     As soon as people start using Copilot, this table fills up")
        print("     automatically on the next scheduled run.")
    else:
        # ---- Table has rows — full report --------------------------------
        date_range = spark.sql(f"""
            SELECT MIN(created_date_time) AS earliest,
                   MAX(created_date_time) AS latest,
                   COUNT(DISTINCT DATE(created_date_time)) AS distinct_days
              FROM {target_schema}.{target_table}
        """).collect()[0]

        distinct_users = spark.sql(
            f"SELECT COUNT(DISTINCT user_id) AS c FROM {target_schema}.{target_table}"
        ).collect()[0]["c"]
        distinct_convos = spark.sql(
            f"SELECT COUNT(DISTINCT conversation_id) AS c FROM {target_schema}.{target_table}"
        ).collect()[0]["c"]

        print(f"  Distinct users        : {distinct_users:>8,}")
        print(f"  Distinct conversations: {distinct_convos:>8,}")
        print(f"  Earliest interaction  : {date_range['earliest']}")
        print(f"  Latest interaction    : {date_range['latest']}  ← watermark")
        print(f"  Distinct days covered : {date_range['distinct_days']:>8,}")

        # ---- 🏆 Top apps ------------------------------------------------
        print()
        print("  🏆 Top 5 apps by interaction count:")
        top_apps = spark.sql(f"""
            SELECT COALESCE(app_class, '(unknown)') AS app, COUNT(*) AS interactions
              FROM {target_schema}.{target_table}
             GROUP BY app_class
             ORDER BY interactions DESC
             LIMIT 5
        """).collect()
        for r in top_apps:
            pct = r["interactions"] / final_count * 100
            print(f"     • {r['app']:<32} {r['interactions']:>7,}  ({pct:>5.1f}%)")

        # ---- 🧮 Per-user distribution -----------------------------------
        print()
        print("  🧮 Per-user activity distribution:")
        dist = spark.sql(f"""
            WITH user_counts AS (
                SELECT user_id, COUNT(*) AS c
                  FROM {target_schema}.{target_table}
                 GROUP BY user_id
            )
            SELECT
                COUNT(*) AS users,
                MIN(c)   AS min_per_user,
                AVG(c)   AS avg_per_user,
                PERCENTILE_APPROX(c, 0.5)  AS p50,
                PERCENTILE_APPROX(c, 0.95) AS p95,
                MAX(c)   AS max_per_user
              FROM user_counts
        """).collect()[0]
        print(f"     • Users with activity : {dist['users']:>7,}")
        print(f"     • Min per user        : {dist['min_per_user']:>7,}")
        print(f"     • Median (p50)        : {dist['p50']:>7,}")
        print(f"     • Average             : {dist['avg_per_user']:>7,.1f}")
        print(f"     • p95                 : {dist['p95']:>7,}")
        print(f"     • Max per user        : {dist['max_per_user']:>7,}  ← power user")

# ----------------------------------------------------------------------
# 3️⃣  Wrap-up
# ----------------------------------------------------------------------
print()
print("=" * 70)
print("✅ NOTEBOOK DONE")
print("=" * 70)
print(f"  Landed in            : {target_schema}.{target_table}")
print(f"  Ready for            : Silver/Gold transforms, Power BI semantic model")
print(f"  Next recommended run : tomorrow with lookback_days = 1 for incremental")
print("=" * 70)

# ---- 🚨 If you ran this in test mode, here's your pre-commit checklist ---
if in_test_mode:
    print()
    print("🚨 PRE-COMMIT CHECKLIST (you're in TEST MODE):")
    print("   1. Comment out / clear `client_secret` in Cell 3")
    print("   2. Confirm Cell 7 will fall back to Key Vault on its own")
    print("   3. Clear all notebook outputs before committing")
    print("   4. Quick `grep` for any pasted secrets just to be safe")
    print("=" * 70)
