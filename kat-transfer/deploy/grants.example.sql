-- Apply as the archive owner after creating the API login securely.
-- Change this example role name to the actual server-side API role.
-- This file grants permissions; it creates no role, database or table.
\set api_role kat_api
GRANT USAGE ON SCHEMA public TO :"api_role";
GRANT SELECT, INSERT, UPDATE ON experiment, experimental_data, environment TO :"api_role";
GRANT SELECT, INSERT ON transfer_log TO :"api_role";
GRANT USAGE, SELECT ON SEQUENCE transfer_log_id_seq TO :"api_role";
