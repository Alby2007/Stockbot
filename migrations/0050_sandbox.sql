-- 0050: Sandbox account -- a private, resettable practice season.
-- seasons.sandbox_user_id marks a season as one user's personal sandbox:
-- it never appears in league joins/standings, mints no trophies, and its
-- entries stay out of every "active season" lookup. The league account
-- quarantine (kind='LEAGUE' + season_id scoping) is reused as-is.

ALTER TABLE seasons ADD COLUMN sandbox_user_id BIGINT REFERENCES users (id);

-- One open sandbox per user, resolved by owner without scanning league rows.
CREATE INDEX seasons_sandbox_user_active
    ON seasons (sandbox_user_id) WHERE status = 'ACTIVE';

INSERT INTO shop_items (key, name, description, kind, price_minor, metadata)
VALUES (
    'sandbox_access',
    'Sandbox account',
    'A private practice portfolio with a resettable $100 stake. Wins and losses never touch your real net worth.',
    'PERK', 3000, '{}'
);

INSERT INTO config (key, value) VALUES ('sandbox.stake_minor', 10000);  -- $100.00
