-- Shop: portfolio slots (escalating price, computed in code, not stored),
-- analyst tools and cosmetics (fixed price, tracked as entitlements).

CREATE TABLE shop_items (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('SLOT', 'ANALYST_TOOL', 'COSMETIC')),
    price_minor BIGINT,          -- NULL for SLOT: its price is escalating, computed at purchase time
    duration_days INT,           -- NULL = permanent; otherwise an entitlement that expires and can be renewed
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE entitlements (
    user_id BIGINT NOT NULL REFERENCES users (id),
    item_key TEXT NOT NULL REFERENCES shop_items (key),
    quantity INT NOT NULL DEFAULT 1 CHECK (quantity > 0),
    purchased_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, item_key)
);

INSERT INTO shop_items (key, name, description, kind, price_minor, duration_days, metadata) VALUES
    ('slot', 'Portfolio slot', 'One additional concurrent position beyond the free 5.', 'SLOT', NULL, NULL, '{}'),
    ('analyst_tools', 'Analyst tools', 'Noisy earnings estimates on /calendar. Renews every 30 days.', 'ANALYST_TOOL', 500, 30, '{}'),
    ('theme_sunrise', 'Sunrise chart theme', 'Cosmetic chart color theme. No functional effect.', 'COSMETIC', 300, NULL, '{"chart_color": "#FF8C42"}'),
    ('theme_midnight', 'Midnight chart theme', 'Cosmetic chart color theme. No functional effect.', 'COSMETIC', 300, NULL, '{"chart_color": "#7C4DFF"}');
