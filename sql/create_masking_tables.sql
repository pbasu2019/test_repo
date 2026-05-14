-- =============================================================================
-- Unity Catalog ABAC Masking – Config & Audit Tables
-- =============================================================================
-- Idempotent DDL: safe to run repeatedly via DABs SQL task.
-- Params (from DABs sql_task): :catalog, :schema
-- =============================================================================

USE CATALOG IDENTIFIER(:catalog);

CREATE SCHEMA IF NOT EXISTS IDENTIFIER(:schema);

USE SCHEMA  IDENTIFIER(:schema);

-- ---------------------------------------------------------------------------
-- 1. masking_config
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS masking_config
(
    col_name                STRING      NOT NULL    COMMENT 'Logical PII column name (e.g. ssn, email_address)',
    class_tag               STRING      NOT NULL    COMMENT 'Unity Catalog classification tag (e.g. class.us_ssn)',
    access_group_pattern    STRING      NOT NULL    COMMENT 'Access group with {env} placeholder (e.g. dbx-{env}-pii-ssn-access)',
    masking_function        STRING      NOT NULL    
                                                    COMMENT 'Fully-qualified UDF used in COLUMN MASK clause',
    is_active               BOOLEAN     NOT NULL    DEFAULT TRUE
                                                    COMMENT 'Toggle to disable a policy without deleting the row',
    created_at              TIMESTAMP   NOT NULL    DEFAULT current_timestamp()
                                                    COMMENT 'Row creation timestamp',
    updated_at              TIMESTAMP   NOT NULL    DEFAULT current_timestamp()
                                                    COMMENT 'Last modification timestamp',
    updated_by              STRING                  COMMENT 'Principal that last modified this row'
)
USING DELTA
COMMENT 'Declarative config for Unity Catalog ABAC column-masking policies. Each row maps a PII column type to its classification tag and exempt access group.'
TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.minReaderVersion'     = '1',
    'delta.minWriterVersion'     = '4',
    'delta.feature.allowColumnDefaults' = 'supported'
);

-- ---------------------------------------------------------------------------
-- 2. Seed initial config rows (MERGE to avoid duplicates on re-run)
-- ---------------------------------------------------------------------------
WITH src AS (
    SELECT * FROM VALUES
    ('NAME','class.name','dbx-{env}-pii-name-access'),
    ('EMAIL_ADDRESS','class.email_address','dbx-{env}-pii-email-address-access'),
    ('LOCATION_ADDRESS','class.location','dbx-{env}-pii-location-access'),
    ('PHONE_NUMBER','class.phone_number','dbx-{env}-pii-phone-number-access'),
    ('SSN','class.us_ssn','dbx-{env}-pii-ssn-access'),
    ('ITIN','class.us_itin','dbx-{env}-pii-ssn-access'),
    ('DRIVER_LICENSE','class.us_driver_license','dbx-{env}-pii-govid-access'),
    ('PASSPORT','class.us_passport','dbx-{env}-pii-govid-access'),
    ('IMMIGRATION_OR_WORK_AUTH_IDENTIFIER','class__govid','dbx-{env}-pii-govid-access'),
    ('MED_CODE','class__medcode','dbx-{env}-pii-med-code-access'),
    ('HEALTH_CODE','class__hlthcode','dbx-{env}-pii-hlth-code-access'),
    ('CREDIT_CARD','class.credit_card','dbx-{env}-pii-ccn-access'),
    ('BANK_ACC_NUMBER','class.us_bank_number','dbx-{env}-pii-bank-number-access'),
    ('IBAN','class.iban_code','dbx-{env}-pii-bank-number-access'),
    ('ROUTING_NUMBER','class__rout_num','dbx-{env}-pii-rout-number-access'),
    ('MICR_CODE','class__micrcode','dbx-{env}-pii-micr-code-access'),
    ('STATISTICAL_ID','class__statid','dbx-{env}-pii-statid-access'),
    ('MEDICAL_ID','class__medid','dbx-{env}-pii-medid-access'),
    ('GENETIC_OR_NEURAL','class__genetic_neural','dbx-{env}-pii-genetic-neural-access'),
    ('BIOMETRIC_ID','class__biometric_id','dbx-{env}-pii-biometric-id-access'),
    ('SEXUAL_ORIENTATION','class__sex_orientation','dbx-{env}-pii-sex-orientation-access'),
    ('RACE_OR_ETHNICITY_OR_PHILOSOPHICAL_BELIEFS','class__race_ethnic_phil','dbx-{env}-pii-race-ethnic-phil-access'),
    ('CONVICTION','class__conviction','dbx-{env}-pii-conviction-access'),
    ('FREE_TEXT','class__free_text','dbx-{env}-pii-free-text-access'),
    ('PASSWORD','class__password','dbx-{env}-pii-password-access')


    AS t (col_name, class_tag, access_group_pattern)
)
MERGE INTO masking_config AS tgt
USING src
ON tgt.col_name = src.col_name
WHEN NOT MATCHED THEN
    INSERT (col_name, class_tag, access_group_pattern,masking_function)
    VALUES (src.col_name, src.class_tag, src.access_group_pattern,concat(:catalog,'.',:schema,'.masking_function'));

-- ---------------------------------------------------------------------------
-- 3. masking_audit_log
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS masking_audit_log
(
    col_name            STRING      NOT NULL    COMMENT 'PII column name from masking_config',
    catalog_name        STRING      NOT NULL    COMMENT 'Catalog the policy was applied to',
    environment         STRING      NOT NULL    COMMENT 'Environment (dev / qa / prod)',
    action              STRING      NOT NULL    COMMENT 'APPLIED | DROPPED | FAILED | SKIPPED',
    policy_sql          STRING                  COMMENT 'Full DDL statement executed (for reproducibility)',
    error_message       STRING                  COMMENT 'Error details if action = FAILED',
    executed_by         STRING      NOT NULL    DEFAULT current_user()
                                                COMMENT 'Principal that ran the job',
    executed_at         TIMESTAMP   NOT NULL    DEFAULT current_timestamp()
                                                COMMENT 'UTC timestamp of execution'
)
USING DELTA
PARTITIONED BY (environment)
COMMENT 'Append-only audit log for masking policy deployments. Partitioned by environment for efficient governance queries.'
TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.appendOnly'           = 'true',
    'delta.logRetentionDuration' = '365 days',
    'delta.feature.allowColumnDefaults' = 'supported'
);
