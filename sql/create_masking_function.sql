-- =============================================================================
-- Unity Catalog ABAC Masking – Masking Function
-- =============================================================================
-- Creates SHA-256 salted masking UDF used in COLUMN MASK policies.
-- Params (from DABs sql_task): :catalog, :schema
-- =============================================================================

USE CATALOG IDENTIFIER(:catalog);

CREATE SCHEMA IF NOT EXISTS IDENTIFIER(:schema);

USE SCHEMA  IDENTIFIER(:schema);

-- ---------------------------------------------------------------------------
-- Masking function
-- ---------------------------------------------------------------------------
-- =============================================================================
-- Universal PII masking UDF (VARIANT-input, VARIANT-output)
-- Designed for ABAC column-mask policies in Unity Catalog.
-- =============================================================================
-- SUPPORTED COLUMN TYPES:
--   STRING                       — full SHA2-256 hex digest (64 chars)
--   BIGINT, INT                  — deterministic 7-hex pseudonym, fits INT range
--   DOUBLE, FLOAT                — same as BIGINT/INT; decimal portion not preserved
--   DECIMAL(p,s) where p-s >= 9  — same as BIGINT/INT; 7-hex-digit integer pseudonym
--   DECIMAL(p,s) where p-s < 9   — fractional pseudonym at DECIMAL(38,38) max precision, always < 1.0
--   DATE, TIMESTAMP, TIMESTAMP_NTZ — deterministic offset within ~100 years
--   BOOLEAN                      — collapsed to FALSE
--
-- OUT OF SCOPE (do not tag columns of these types with the masking tag):
--   SMALLINT, TINYINT            — masked output exceeds declared range, cast fails
--   DECIMAL(p,s) where p < 9 and p-s >= 9 — edge case, unlikely in practice
--
-- DESIGN NOTES:
--   - schema_of_variant() collapses INT/SMALLINT/TINYINT to 'BIGINT' inside VARIANT.
--     The single 'BIGINT' branch handles all integer inputs; output sized for INT.
--   - 7-hex-digit numeric output (max 268,435,455) fits INT range cleanly,
--     also fits BIGINT and wide DECIMAL with reduced entropy.
--   - For BIGINT join keys above ~10K cardinality, collision risk is non-trivial.
--     Consider STRING-output masking or a typed BIGINT wrapper for those cases.
-- =============================================================================

CREATE OR REPLACE FUNCTION masking_function(
  value VARIANT
)
RETURNS VARIANT
LANGUAGE SQL
COMMENT 'SHA-256 hash of salt + PII value. Returns NULL for NULL input. Salt sourced from Databricks Secret masking/pii-salt.'
DETERMINISTIC
RETURN 
CASE
  -- NULL pass-through
  WHEN value IS NULL THEN value

  -- Empty string pass-through (only meaningful for STRING variants)
  WHEN SCHEMA_OF_VARIANT(value) = 'STRING' AND CAST(value AS STRING) = '' THEN value

  -- DECIMAL(p,s) where the integer portion (p - s) is too narrow for the 7-hex-digit
  -- masked integer (max 268,435,455 = 9 digits). Produce a fractional pseudonym instead.
  -- Uses max precision DECIMAL(38,38): two 15-hex-char hash chunks → 19 decimal digits each
  -- → 38-digit fractional value always < 1.0. UC engine truncates to column's actual type.
  -- Covers DECIMAL(18,18), DECIMAL(38,38), DECIMAL(25,20), and all high-scale variants.
  WHEN SCHEMA_OF_VARIANT(value) LIKE 'DECIMAL%'
       AND (
         CAST(REGEXP_EXTRACT(SCHEMA_OF_VARIANT(value), 'DECIMAL\\((\\d+),(\\d+)\\)', 1) AS INT)
         - CAST(REGEXP_EXTRACT(SCHEMA_OF_VARIANT(value), 'DECIMAL\\((\\d+),(\\d+)\\)', 2) AS INT)
       ) < 9 THEN
    CAST(
      CAST(
        CONCAT('0.',
          LPAD(CONV(SUBSTRING(SHA2(CONCAT(CAST(value AS STRING), secret('masking','pii-salt')), 256),  1, 15), 16, 10), 19, '0'),
          LPAD(CONV(SUBSTRING(SHA2(CONCAT(CAST(value AS STRING), secret('masking','pii-salt')), 256), 16, 15), 16, 10), 19, '0')
        )
      AS DECIMAL(38,38))
    AS VARIANT)

  -- All integer types collapse to 'BIGINT' inside VARIANT (per Databricks behavior).
  -- Sized to fit INT range (7 hex digits → max 268,435,455).
  -- This branch handles INT and BIGINT; SMALLINT/TINYINT are out of scope.
  WHEN SCHEMA_OF_VARIANT(value) = 'BIGINT'
       OR SCHEMA_OF_VARIANT(value) IN ('DOUBLE', 'FLOAT')
       OR SCHEMA_OF_VARIANT(value) LIKE 'DECIMAL%' THEN
    CAST(
      ABS(CAST(
        CONV(SUBSTRING(SHA2(CONCAT(CAST(value AS STRING), secret('masking','pii-salt')), 256), 1, 7), 16, 10)
      AS INT))
    AS VARIANT)

  -- STRING: full SHA2-256 hex digest
  WHEN SCHEMA_OF_VARIANT(value) = 'STRING' THEN
    CAST(SHA2(CONCAT(CAST(value AS STRING), secret('masking','pii-salt')), 256) AS VARIANT)

  -- DATE / TIMESTAMP / TIMESTAMP_NTZ: deterministic offset within ~100 years from 1900-01-01
  WHEN SCHEMA_OF_VARIANT(value) IN ('DATE', 'TIMESTAMP', 'TIMESTAMP_NTZ') THEN
    CAST(
      DATE_ADD(DATE'1900-01-01',
        CAST(PMOD(
          CONV(SUBSTRING(SHA2(CONCAT(CAST(value AS STRING), secret('masking','pii-salt')), 256), 1, 8), 16, 10),
          36500
        ) AS INT)
      )
    AS VARIANT)

  -- BOOLEAN: collapse to FALSE
  WHEN SCHEMA_OF_VARIANT(value) = 'BOOLEAN' THEN
    CAST(FALSE AS VARIANT)

  -- Unrecognized type — returns NULL rather than failing
  ELSE CAST(NULL AS VARIANT)
END;

-- ---------------------------------------------------------------------------
-- Grant EXECUTE to account users so UC policy engine can invoke per-row
-- ---------------------------------------------------------------------------
GRANT EXECUTE ON FUNCTION masking_function TO `account users`;
