-- Phase 0: users, accounts, and the append-only double-entry ledger.

CREATE TYPE account_kind AS ENUM ('SYSTEM', 'USER');

CREATE TABLE users (
    id BIGINT PRIMARY KEY,              -- Discord user snowflake
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE accounts (
    id BIGSERIAL PRIMARY KEY,
    kind account_kind NOT NULL,
    user_id BIGINT REFERENCES users(id),
    system_name TEXT,                   -- e.g. 'FAUCET'; only set when kind = SYSTEM
    balance BIGINT NOT NULL DEFAULT 0,  -- minor units (cents); materialized cache over ledger_entries
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT accounts_user_xor_system CHECK (
        (kind = 'USER' AND user_id IS NOT NULL AND system_name IS NULL)
        OR (kind = 'SYSTEM' AND user_id IS NULL AND system_name IS NOT NULL)
    ),
    -- Backstop only. The application must never rely on this to reject an
    -- overdraft; it must check balances itself before committing.
    CONSTRAINT accounts_balance_nonneg_for_user CHECK (kind <> 'USER' OR balance >= 0)
);

CREATE UNIQUE INDEX accounts_user_id_key ON accounts (user_id) WHERE kind = 'USER';
CREATE UNIQUE INDEX accounts_system_name_key ON accounts (system_name) WHERE kind = 'SYSTEM';

CREATE TABLE ledger_entries (
    id BIGSERIAL PRIMARY KEY,
    transfer_id UUID NOT NULL,          -- groups the postings of one transfer; SUM(amount) per transfer = 0
    account_id BIGINT NOT NULL REFERENCES accounts (id),
    amount BIGINT NOT NULL,             -- signed minor units; positive = credit, negative = debit
    reason TEXT NOT NULL,
    memo TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX ledger_entries_account_id_idx ON ledger_entries (account_id);
CREATE INDEX ledger_entries_transfer_id_idx ON ledger_entries (transfer_id);

-- Append-only, enforced independently of role grants: no UPDATE or DELETE,
-- ever, for anyone, including the table owner.
CREATE FUNCTION ledger_entries_immutable() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'ledger_entries is append-only: % is not allowed', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_entries_no_update
    BEFORE UPDATE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entries_immutable();

CREATE TRIGGER ledger_entries_no_delete
    BEFORE DELETE ON ledger_entries
    FOR EACH ROW EXECUTE FUNCTION ledger_entries_immutable();

-- In production, the application's DB role should also have UPDATE/DELETE
-- revoked on this table as defense in depth on top of the trigger above:
--   REVOKE UPDATE, DELETE ON ledger_entries FROM stockbot_app;

-- System accounts. FAUCET and SINK are expected to carry negative balances
-- (FAUCET's magnitude is total money printed); the nonneg CHECK above only
-- applies to USER accounts, so this is fine.
INSERT INTO accounts (kind, system_name, balance) VALUES
    ('SYSTEM', 'FAUCET', 0),
    ('SYSTEM', 'SINK', 0),
    ('SYSTEM', 'MARKET_MAKER', 0),
    ('SYSTEM', 'INSURANCE_FUND', 0);
