-- Plan F: instrument lifecycle -- admin listings and delistings with a
-- fixed-mark settlement sweep. instruments.is_active already exists; the
-- schema needs three additions:
--
--   index_member: the tick computes the SBX-40 basket from live rows, so
--   without explicit membership a new listing would silently join the
--   index (and a delisted one would silently leave it). Seeded STOCK rows
--   are the fixed v1 basket; listings default FALSE.
--
--   delisted_tick: audit stamp for when the instrument went inactive.
--
--   bounded_shorts 'DELISTED': a delisting settles the short at intrinsic
--   value (collateral + entry - mark, floored at 0) -- distinct from a
--   voluntary 'CLOSED' and from a 'KNOCKED_OUT' wipe.

ALTER TABLE instruments
    ADD COLUMN index_member BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN delisted_tick BIGINT;

UPDATE instruments SET index_member = TRUE WHERE kind = 'STOCK';

ALTER TABLE bounded_shorts DROP CONSTRAINT bounded_shorts_status_check;
ALTER TABLE bounded_shorts ADD CONSTRAINT bounded_shorts_status_check
    CHECK (status IN ('OPEN', 'CLOSED', 'KNOCKED_OUT', 'DELISTED'));
