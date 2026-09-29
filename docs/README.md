# StockBot documentation

StockBot is a Discord synthetic stock-market: one continuous market
(two regional venues), one currency, a deterministic factor-model price
engine, and a Postgres database treated as the single source of truth.

| Doc | Contents |
|-----|----------|
| [architecture.md](architecture.md) | Process topology, tick pipeline, transaction model — with diagrams |
| [database.md](database.md) | Schema domains, the ledger, and the audit/invariant tables |
| [determinism.md](determinism.md) | Seeded RNG, factor replay, provably-fair card pulls, NPC determinism |
| [operations.md](operations.md) | Local setup, docker services, deploy pipeline, tooling |
| [features.md](features.md) | Player + admin command surface by domain |

Also see [AGENTS.md](../AGENTS.md) at the repo root — it is the dense
engineering log: design invariants, calibration notes, and the "why"
behind every subsystem. These docs are the map; AGENTS.md is the legend.
