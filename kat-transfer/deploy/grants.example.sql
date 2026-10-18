-- Apply as the archive owner after creating the API login securely.
-- Change this example role name to the actual server-side API role.
-- This file grants permissions; it creates no role, database or table.
\set api_role kat_api
\set issuer_role kat_token_issuer
GRANT USAGE ON SCHEMA public TO :"api_role";
GRANT SELECT, INSERT, UPDATE ON experiment, experimental_data, environment TO :"api_role";
GRANT SELECT, INSERT ON transfer_log TO :"api_role";
GRANT USAGE, SELECT ON SEQUENCE transfer_log_id_seq TO :"api_role";
GRANT SELECT ON api_user TO :"api_role";
GRANT SELECT ON api_token TO :"api_role";
GRANT UPDATE (last_used_at) ON api_token TO :"api_role";

-- Use this separate owner-managed login only for the token/user CLI.
GRANT USAGE ON SCHEMA public TO :"issuer_role";
GRANT SELECT ON api_user TO :"issuer_role";
GRANT INSERT, SELECT ON api_token TO :"issuer_role";
GRANT UPDATE (revoked_at) ON api_token TO :"issuer_role";
GRANT USAGE, SELECT ON SEQUENCE api_token_id_seq TO :"issuer_role";
GRANT INSERT ON api_user TO :"issuer_role";
GRANT UPDATE (max_role, granted_by, active, disabled_at) ON api_user TO :"issuer_role";
