#!/usr/bin/env python3
"""
Trigger the UC Masking Policy Manager job via Databricks Jobs API.

Prerequisites:
  pip install databricks-sdk

Authentication (any one):
  - DATABRICKS_HOST + DATABRICKS_TOKEN env vars
  - ~/.databrickscfg profile
  - Azure/GCP managed identity (when running inside cloud)

Usage:
  # Masking only (no tags)
  python run_masking_job.py --job-name "[dev] UC Masking Policy Manager"

  # Single catalog + single tag
  python run_masking_job.py \
    --job-name "[dev] UC Masking Policy Manager" \
    --catalog dev_builder_governance \
    --tags "org:fb_product_solutions"

  # Multiple catalogs + positional tags
  python run_masking_job.py \
    --job-name "[dev] UC Masking Policy Manager" \
    --catalog "cat_a,cat_b" \
    --tags "org:fb_product_solutions,org:sb_gmad"

  # All catalogs + broadcast one tag
  python run_masking_job.py \
    --job-name "[dev] UC Masking Policy Manager" \
    --catalog ALL \
    --tags "class_sensitivity:internal"

  # Force re-apply policies
  python run_masking_job.py \
    --job-name "[dev] UC Masking Policy Manager" \
    --force-reapply
"""

import argparse
import sys
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import RunNow


def find_job_by_name(client: WorkspaceClient, job_name: str) -> int:
    for job in client.jobs.list(name=job_name):
        if job.settings and job.settings.name == job_name:
            return job.job_id
    print(f"ERROR: Job '{job_name}' not found.", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Trigger the UC Masking Policy Manager job")
    parser.add_argument("--job-name", required=True, help="Exact job name (e.g. '[dev] UC Masking Policy Manager')")
    parser.add_argument("--catalog", default=None, help="Target catalog(s): name, comma-separated list, or ALL")
    parser.add_argument("--tags", default=None, help="Comma-separated tag key:value pairs (positional with catalogs)")
    parser.add_argument("--exclude-catalogs", default=None, help="Catalogs to exclude when catalog=ALL")
    parser.add_argument("--config-catalog", default=None, help="Config catalog override")
    parser.add_argument("--schema", default=None, help="Schema override")
    parser.add_argument("--masking-function", default=None, help="Masking UDF path override")
    parser.add_argument("--force-reapply", action="store_true", help="Force re-apply existing policies")
    parser.add_argument("--wait", action="store_true", help="Wait for job to complete and print result")
    parser.add_argument("--profile", default=None, help="Databricks CLI profile name")
    args = parser.parse_args()

    client = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    job_id = find_job_by_name(client, args.job_name)
    print(f"Found job: {args.job_name} (id={job_id})")

    notebook_params = {}
    if args.catalog:
        notebook_params["catalog"] = args.catalog
    if args.tags:
        notebook_params["tag_key_value_pairs"] = args.tags
    if args.exclude_catalogs:
        notebook_params["exclude_catalogs"] = args.exclude_catalogs
    if args.config_catalog:
        notebook_params["config_catalog"] = args.config_catalog
    if args.schema:
        notebook_params["schema"] = args.schema
    if args.masking_function:
        notebook_params["masking_function"] = args.masking_function
    if args.force_reapply:
        notebook_params["force_reapply"] = "true"

    print(f"Parameters: {notebook_params if notebook_params else '(defaults)'}")

    run = client.jobs.run_now(
        job_id=job_id,
        notebook_params=notebook_params,
    )
    print(f"Run started: run_id={run.run_id}")
    print(f"URL: {client.config.host}#job/{job_id}/run/{run.run_id}")

    if args.wait:
        print("Waiting for completion...", flush=True)
        result = run.result()
        state = result.state
        print(f"\nResult: {state.result_state.value if state.result_state else state.life_cycle_state.value}")
        if state.state_message:
            print(f"Message: {state.state_message}")
        if state.result_state and state.result_state.value != "SUCCESS":
            sys.exit(1)


if __name__ == "__main__":
    main()
