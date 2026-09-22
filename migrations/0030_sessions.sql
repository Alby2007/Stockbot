-- Plan C: session auctions + pending-event halts.
--
-- instruments.next_halting_event_tick mirrors next_event_tick but is
-- restricted to EARNINGS -- the scheduled, material events that real
-- exchanges halt into. refresh_next_event_ticks maintains both
-- aggregates in one UPDATE (self-healing, can't drift). DIVIDEND events
-- don't halt (they're a mechanical cash transfer, not information) and
-- NEWS doesn't (it's unscheduled -- halting would telegraph it).
ALTER TABLE instruments
    ADD COLUMN next_halting_event_tick BIGINT;

INSERT INTO config (key, value) VALUES
    -- session.auction_ticks: the closing-auction window -- on each of
    -- the LAST N open ticks (ticks_until_close <= N) match_orders
    -- switches pass 1 to a uniform-price clearing auction and skips the
    -- MM fallback. 0 disables the auction entirely.
    ('session.auction_ticks', 30),
    -- event.halt_lead_ticks: T1-style halt -- opening exposure on an
    -- instrument is blocked from (resolve_tick - lead) until its
    -- EARNINGS event resolves. Risk-reducing trades stay legal, same
    -- as circuit-breaker halts. 0 disables.
    ('event.halt_lead_ticks', 10)
ON CONFLICT (key) DO NOTHING;
