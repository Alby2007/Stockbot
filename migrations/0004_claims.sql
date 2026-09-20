-- Daily faucet claims, keyed globally by user id (never per-guild) with a
-- streak multiplier, per the design doc's anti-alt-farming section.

CREATE TABLE claims (
    user_id BIGINT PRIMARY KEY REFERENCES users (id),
    last_claim_date DATE NOT NULL,
    streak INT NOT NULL DEFAULT 1 CHECK (streak > 0)
);
