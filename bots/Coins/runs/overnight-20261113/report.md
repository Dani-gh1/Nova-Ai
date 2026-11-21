# Memecoin desk report - run overnight-20261113

Generated 2026-11-14 06:52 UTC. **Paper trading only.**

Window: 11-13 19:07 -> 11-14 06:52 UTC (11.7 h, 707 scan cycles)

## Account

| starting equity | cash | open MTM | equity | return | realised P&L | closed trades | win rate |
|---|---|---|---|---|---|---|---|
| $10,000.00 | $10,055.71 | $0.00 | $10,055.71 | +0.56% | $55.71 | 2 | 100% |

## Closed trades

| symbol | opened | closed | score | cost | proceeds | P&L | % | exit |
|---|---|---|---|---|---|---|---|---|
| CSI | 11-14 00:16 | 11-14 00:18 | 84.2 | $290.58 | $342.58 | $52.01 | +17.9% | trailing_stop |
| WOHAIHAO | 11-14 03:24 | 11-14 03:41 | 72.9 | $69.89 | $73.59 | $3.70 | +5.3% | trailing_stop |

## Kills

1005 kill decisions across 33 distinct coins (filter 545, analyst 457, executor 3). A coin can fail several rules at once.

| stage | rule | times hit |
|---|---|---|
| filter | low_volume_1h | 484 |
| analyst | score_below_70 | 457 |
| filter | low_liquidity | 345 |
| filter | already_pumped | 56 |
| filter | bundled | 3 |
| filter | mint_authority | 2 |
| filter | lp_unlocked | 2 |
| filter | top10_concentration | 2 |
| executor | price impact 2.38% > 2.0% | 1 |
| executor | price impact 2.21% > 2.0% | 1 |
| executor | re-entry cooldown | 1 |

## Analyst scores

462 scorecards, 5 at 70+.

| symbol | when | score | momentum | unique buyers | buy/sell | holder growth | liq/mcap | smart wallets |
|---|---|---|---|---|---|---|---|---|
| CSI | 11-14 00:16 | **84.2** | 87 | 100 | 100 | 89 | 39 | - |
| WOHAIHAO | 11-14 03:24 | **72.9** | 88 | 98 | 33 | 60 | 72 | - |
| Chunkee | 11-14 04:07 | **68.3** | 67 | 61 | 51 | 100 | 65 | - |
| SPEED | 11-13 22:29 | **63.6** | 70 | 42 | 81 | 60 | 70 | - |
| WANTED | 11-13 21:03 | **56.6** | 74 | 74 | 42 | 25 | 57 | - |
| JENSEN | 11-14 03:09 | **54.5** | 25 | 71 | 76 | 82 | 22 | - |
| inuhao | 11-14 02:47 | **54.1** | 99 | 77 | 4 | 9 | 59 | - |
| FOOD | 11-13 19:32 | **45.2** | 48 | 77 | 45 | 5 | 39 | - |
| Max | 11-14 01:41 | **43.8** | 60 | 16 | 100 | 0 | 47 | - |
| AMC | 11-13 21:29 | **39.2** | 53 | 37 | 100 | 2 | 0 | - |

## Every coin the desk evaluated

| coin | mint | first seen | last seen | checks | best score | trades | what stopped it (times) |
|---|---|---|---|---|---|---|---|
| inuhao | `FyHakv…` | 11-13 19:08 | 11-14 06:49 | 115 | 54.1 | - | score < 70 (115) |
| FOOD | `FTu55M…` | 11-13 19:08 | 11-14 03:25 | 25 | 45.2 | - | score < 70 (23), bundled (2) |
| Max | `5qaP7o…` | 11-13 19:08 | 11-14 06:51 | 94 | 43.8 | - | score < 70 (60), low_volume_1h (34) |
| WANTED | `BeLVQE…` | 11-13 19:08 | 11-13 23:48 | 47 | 56.6 | - | score < 70 (46), already_pumped (1) |
| SHARKBUX | `ttwocS…` | 11-13 19:08 | 11-14 06:47 | 68 | - | - | low_volume_1h (68) |
| QQQ | `QQQvDY…` | 11-13 19:08 | 11-13 19:38 | 4 | - | - | low_volume_1h (4) |
| LAB | `BQ1onb…` | 11-13 19:08 | 11-14 06:47 | 68 | - | - | low_liquidity (68), low_volume_1h (68) |
| PREDICT | `7GM6DT…` | 11-13 19:08 | 11-13 22:56 | 23 | - | - | low_liquidity (23), low_volume_1h (23) |
| PICO | `6rWAho…` | 11-13 19:08 | 11-14 06:47 | 68 | - | - | low_liquidity (68), low_volume_1h (68) |
| PICO | `6YMLWr…` | 11-13 19:08 | 11-14 05:34 | 61 | - | - | low_liquidity (61), low_volume_1h (61) |
| UODF | `GdauN3…` | 11-13 19:08 | 11-13 21:54 | 17 | - | - | low_liquidity (17), low_volume_1h (17) |
| GTA 6 Coin | `D2pZMT…` | 11-13 19:08 | 11-13 20:36 | 8 | - | - | low_liquidity (8), low_volume_1h (8) |
| AMC | `HWya5C…` | 11-13 19:34 | 11-14 06:49 | 107 | 39.2 | - | score < 70 (96), already_pumped (11) |
| AKT | `5d3fjP…` | 11-13 19:40 | 11-13 22:55 | 6 | - | - | low_volume_1h (6) |
| MORI | `MoR1wh…` | 11-13 19:50 | 11-14 01:50 | 2 | - | - | mint_authority (2), lp_unlocked (2), top10_concentration (2) |
| Shitcoin | `8p7GCo…` | 11-13 19:58 | 11-14 06:41 | 36 | - | - | low_volume_1h (36) |
| Ballfin | `9jh6f2…` | 11-13 20:35 | 11-13 20:35 | 1 | - | - | already_pumped (1) |
| Speed | `4cPQ6s…` | 11-13 21:05 | 11-13 21:33 | 2 | - | - | low_volume_1h (1), bundled (1) |
| punchhao | `C5jaQ3…` | 11-13 21:11 | 11-13 21:11 | 1 | - | - | already_pumped (1) |
| SPEED | `7csfMg…` | 11-13 21:51 | 11-13 23:04 | 11 | 63.6 | - | score < 70 (7), already_pumped (4) |
| AI | `A8oP4D…` | 11-13 21:59 | 11-13 23:03 | 7 | - | - | low_liquidity (7), low_volume_1h (7) |
| CSI | `CRwB4G…` | 11-13 23:07 | 11-14 06:51 | 63 | 84.2 | 1 ($52.01) | score < 70 (50), already_pumped (9), executor: price impact (2) |
| FOOD | `9YuoQD…` | 11-14 00:54 | 11-14 01:24 | 4 | - | - | low_liquidity (4), low_volume_1h (4) |
| PREDICT | `6dWweg…` | 11-14 00:59 | 11-14 03:38 | 16 | - | - | low_liquidity (16), low_volume_1h (16) |
| JENSEN | `B2YpP2…` | 11-14 01:23 | 11-14 06:51 | 50 | 54.5 | - | score < 70 (40), already_pumped (9), low_volume_1h (1) |
| GCAT | `7U2kPF…` | 11-14 01:49 | 11-14 06:44 | 29 | - | - | low_liquidity (29), low_volume_1h (23), already_pumped (6) |
| ANTH | `2bvKtU…` | 11-14 02:36 | 11-14 06:47 | 25 | - | - | low_liquidity (25), low_volume_1h (25) |
| WOHAIHAO | `4B1mCG…` | 11-14 03:14 | 11-14 05:11 | 17 | 72.9 | 1 ($3.70) | score < 70 (14), already_pumped (2) |
| Chunkee | `5ZDiuB…` | 11-14 03:39 | 11-14 04:35 | 9 | 68.3 | - | score < 70 (6), already_pumped (3) |
| HARAMBE | `EjoWiu…` | 11-14 03:45 | 11-14 03:45 | 1 | - | - | already_pumped (1) |
| SWJ | `69HP7a…` | 11-14 03:50 | 11-14 06:47 | 18 | - | - | low_liquidity (18), low_volume_1h (13), already_pumped (5) |
| Believe | `DE9TPo…` | 11-14 05:27 | 11-14 05:27 | 1 | - | - | low_liquidity (1), low_volume_1h (1) |
| ASTRALIS | `DWsutT…` | 11-14 05:29 | 11-14 06:11 | 3 | - | - | already_pumped (3) |

## Smart-wallet book

empty
