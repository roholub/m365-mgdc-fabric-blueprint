# Contributing

Thanks for your interest in improving the **M365 MGDC + Fabric Blueprint**! This
repository is a reference implementation for ingesting Microsoft 365 telemetry
(via Microsoft Graph Data Connect and the Microsoft Graph API) into a Microsoft
Fabric lakehouse. Contributions that improve clarity, correctness, and
replicability are welcome.

## Ways to contribute

- **Fix bugs** in the ingestion notebook or pipeline template.
- **Improve documentation** in [`docs/`](docs/) so the blueprint is easier to follow.
- **Report issues** when something is unclear or does not work as described.

## Repository layout

```
.
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── docs/
│   ├── architecture.md       # Hybrid MGDC + Graph API architecture
│   └── DEPLOYMENT.md         # Step-by-step deployment guide
└── notebooks/
    ├── copilot_interaction_history_ingestion.ipynb   # Fabric-ready notebook (import directly)
    └── copilot_interaction_history_ingestion.py      # Explained companion (same logic, commented)
```

## Keeping things consistent

The notebook is the **source of truth** for parameter names. Any documentation or
pipeline change that references a parameter must match the variables defined in
[`notebooks/copilot_interaction_history_ingestion.ipynb`](notebooks/copilot_interaction_history_ingestion.ipynb)
**exactly** (they are lowercase, e.g. `tenant_id`, `client_id`, `key_vault_url`,
`copilot_secret_name`, `lookback_days`, `source_user_table`, `target_schema`,
`target_table`). The `.py` companion shares this contract and adds a few extra
tuning knobs (`page_size`, `max_retries`, `initial_backoff_s`, `user_batch_limit`).

Fabric matches pipeline parameters to notebook parameters by key; a mismatched
name is silently ignored and the notebook falls back to its default, so please
double-check names when editing across files.

## Submitting changes

1. Fork the repository and create a feature branch.
2. Make your change, keeping commits focused and descriptive.
3. Verify notebooks run and JSON/Markdown is valid.
4. Open a pull request describing **what** you changed and **why**.

## Reporting issues

When filing an issue, please include:

- What you expected to happen and what actually happened.
- Relevant logs or error messages (redact tenant IDs, secrets, and user data).
- The file(s) involved and any reproduction steps.

> **Never** commit secrets, client secrets, tenant identifiers tied to a real
> tenant, or exported M365 user data. Store secrets in Azure Key Vault as the
> blueprint describes.
