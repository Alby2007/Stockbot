-- Crossing book: resting limit orders cross each other at the maker's
-- price before falling through to the market maker.
--
-- Orders gain partial-fill tracking; trades record whether each side was
-- the maker (earlier order) or taker, who the counterparty was, and which
-- resting order it filled -- the provenance wash-trade detection and audit
-- need to distinguish a book cross from two independent MM trades.

ALTER TABLE orders
    ADD COLUMN filled_quantity BIGINT NOT NULL DEFAULT 0
        CHECK (filled_quantity >= 0),
    ADD CONSTRAINT orders_filled_lte_qty CHECK (filled_quantity <= quantity);

ALTER TABLE trades
    ADD COLUMN maker_taker TEXT CHECK (maker_taker IN ('MAKER', 'TAKER')),
    ADD COLUMN counterparty_user_id BIGINT REFERENCES users (id),
    ADD COLUMN order_id BIGINT REFERENCES orders (id);

-- Cross prices must stay near the mark: a resting order whose price is
-- more than this fraction from the quoted price cannot cross (anti-
-- collusion; two users can't print an off-market price at each other).
INSERT INTO config (key, value) VALUES ('cross.collar_pct', 0.02)
ON CONFLICT (key) DO NOTHING;
