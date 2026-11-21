# Memecoin desk report - run paper-20261114

Generated 2026-11-15 00:10 UTC. **Paper trading only.**

Window: 11-14 15:43 -> 11-15 00:10 UTC (8.5 h, 508 scan cycles)

## Account

| starting equity | cash | open MTM | equity | return | realised P&L | closed trades | win rate |
|---|---|---|---|---|---|---|---|
| $10,000.00 | $9,966.55 | $0.00 | $9,966.55 | -0.33% | $-33.45 | 3 | 67% |

## Closed trades

| symbol | opened | closed | score | cost | proceeds | P&L | % | exit |
|---|---|---|---|---|---|---|---|---|
| CSI | 11-14 00:16 | 11-14 00:18 | 84.2 | $290.58 | $342.58 | $52.01 | +17.9% | trailing_stop |
| WOHAIHAO | 11-14 03:24 | 11-14 03:41 | 72.9 | $69.89 | $73.59 | $3.70 | +5.3% | trailing_stop |
| bill | 11-14 23:19 | 11-14 23:31 | 70.2 | $804.51 | $715.35 | $-89.15 | -11.1% | stop_loss |

## Kills

317 kill decisions across 19 distinct coins (filter 209, analyst 108, executor 0). A coin can fail several rules at once.

| stage | rule | times hit |
|---|---|---|
| filter | low_volume_1h | 197 |
| analyst | score_below_70 | 108 |
| filter | low_liquidity | 59 |
| filter | already_pumped | 20 |
| filter | bundled | 2 |
| filter | top10_concentration | 1 |

## Analyst scores

109 scorecards, 1 at 70+.

| symbol | when | score | momentum | unique buyers | buy/sell | holder growth | liq/mcap | smart wallets |
|---|---|---|---|---|---|---|---|---|
| bill | 11-14 23:19 | **70.2** | 67 | 76 | 19 | 100 | 89 | - |
| JENSEN | 11-14 19:53 | **56.5** | 65 | 56 | 100 | 0 | 59 | - |
| Max | 11-14 17:53 | **43.4** | 30 | 54 | 62 | 20 | 52 | - |
| AMC | 11-14 15:43 | **42.0** | 70 | 28 | 100 | 1 | 5 | - |

## Every coin the desk evaluated

| coin | mint | first seen | last seen | checks | best score | trades | what stopped it (times) |
|---|---|---|---|---|---|---|---|
| FOOD | `FTu55M…` | 11-14 15:43 | 11-14 21:44 | 2 | - | - | bundled (2) |
| AMC | `HWya5C…` | 11-14 15:43 | 11-14 19:42 | 34 | 42.0 | - | score < 70 (24), low_volume_1h (10) |
| Max | `5qaP7o…` | 11-14 15:43 | 11-14 20:59 | 44 | 43.4 | - | score < 70 (29), low_volume_1h (15) |
| JENSEN | `B2YpP2…` | 11-14 15:43 | 11-15 00:06 | 71 | 56.5 | - | score < 70 (49), low_volume_1h (22) |
| MORI | `MoR1wh…` | 11-14 15:43 | 11-15 00:00 | 48 | - | - | low_volume_1h (48) |
| Christmax | `D7MyAD…` | 11-14 15:43 | 11-14 18:01 | 12 | - | - | low_volume_1h (12) |
| FLAG | `FvQHUA…` | 11-14 15:43 | 11-14 16:47 | 7 | - | - | low_volume_1h (7) |
| VTC | `3tjPCX…` | 11-14 15:43 | 11-14 16:57 | 8 | - | - | low_volume_1h (8) |
| ANTH | `2bvKtU…` | 11-14 15:43 | 11-14 16:25 | 5 | - | - | low_liquidity (5), low_volume_1h (5) |
| GCAT | `7U2kPF…` | 11-14 15:43 | 11-15 00:00 | 48 | - | - | low_liquidity (48), low_volume_1h (48) |
| QTC | `Bn9oW3…` | 11-14 16:01 | 11-14 16:01 | 1 | - | - | low_volume_1h (1) |
| AKT | `5d3fjP…` | 11-14 17:35 | 11-14 23:57 | 2 | - | - | low_volume_1h (2) |
| BAWSAQ | `7VQpUT…` | 11-14 18:31 | 11-14 19:25 | 6 | - | - | low_liquidity (6), low_volume_1h (6) |
| aura  | `HXW15J…` | 11-14 18:34 | 11-14 18:34 | 1 | - | - | already_pumped (1) |
| SHARKBUX | `ttwocS…` | 11-14 19:45 | 11-14 20:17 | 4 | - | - | low_volume_1h (4), already_pumped (2) |
| bill | `85RhcS…` | 11-14 20:23 | 11-15 00:05 | 14 | 70.2 | 1 ($-89.15) | already_pumped (7), score < 70 (6) |
| BLACKOUT | `HJWMay…` | 11-14 20:46 | 11-14 21:18 | 4 | - | - | low_volume_1h (4), already_pumped (4) |
| COA | `Cmf39V…` | 11-14 21:28 | 11-14 22:10 | 5 | - | - | low_volume_1h (5), already_pumped (5) |
| AURA | `EvbSSz…` | 11-14 23:56 | 11-15 00:06 | 2 | - | - | already_pumped (1), top10_concentration (1) |

## Smart-wallet book

learned: 2
