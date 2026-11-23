-- fast lookup of recent failed logins per client address (login throttling)
CREATE INDEX IF NOT EXISTS ix_audit_ip_time ON audit_logs (ip_address, occurred_at DESC) WHERE ip_address IS NOT NULL;
