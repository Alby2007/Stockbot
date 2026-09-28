"""Synthetic trader population (NPCs): ordinary USER accounts flagged
`users.is_bot`, driven per-tick by the npc service with a one-time
NPC_STAKE grant and permadeath. Anonymous on every public surface --
the tape renders market mechanics unattributed and nothing else names
actors at all.

Invariants the rest of the system relies on:

- Bounded injection: FAUCET->NPC money flows only as NPC_STAKE ledger
  rows, exactly `npc.stake_minor` per spawned agent. When it's gone the
  agent is dead; there is no refill path.
- Aggregate flow is capped by `npc.max_tick_notional` per round -- the
  per-fill participation cap is keyed per order/fill, so it's the only
  bound on a whole population acting at once.
- Determinism is per-DECISION only: RNG is keyed
  seed|npc|user_id|tick so "why did agent N act" is replayable, but
  wall-clock arrival order against humans isn't (same limit as replay).
"""
