-- Self-hosted installs without Supabase create auth.users as a stub
-- (20221221050000_create_auth_schema_and_users_table.sql) that has no
-- raw_user_meta_data column. 20261019200000_jawn_set_accepted_terms_date.sql
-- creates a SQL function that updates that column, and Postgres validates the
-- function body at creation, so the migration fails on those installs.
-- Add the column first. On Supabase the column already exists and this is a no-op.
ALTER TABLE auth.users ADD COLUMN IF NOT EXISTS raw_user_meta_data jsonb;
