-- Run in pgAdmin 4's Query Tool connected to experimentaldata on Skywards.
-- These queries inspect identity and schema only; they change no data.

-- 1. Confirm the selected database and login.
SELECT current_database() AS database_name,
       current_user AS database_user,
       inet_server_addr() AS server_address,
       inet_server_port() AS server_port;

-- 2. Check the expected archive tables. A NULL means that table was not found
-- in public; inspect the next query if the archive uses another schema.
SELECT to_regclass('public.experiment') AS experiment,
       to_regclass('public.experimental_data') AS experimental_data,
       to_regclass('public.environment') AS environment,
       to_regclass('public.transfer_log') AS transfer_log;

-- 3. Inspect columns and types in user schemas.
SELECT table_schema, table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
  AND table_name IN ('experiment', 'experimental_data', 'environment', 'transfer_log')
ORDER BY table_schema, table_name, ordinal_position;

-- 4. Confirm primary and foreign keys without touching the records.
SELECT ns.nspname AS table_schema, cls.relname AS table_name,
       con.conname AS constraint_name, pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class cls ON cls.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = cls.relnamespace
WHERE ns.nspname NOT IN ('pg_catalog', 'information_schema')
  AND cls.relname IN ('experiment', 'experimental_data', 'environment')
  AND con.contype IN ('p', 'f')
ORDER BY table_schema, table_name, constraint_name;
