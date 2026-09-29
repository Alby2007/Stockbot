# Feature & command map

Everything reachable through Discord, grouped by subsystem. `/admin`
commands require `ADMIN_USER_IDS`; everything else is player-facing.
`{}` = subcommand group.

## Markets & trading

| Command | What it does |
|---------|--------------|
| `/market` | Venue sections: open/closed state, tick clock, movers per venue |
| `/stock` | Instrument detail — mark, session state of its venue, book depth |
| `/movers` `/sectors` | Biggest moves; per-sector rollups |
| `/chart` | Candle chart; stateless buttons (pan/zoom/span, time↔tick axis), per-user prefs |
| `/news` `/calendar` | Latest feed headlines; scheduled earnings/dividends |
| `/buy` `/sell` | Market fills vs the liquidity model (spread + concave impact + participation cap) |
| `/order {buy,sell,bracket,list,cancel}` | Resting LIMIT / STOP / STOP_LIMIT orders (+ bracket entry/exit pair); cross-book matching, GTD expiry |
| `/compare` `/whois` `/history` | Player comparison, profile lookup, trade history |
| `/alert {add,list,cancel}` | Price alerts → DM on cross |

## Account & progression

| Command | What it does |
|---------|--------------|
| `/balance` `/portfolio` `/positions` `/status` | Cash, holdings, margin state, account summary |
| `/claim` | Daily grant + the claim wheel (`claim.wheel_enabled`) |
| `/start` `/notify` | Onboarding, DM notification prefs |
| `/profile` | Net worth, badges, featured card + title flair |
| `/quests` `/reroll` | Daily quest progress; swap a quest with a token |
| `/title` `/theme` `/equip` `/unequip` | Cosmetics from the shop |
| `/leaderboard` `/leaderboard-setup` | Standings + per-channel auto-posts |
| `/sandbox {open,status,reset}` | Isolated practice season (`sandbox_access` shop unlock) |

## Leverage & shorts

| Command | What it does |
|---------|--------------|
| `/margin` `/collateral` `/liquidations` | True-margin shorts: health, collateral, liquidation history |
| `/short` `/shorts` `/cover` | Bounded (collateralized) shorts with knockout barriers |
| `/options {chain,buy,positions,sell}` | Cash-settled options vs the market maker |

`margin_tier` is a shop unlock — margin requires purchasing the tier
item first.

## Events & seasons

| Command | What it does |
|---------|--------------|
| `/ipo {list,subscribe}` | Browse open/settled offerings; commit cash before listing |
| `/league {info,join,standings}` | Season system: isolated league accounts, `league` flag on trades, equity standings |
| `/feed-setup` `/feed-remove` | Bind/unbind a channel to the public market-drama tape |

## Collectibles

| Command | What it does |
|---------|--------------|
| `/shop` | Category browser — packs are CONSUMABLE items, stack in `/open` |
| `/open` | Open a card pack (3 pulls; premium's third is GOLD+ floored) |
| `/collection` | Paginated binder — instruments by sector, lore, commemoratives |
| `/card` | Card detail (search by key, name, or ticker) |
| `/craft` | Shards → missing instrument card, or +1 frame on a held one |
| `/feature` | Pin a held card on profile/leaderboard flair |

Pulls are provably fair — see [determinism.md](determinism.md).

## Admin surface

| Command | What it does |
|---------|--------------|
| `/admin health` `/admin ledger-audit` `/admin recalc-balances` | Liveness, ledger invariant check, cache-vs-ledger reconciliation |
| `/admin tune` | Per-instrument price-engine params (TUNABLE_PARAMS bounds) |
| `/admin config` | Global config keys (CONFIG_BOUNDS; `pack.rate_*` must sum > 0) |
| `/admin wash-trades` | Compliance findings |
| `/admin season-create` `/admin season-close` | League lifecycle |
| `/admin instrument-add` `/admin delist` | Listings (delist freezes marks, never deletes) |
| `/admin ipo-create` | Schedule an offering |
| `/admin adjust` `/admin order-cancel` | Balance adjustment (journaled), force-cancel an order |
| `/admin disable` `/admin enable` `/admin user-info` | User suspension + inspection |
| `/admin npc-list` `/admin npc-spawn` `/admin npc-enable` `/admin npc-disable` | NPC trader census, spawn (archetype, stake bounds), toggles |

## Non-command surfaces

- **Tick engine** — 60 s cadence; price steps, events, orders, sweeps,
  bookkeeping (see [architecture.md](architecture.md)).
- **Public feed** — drama tape: whale prints, liquidations, knockouts,
  halts, EPIC+ card pulls, IPO fills, season results.
- **DM notifications** — alerts, badge grants, season outcomes,
  liquidation warnings.
- **NPC traders** — six archetypes (whale, grinder, yolo, shorter,
  liquidity provider, stop-loss monk) on bounded stakes with
  permadeath; excluded from all human surfaces.
