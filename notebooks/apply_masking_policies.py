# Databricks notebook source

# COMMAND ----------

# MAGIC %md
# MAGIC # Apply Unity Catalog ABAC Masking Policies & Catalog Tags
# MAGIC
# MAGIC Reads `masking_config` Delta table and idempotently applies `CREATE OR REPLACE POLICY`
# MAGIC statements for each active PII column type.
# MAGIC
# MAGIC **Catalog targeting:**
# MAGIC - Pass a single catalog name → applies policies to that catalog only
# MAGIC - Pass a comma-separated list (e.g. `cat_a,cat_b,cat_c`) → applies to each listed catalog
# MAGIC - Pass `ALL` (default) → discovers all non-system catalogs and applies to each
# MAGIC
# MAGIC **Catalog tagging (optional):**
# MAGIC - Pass `tag_key_value_pairs` as a comma-separated string of `key:value` pairs
# MAGIC - Positional mapping: first catalog gets first tag, second catalog gets second tag, etc.
# MAGIC - Example: catalogs=`cat_a,cat_b` + tags=`org:fb_product_solutions,org:sb_gmad`
# MAGIC   → `cat_a` tagged with `org = fb_product_solutions`, `cat_b` tagged with `org = sb_gmad`

# COMMAND ----------

# DBTITLE 1,Parameters
dbutils.widgets.text("catalog", "ALL", "Target Catalog (name, comma-separated list, or ALL)")
dbutils.widgets.text("exclude_catalogs", "system,hive_metastore,__databricks_internal,samples", "Catalogs to exclude (comma-separated)")
dbutils.widgets.text("config_catalog", "dg_metadata_catalog", "Config Catalog")
dbutils.widgets.text("schema", "db_metadata_masking", "Schema")
dbutils.widgets.text("masking_function", "dg_metadata_catalog.db_metadata_masking.masking_function", "Masking UDF")
dbutils.widgets.dropdown("force_reapply", "false", ["true", "false"], "Force re-apply existing policies")
dbutils.widgets.text("tag_key_value_pairs", "", "Catalog Tags (comma-separated key:value pairs, positional with catalogs)")

catalog_param       = dbutils.widgets.get("catalog").strip()
exclude_catalogs    = {c.strip().lower() for c in dbutils.widgets.get("exclude_catalogs").split(",") if c.strip()}
config_catalog      = dbutils.widgets.get("config_catalog")
schema              = dbutils.widgets.get("schema")
masking_function    = dbutils.widgets.get("masking_function")
force_reapply       = dbutils.widgets.get("force_reapply").lower() == "true"
tag_kvp_param       = dbutils.widgets.get("tag_key_value_pairs").strip()

# Derive environment from workspace URL (e.g. nyl-builder-dev.cloud.databricks.com → dev)
workspace_url = spark.conf.get("spark.databricks.workspaceUrl")
env = workspace_url.split(".")[0].split("-")[-1]

print(f"Target Catalog      : {catalog_param}")
print(f"Exclude Catalogs    : {exclude_catalogs}")
print(f"Config Catalog      : {config_catalog}")
print(f"Schema              : {schema}")
print(f"Masking Function    : {masking_function}")
print(f"Force Re-apply      : {force_reapply}")
print(f"Tag Key-Value Pairs : {tag_kvp_param if tag_kvp_param else '(none)'}")
print(f"Environment         : {env} (from workspace URL: {workspace_url})")

# COMMAND ----------

# DBTITLE 1,Resolve target catalogs
def resolve_catalogs(catalog_param):
    """Return list of catalog names to apply policies to.

    Accepts:
      - 'ALL'                       → discover all non-system, non-config catalogs
      - 'cat_a'                     → single catalog
      - 'cat_a,cat_b,cat_c'         → explicit comma-separated list
    """
    if catalog_param.upper() == "ALL":
        all_catalogs_df = spark.sql("SHOW CATALOGS")
        all_catalogs = [row["catalog"] for row in all_catalogs_df.collect()]
        exclusion_set = exclude_catalogs | {config_catalog.lower()}
        target_catalogs = [c for c in all_catalogs if c.lower() not in exclusion_set]
        print(f"Discovered {len(target_catalogs)} catalogs (excluded {len(all_catalogs) - len(target_catalogs)} system/config catalogs)")
        return sorted(target_catalogs)

    # Parse comma-separated list (also handles single-catalog case naturally).
    # De-duplicate while preserving first-seen order.
    requested = []
    seen = set()
    for c in catalog_param.split(","):
        name = c.strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        requested.append(name)

    if not requested:
        raise ValueError(f"No valid catalog names parsed from input: {catalog_param!r}")

    # Validate that the requested catalogs actually exist in the metastore.
    all_catalogs_df = spark.sql("SHOW CATALOGS")
    existing = {row["catalog"].lower() for row in all_catalogs_df.collect()}
    missing = [c for c in requested if c.lower() not in existing]
    if missing:
        raise ValueError(f"Catalog(s) not found in metastore: {missing}")

    # Warn (but don't fail) if user explicitly listed a catalog that's in the exclude set
    # or matches the config catalog — explicit intent wins, but make it visible.
    exclusion_set = exclude_catalogs | {config_catalog.lower()}
    overlap = [c for c in requested if c.lower() in exclusion_set]
    if overlap:
        print(f"WARNING: explicitly requested catalog(s) overlap exclude/config list and will still be processed: {overlap}")

    return requested

target_catalogs = resolve_catalogs(catalog_param)
print(f"Target catalogs  : {target_catalogs}")

# COMMAND ----------

# DBTITLE 1,Load masking config
config_df = spark.table(f"{config_catalog}.{schema}.masking_config").filter("is_active = true")
config_rows = config_df.collect()

print(f"Active masking policies per catalog: {len(config_rows)}")
print(f"Total policies to apply: {len(config_rows)} x {len(target_catalogs)} catalogs = {len(config_rows) * len(target_catalogs)}")
display(config_df)

# COMMAND ----------

# DBTITLE 1,Load existing UC policies for skip check
# ABAC policies are NOT in system.information_schema. Use UC Policies REST API
# (databricks-sdk) per-catalog with parallel fanout. Build set of (catalog_lower, policy_name)
# tuples for O(1) lookup in the apply loop.
from databricks.sdk import WorkspaceClient
from concurrent.futures import ThreadPoolExecutor

_w = WorkspaceClient()

def _list_policies_for_catalog(cat):
    found = set()
    try:
        for p in _w.policies.list_policies(
            on_securable_type="CATALOG",
            on_securable_fullname=cat,
        ):
            if p.name and p.name.startswith("masking_policy_"):
                found.add((cat.lower(), p.name))
    except Exception as e:
        print(f"{cat}: list_policies failed ({e}) — will re-apply")
    return found

existing_policies = set()
with ThreadPoolExecutor(max_workers=16) as ex:
    for result in ex.map(_list_policies_for_catalog, target_catalogs):
        existing_policies |= result

print(f"Found {len(existing_policies)} existing masking policies across {len(target_catalogs)} catalogs")

# COMMAND ----------

# DBTITLE 1,Build policy SQL
def build_policy_sql(col_name, catalog, tag_name, except_group, masking_fn):
    """Generate a CREATE OR REPLACE POLICY statement from config parameters."""
    return f"""CREATE OR REPLACE POLICY `masking_policy_{col_name}`
ON CATALOG {catalog}
COMMENT 'masking_policy_{col_name}'
COLUMN MASK {masking_fn}
TO `account users`
EXCEPT `{except_group}`
FOR TABLES
MATCH COLUMNS has_tag('{tag_name}') AS m
ON COLUMN m"""

# COMMAND ----------

# DBTITLE 1,Apply policies across all target catalogs
results = []

for catalog in target_catalogs:
    print(f"\n{'='*60}")
    print(f"Catalog: {catalog}  |  Environment: {env}")
    print(f"{'='*60}")

    for row in config_rows:
        col_name = row["col_name"]
        except_group = row["access_group_pattern"].replace("{env}", env)
        tag_name = row["class_tag"]
        masking_fn = row["masking_function"] if row["masking_function"] else masking_function

        policy_sql = build_policy_sql(col_name, catalog, tag_name, except_group, masking_fn)

        policy_name = f"masking_policy_{col_name}"
        already_exists = (catalog.lower(), policy_name) in existing_policies

        if already_exists and not force_reapply:
            action = "SKIPPED"
            error_message = None
            print(f"{col_name} (already applied)")
        else:
            try:
                spark.sql(policy_sql)
                action = "APPLIED"
                error_message = None
                print(f"{col_name}")
            except Exception as e:
                action = "FAILED"
                error_message = str(e)[:2000]
                print(f"{col_name}: {error_message}")

        results.append({
            "col_name": col_name,
            "catalog_name": catalog,
            "environment": env,
            "action": action,
            "policy_sql": policy_sql,
            "error_message": error_message,
        })

# COMMAND ----------

# DBTITLE 1,Apply catalog tags (optional)
tag_results = []

if tag_kvp_param:
    tag_pairs = [t.strip() for t in tag_kvp_param.split(",") if t.strip()]

    if catalog_param.upper() == "ALL":
        if len(tag_pairs) != 1:
            raise ValueError(
                f"When catalog=ALL, supply exactly one tag (applied to every catalog). "
                f"Got {len(tag_pairs)} tags."
            )
        catalog_tag_map = [(cat, tag_pairs[0]) for cat in target_catalogs]
    else:
        if len(tag_pairs) != len(target_catalogs):
            raise ValueError(
                f"Positional mismatch: {len(target_catalogs)} catalog(s) but {len(tag_pairs)} tag(s). "
                f"Catalogs: {target_catalogs}, Tags: {tag_pairs}"
            )
        catalog_tag_map = list(zip(target_catalogs, tag_pairs))

    print(f"\n{'='*60}")
    print(f"CATALOG TAGGING")
    print(f"{'='*60}")

    for catalog, kvp in catalog_tag_map:
        if ":" not in kvp:
            error_msg = f"Invalid tag format '{kvp}' — expected 'key:value'"
            print(f"  {catalog}: {error_msg}")
            tag_results.append({
                "col_name": f"TAG:{kvp}",
                "catalog_name": catalog,
                "environment": env,
                "action": "FAILED",
                "policy_sql": None,
                "error_message": error_msg,
            })
            continue

        tag_key, tag_value = kvp.split(":", 1)
        tag_key = tag_key.strip()
        tag_value = tag_value.strip()
        tag_sql = f"ALTER CATALOG `{catalog}` SET TAGS ('{tag_key}' = '{tag_value}')"

        try:
            spark.sql(tag_sql)
            action = "TAG_APPLIED"
            error_message = None
            print(f"  {catalog}: {tag_key} = {tag_value}")
        except Exception as e:
            action = "TAG_FAILED"
            error_message = str(e)[:2000]
            print(f"  {catalog}: FAILED — {error_message}")

        tag_results.append({
            "col_name": f"TAG:{tag_key}:{tag_value}",
            "catalog_name": catalog,
            "environment": env,
            "action": action,
            "policy_sql": tag_sql,
            "error_message": error_message,
        })

    results.extend(tag_results)
else:
    print("\nNo tag_key_value_pairs provided — skipping catalog tagging.")

# COMMAND ----------

# DBTITLE 1,Write audit log
from pyspark.sql.types import StructType, StructField, StringType
from pyspark.sql.functions import current_user, current_timestamp

audit_schema = StructType([
    StructField("col_name", StringType()),
    StructField("catalog_name", StringType()),
    StructField("environment", StringType()),
    StructField("action", StringType()),
    StructField("policy_sql", StringType()),
    StructField("error_message", StringType()),
])

audit_df = (
    spark.createDataFrame(results, schema=audit_schema)
    .withColumn("executed_by", current_user())
    .withColumn("executed_at", current_timestamp())
)

audit_df.write.mode("append").saveAsTable(f"{config_catalog}.{schema}.masking_audit_log")

print(f"\nAudit log written: {len(results)} entries across {len(target_catalogs)} catalogs")
display(audit_df)

# COMMAND ----------

# DBTITLE 1,Summary
failed       = [r for r in results if r["action"] == "FAILED"]
applied      = [r for r in results if r["action"] == "APPLIED"]
skipped      = [r for r in results if r["action"] == "SKIPPED"]
tags_applied = [r for r in results if r["action"] == "TAG_APPLIED"]
tags_failed  = [r for r in results if r["action"] == "TAG_FAILED"]
all_failures = failed + tags_failed

print(f"\n{'='*60}")
print(f"SUMMARY")
print(f"{'='*60}")
print(f"Catalogs targeted : {len(target_catalogs)}")
print(f"Environment       : {env}")
print(f"Policies applied  : {len(applied)}")
print(f"Policies skipped  : {len(skipped)}")
print(f"Policies failed   : {len(failed)}")
print(f"Tags applied      : {len(tags_applied)}")
print(f"Tags failed       : {len(tags_failed)}")

if all_failures:
    print(f"\nFailures:")
    for f_item in all_failures:
        print(f"  - {f_item['catalog_name']}.{f_item['col_name']}: {f_item['error_message'][:100]}")
    msg = f"{len(all_failures)} of {len(results)} operations failed across {len(target_catalogs)} catalogs."
    print(f"\n{msg}")
    # Optionally raise to fail the DABs job and trigger email notification
    # dbutils.notebook.exit(msg)
else:
    msg = f"SUCCESS: {len(applied)} policies applied, {len(tags_applied)} tags applied across {len(target_catalogs)} catalogs"
    print(f"\n{msg}")
    dbutils.notebook.exit(msg)