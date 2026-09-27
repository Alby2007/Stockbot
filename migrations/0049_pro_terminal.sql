-- Shop expansion Phase D: Pro Terminal. An ANALYST_TOOL-class recurring
-- entitlement -- renewal machinery (entitlements.expires_at,
-- buy_item's duration_days renewal) is reused as-is. Unlocks the gated
-- /stock fields (7d range, realized vol, 24h flow split) and the 2w/1M
-- chart spans (MAX_SPAN_PRO in chart_view).

INSERT INTO shop_items (key, name, description, kind, price_minor, duration_days, metadata) VALUES
    ('pro_terminal', 'Pro Terminal', 'Deeper /stock stats (7d range, realized vol, flow split, ADV, event countdown) and 2w/1M chart spans. Renews every 30 days.', 'ANALYST_TOOL', 1000, 30, '{}');
