#!/usr/bin/env bash
# Remove schema changes made after the baseline (extra indexes, applied migrations)
# from a stack's database. Data is kept.
#   PROJECT=driftwood-sandbox ENV_FILE=sandbox.env ops/reset_db.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PROJECT="${PROJECT:-driftwood}"
ENV_FILE="${ENV_FILE:-.env}"

docker compose -p "$PROJECT" --env-file "$ENV_FILE" exec -T postgres \
  psql -q -v ON_ERROR_STOP=1 -U driftwood -d driftwood <<'SQL'
DO $$
DECLARE idx record;
BEGIN
  FOR idx IN
    SELECT i.relname AS name
    FROM pg_index x
    JOIN pg_class i ON i.oid = x.indexrelid
    JOIN pg_class t ON t.oid = x.indrelid
    JOIN pg_namespace n ON n.oid = t.relnamespace
    WHERE n.nspname = 'public'
      AND NOT x.indisprimary AND NOT x.indisunique
      AND i.relname NOT IN ('idx_orders_created_at', 'idx_order_items_order_id')
  LOOP
    EXECUTE format('DROP INDEX IF EXISTS %I', idx.name);
    RAISE NOTICE 'dropped index %', idx.name;
  END LOOP;
END $$;
DELETE FROM schema_migrations WHERE name <> '001_baseline.sql';
SELECT pg_stat_statements_reset();
SQL
echo "database schema reset to baseline (${PROJECT})"
