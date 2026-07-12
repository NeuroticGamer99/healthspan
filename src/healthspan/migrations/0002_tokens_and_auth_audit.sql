-- Migration 0002 — named scoped tokens and the auth audit log (Phase 2 WI-2).
--
-- The control-plane tables behind ADR-0026's bearer-token model: the server
-- side of every credential (hashes only, never plaintext) and the append-only
-- record of every authentication outcome. Runs inside the migration runner's
-- transaction discipline (ADR-0035) like 0001; never guards its own DDL.
--
-- Conventions carried over from 0001: STRICT tables, TEXT ISO-8601
-- timestamps, INTEGER 0/1 booleans, RAISE(ABORT) append-only triggers.
-- auth_audit timestamps are system event times, UTC only — the four-column
-- local-time quadruple is for clinically meaningful times, which these are
-- not.

--------------------------------------------------------------------------
-- tokens (ADR-0026) — one row per issued credential. Stores only
-- SHA-256(token); the plaintext exists at issuance and in the holder's own
-- keyring, never here. `authorship` is stamped onto interpretive rows
-- (ADR-0043); `publish_namespaces` is the events-scope allowlist (ADR-0026);
-- `job_id` binds an ephemeral job token to its job (ADR-0026), NULL for
-- standing tokens.
--------------------------------------------------------------------------

CREATE TABLE tokens (
    id                 INTEGER PRIMARY KEY,
    name               TEXT NOT NULL UNIQUE,
    token_hash         TEXT NOT NULL UNIQUE,    -- SHA-256 hex of the full token string
    scopes             TEXT NOT NULL,           -- space-separated flat scope list (ADR-0026)
    authorship         TEXT NOT NULL DEFAULT 'self',
    publish_namespaces TEXT,                    -- space-separated allowlist; NULL without `events`
    job_id             INTEGER REFERENCES jobs(id),
    created_utc        TEXT NOT NULL,
    last_used_utc      TEXT,
    revoked            INTEGER NOT NULL DEFAULT 0,
    revoked_utc        TEXT,
    CHECK (authorship IN ('self', 'ai')),
    CHECK (revoked IN (0, 1)),
    CHECK (revoked_utc IS NULL OR revoked = 1)
) STRICT;

--------------------------------------------------------------------------
-- auth_audit (ADR-0026) — append-only authentication outcomes: token *name*
-- (or 'invalid' for unrecognized credentials), never token values, never
-- request bodies, never health data. Separate from audit_log deliberately:
-- audit_log records data mutations (ADR-0027); this records control-plane
-- access.
--------------------------------------------------------------------------

CREATE TABLE auth_audit (
    id           INTEGER PRIMARY KEY,
    occurred_utc TEXT NOT NULL,
    token_name   TEXT NOT NULL,                 -- advisory name, or 'invalid'
    source_addr  TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    method       TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    CHECK (outcome IN (
        'ok', 'denied:scope', 'denied:invalid', 'denied:revoked', 'rate-limited'
    ))
) STRICT;

CREATE INDEX ix_auth_audit_time ON auth_audit (occurred_utc);
CREATE INDEX ix_auth_audit_name ON auth_audit (token_name, occurred_utc);

-- Append-only, enforced in the schema (the audit_log pattern, ADR-0026/0027):
-- token issuance and failed attempts are exactly the events whose silent
-- disappearance would make control-plane subversion invisible.
CREATE TRIGGER auth_audit_no_update BEFORE UPDATE ON auth_audit BEGIN
    SELECT RAISE(ABORT, 'auth_audit is append-only (ADR-0026)');
END;
CREATE TRIGGER auth_audit_no_delete BEFORE DELETE ON auth_audit BEGIN
    SELECT RAISE(ABORT, 'auth_audit is append-only (ADR-0026)');
END;
