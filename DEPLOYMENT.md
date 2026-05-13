# Deployment Guide (Windows / PowerShell)

Step-by-step deployment guide for the UC ABAC Masking Policy Manager bundle.

## Pre-requisites (one-time, admin)

```powershell
# 1. Secret scope + salt for SHA-256 masking UDF
databricks secrets create-scope masking

# Generate cryptographically secure 256-bit salt (64 hex chars)
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
$salt = [BitConverter]::ToString($bytes).Replace("-","").ToLower()

databricks secrets put-secret masking pii-salt --string-value $salt

# 2. Service principal (prod only) — grant the following permissions:
#    - USE CATALOG on all target catalogs
#    - CREATE POLICY on all target catalogs
#    - SELECT on system.information_schema.policies
#    - ALL PRIVILEGES on dg_metadata_catalog.db_metadata_masking
```

## Set warehouse_id once per session

The bundle requires a SQL warehouse ID for deploy-time DDL tasks. Set it as an env var so you don't repeat `--var` on every command:

```powershell
$env:BUNDLE_VAR_warehouse_id = "<your-warehouse-id>"
$env:WID = "<your-warehouse-id>"   # also useful for direct sql execute calls
```

Alternatively, pass `--var "warehouse_id=<id>"` on every `databricks bundle ...` command below.

## First Deploy

```powershell
# 3. Validate yml
databricks bundle validate -t dev

# 4. Deploy bundle (uploads files, registers jobs — does not run them)
databricks bundle deploy -t dev

# 5. Run setup once (creates UDF, then config + audit tables in order)
databricks bundle run setup_masking -t dev

# 6. Verify setup
databricks sql execute --warehouse-id $env:WID --query @"
SELECT * FROM dg_metadata_catalog_dev.db_metadata_masking.masking_config;
DESCRIBE FUNCTION dg_metadata_catalog_dev.db_metadata_masking.masking_function;
"@
```

## First Policy Apply (manual)

```powershell
# 7. Test on single catalog first
databricks bundle run masking_policy_manager -t dev --params "catalog=cl_test_dev"

# 8. Check audit log
databricks sql execute --warehouse-id $env:WID --query @"
SELECT action, COUNT(*) FROM dg_metadata_catalog_dev.db_metadata_masking.masking_audit_log
WHERE executed_at > current_timestamp() - INTERVAL 1 HOUR
GROUP BY action;
"@

# 9. Run on ALL catalogs
databricks bundle run masking_policy_manager -t dev
```

## Activate Schedule

```powershell
# 10. Edit databricks.yml — change pause_status: PAUSED to UNPAUSED
# 11. Redeploy
databricks bundle deploy -t dev
```

## Promote to QA / Prod

```powershell
# Reset env var if QA/prod uses a different warehouse
$env:BUNDLE_VAR_warehouse_id = "<qa-warehouse-id>"

databricks bundle deploy -t qa
databricks bundle run setup_masking -t qa

$env:BUNDLE_VAR_warehouse_id = "<prod-warehouse-id>"
databricks bundle deploy -t prod
databricks bundle run setup_masking -t prod
```

## Force Re-apply (admin override)

```powershell
# When config changes (e.g. access_group renamed) and policies need refresh
databricks bundle run masking_policy_manager -t dev 
```

## Notes for Windows

- Use `pwsh` (PowerShell 7+) for best cross-platform consistency
- Path separators: `\` in PS, but databricks CLI accepts both `\` and `/`
- Heredoc syntax: `@"..."@` instead of bash `EOF`
- Env vars: `$env:VAR_NAME`
- Execution policy may block scripts — run once: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`
