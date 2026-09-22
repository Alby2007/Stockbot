-- Plan B: execution surface -- iceberg orders.
--
-- orders.display_qty caps how much of a resting order shows in the
-- /stock depth ladder (NULL = fully displayed, today's behavior). The
-- matcher still works the REAL remaining quantity -- the hidden size
-- refills the displayed slice until the order drains, like a real
-- iceberg's refresh. Bounds: positive and <= quantity (a display cap
-- larger than the order is just noise).
ALTER TABLE orders
    ADD COLUMN display_qty BIGINT
    CHECK (display_qty IS NULL OR (display_qty > 0 AND display_qty <= quantity));
