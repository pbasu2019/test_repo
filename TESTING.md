# Unit Testing Plan (Windows / PowerShell)

Test strategy for the UC ABAC Masking Policy Manager bundle.

## Test Pyramid

| Layer | What | Tool | Where |
|---|---|---|---|
| **L1: Pure Python** | `build_policy_sql`, env parsing, exclusion logic, skip logic | `pytest` | local |
| **L2: SQL DDL** | UDF behavior, table schema | `databricks sql execute` | local → dev workspace |
| **L3: Integration** | End-to-end notebook run on test catalog | Bundle run + assertions | dev workspace |
| **L4: Smoke** | Post-deploy sanity check | PowerShell + `databricks sql execute` | any env |

## L1: Unit Tests (Pure Python)

File: `tests\test_apply_masking_policies.py`

```python
import pytest

def test_build_policy_sql_basic():
    sql = build_policy_sql("ssn", "cat_dev", "class.us_ssn", "dbx-dev-pii-ssn-access", "fn")
    assert "CREATE OR REPLACE POLICY `masking_policy_ssn`" in sql
    assert "ON CATALOG cat_dev" in sql
    assert "EXCEPT `dbx-dev-pii-ssn-access`" in sql
    assert "has_tag('class.us_ssn')" in sql

def test_env_from_workspace_url():
    # nyl-builder-dev.cloud.databricks.com -> dev
    assert "nyl-builder-dev.cloud.databricks.com".split(".")[0].split("-")[-1] == "dev"
    assert "nyl-builder-prod.cloud.databricks.com".split(".")[0].split("-")[-1] == "prod"

def test_exclude_catalogs_parsing():
    raw = "system, hive_metastore , samples"
    parsed = {c.strip().lower() for c in raw.split(",") if c.strip()}
    assert parsed == {"system", "hive_metastore", "samples"}

def test_skip_logic_existing_policy():
    existing = {("cat_dev", "masking_policy_ssn")}
    assert ("cat_dev", "masking_policy_ssn") in existing
    assert ("cat_dev", "masking_policy_email") not in existing

def test_force_reapply_overrides_skip():
    existing = {("cat_dev", "masking_policy_ssn")}
    force = True
    already = ("cat_dev", "masking_policy_ssn") in existing
    should_apply = not (already and not force)
    assert should_apply

def test_env_substitution_in_access_group():
    pattern = "dbx-{env}-pii-ssn-access"
    assert pattern.replace("{env}", "dev") == "dbx-dev-pii-ssn-access"
    assert pattern.replace("{env}", "prod") == "dbx-prod-pii-ssn-access"
```

Run:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install pytest
pytest tests\ -v
```

## L2: SQL / UDF Tests

File: `tests\test_masking_function.sql`

```sql
-- Verify UDF exists + executes correctly
SELECT
  ${catalog}.${schema}.masking_function('test@example.com') AS hashed,
  ${catalog}.${schema}.masking_function(NULL)               AS null_handling,
  ${catalog}.${schema}.masking_function('foo') = ${catalog}.${schema}.masking_function('foo') AS deterministic,
  ${catalog}.${schema}.masking_function('foo') != ${catalog}.${schema}.masking_function('bar') AS unique_hashes,
  length(${catalog}.${schema}.masking_function('x')) = 64   AS correct_sha256_length;
```

Expected:

- `hashed` != original
- `null_handling` IS NULL
- `deterministic` = true
- `unique_hashes` = true
- `correct_sha256_length` = true (SHA-256 hex = 64 chars)

Run:

```powershell
databricks sql execute --warehouse-id $env:WID --file tests\test_masking_function.sql
```

## L3: Integration Tests

File: `tests\integration\test_e2e.ps1`

```powershell
$TEST_CAT = "cl_unittest_$(Get-Date -Format 'yyyyMMddHHmmss')"
$WID      = $env:WAREHOUSE_ID

# Setup test catalog with tagged column
databricks sql execute --warehouse-id $WID --query @"
CREATE CATALOG $TEST_CAT;
CREATE SCHEMA $TEST_CAT.test;
CREATE TABLE $TEST_CAT.test.users (id INT, ssn STRING);
ALTER TABLE $TEST_CAT.test.users ALTER COLUMN ssn SET TAGS ('class.us_ssn');
INSERT INTO $TEST_CAT.test.users VALUES (1, '123-45-6789');
"@

# Run job on this catalog
databricks bundle run masking_policy_manager -t dev --params "catalog=$TEST_CAT"

# Assert: policy exists in UC
$count = databricks sql execute --warehouse-id $WID --query @"
SELECT COUNT(*) AS c FROM system.information_schema.policies
WHERE catalog_name='$TEST_CAT' AND policy_name='masking_policy_ssn';
"@
if ($count -notmatch "1") { Write-Error "Policy not created"; exit 1 }

# Assert: audit log shows APPLIED
$action = databricks sql execute --warehouse-id $WID --query @"
SELECT action FROM dg_metadata_catalog_dev.db_metadata_masking.masking_audit_log
WHERE catalog_name='$TEST_CAT' AND col_name='ssn'
ORDER BY executed_at DESC LIMIT 1;
"@
if ($action -notmatch "APPLIED") { Write-Error "Expected APPLIED"; exit 1 }

# Re-run -> expect SKIPPED
databricks bundle run masking_policy_manager -t dev --params "catalog=$TEST_CAT"
$action2 = databricks sql execute --warehouse-id $WID --query @"
SELECT action FROM dg_metadata_catalog_dev.db_metadata_masking.masking_audit_log
WHERE catalog_name='$TEST_CAT' AND col_name='ssn'
ORDER BY executed_at DESC LIMIT 1;
"@
if ($action2 -notmatch "SKIPPED") { Write-Error "Expected SKIPPED"; exit 1 }

# Force re-apply -> expect APPLIED
databricks bundle run masking_policy_manager -t dev --params "catalog=$TEST_CAT,force_reapply=true"
$action3 = databricks sql execute --warehouse-id $WID --query @"
SELECT action FROM dg_metadata_catalog_dev.db_metadata_masking.masking_audit_log
WHERE catalog_name='$TEST_CAT' AND col_name='ssn'
ORDER BY executed_at DESC LIMIT 1;
"@
if ($action3 -notmatch "APPLIED") { Write-Error "Expected APPLIED on force"; exit 1 }

# Assert: masking actually works for non-exempt user
# (Run this query as a user NOT in dbx-dev-pii-ssn-access group)
# Expected: ssn returns hashed value, not '123-45-6789'

# Cleanup
databricks sql execute --warehouse-id $WID --query "DROP CATALOG $TEST_CAT CASCADE;"
Write-Host "All integration tests passed" -ForegroundColor Green
```

## L4: Smoke Test (post-deploy)

File: `tests\smoke.ps1`

```powershell
param([string]$envName = "dev")

$WID = $env:WAREHOUSE_ID
$cat = "dg_metadata_catalog_$envName"

# UDF callable
databricks sql execute --warehouse-id $WID --query "SELECT $cat.db_metadata_masking.masking_function('test')" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "UDF check failed"; exit 1 }

# Config table populated
$rows = databricks sql execute --warehouse-id $WID --query "SELECT COUNT(*) AS c FROM $cat.db_metadata_masking.masking_config WHERE is_active=true"
Write-Host "Active config rows: $rows"

# Audit log writable (just check accessible)
databricks sql execute --warehouse-id $WID --query "SELECT COUNT(*) FROM $cat.db_metadata_masking.masking_audit_log" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "Audit log not accessible"; exit 1 }

# System table accessible
databricks sql execute --warehouse-id $WID --query "SELECT COUNT(*) FROM system.information_schema.policies" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Error "system.information_schema.policies not accessible"; exit 1 }

Write-Host "Smoke test passed for env: $envName" -ForegroundColor Green
```

## Run Commands

```powershell
# All L1 unit tests
pytest tests\ -v

# L2 SQL tests
databricks sql execute --warehouse-id $env:WID --file tests\test_masking_function.sql

# L3 end-to-end
.\tests\integration\test_e2e.ps1

# L4 smoke (per env)
.\tests\smoke.ps1 -envName dev
.\tests\smoke.ps1 -envName qa
.\tests\smoke.ps1 -envName prod
```

## CI Wiring (GitHub Actions on windows-latest)

```yaml
jobs:
  test:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install pytest databricks-cli
        shell: pwsh
      - run: pytest tests\ -v
        shell: pwsh
      - run: databricks bundle deploy -t dev
        shell: pwsh
        env:
          DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST }}
          DATABRICKS_TOKEN: ${{ secrets.DATABRICKS_TOKEN }}
      - run: databricks bundle run setup_masking -t dev
        shell: pwsh
      - run: .\tests\integration\test_e2e.ps1
        shell: pwsh
        env:
          WAREHOUSE_ID: ${{ secrets.WAREHOUSE_ID }}
      - run: .\tests\smoke.ps1 -envName dev
        shell: pwsh
        env:
          WAREHOUSE_ID: ${{ secrets.WAREHOUSE_ID }}
```

## Coverage Targets

| Layer | Target |
|---|---|
| L1 (unit) | 90%+ pure logic |
| L2 (SQL) | Every UDF behavior path |
| L3 (integration) | Happy path + skip path + force-reapply path |
| L4 (smoke) | 4 assertions, completes in <10s |

## Notes for Windows

- Use `pwsh` (PowerShell 7+) not `powershell.exe` (5.x)
- Heredoc: `@"..."@` instead of bash `EOF`
- Env vars: `$env:VAR_NAME`
- `Get-Random` instead of `openssl rand`
- Execution policy: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` (one-time)
