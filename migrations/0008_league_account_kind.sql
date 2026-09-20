-- Phase 1: add the LEAGUE account kind. In its own migration because a new
-- enum value can't be referenced (CHECK constraints, index predicates,
-- inserts) inside the same transaction that adds it; the next migration
-- runs in a fresh transaction where 'LEAGUE' is safe to use.

ALTER TYPE account_kind ADD VALUE 'LEAGUE';
