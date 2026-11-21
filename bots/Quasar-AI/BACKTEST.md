# Backtest-resultat (syntetiske data)

> **Syntetiske data.** Kørt i et miljø uden adgang til Binance/Bybit (geo-blokeret). Tallene viser at mekanikken virker (gearing, funding, stop, gebyrer, slippage, skyggehandler) – de siger **intet** om, hvorvidt et regelsæt tjener penge på et rigtigt marked. Det syntetiske marked har indbyggede trendregimer og tilbagerekyl efter kaskader, som visse regelsæt naturligt rammer.

Genskab:

```bash
python3 -m desk backtest --source synthetic --days 60 --seed 7
```

Med rigtige data (på en maskine med adgang til Binance):

```bash
python3 -m desk backtest --source binance --days 30
```

```

Backtest 2026-09-19 12:00 -> 2026-11-18 12:00 UTC  (1440 timer, 6 kontrakter)
Papirpenge vises i procent af startkapitalen.

Regelsæt                        Afkast  Største fald  Likv.  Handler   Træf   Funding   Gebyrer
-----------------------------------------------------------------------------------------------
Aggressiv 5x                   +99.76%        -7.86%      0       43    74%   -2.935%   -2.601%
Langsom trend 1x               +23.45%        -2.64%      0       37    68%   -0.654%   -0.404%
Aggressiv momentum 5x          +16.46%        -7.32%      0       58    50%   -1.264%   -2.325%
Trend 2x                       +16.00%        -1.61%      0       52    62%   -0.501%   -0.474%
Balanceret 2x                   +5.58%        -1.69%      0       53    55%   -0.335%   -0.472%
Kræsen kun long 2x              +4.20%        -1.42%      0       34    56%   -0.290%   -0.308%
Likvidations-rebound 3x         +4.15%        -0.92%      0       39    64%   +0.020%   -0.514%
Likvidations-rebound 2x         +3.61%        -0.66%      0       38    66%   +0.024%   -0.335%
Kræsen 2x                       +3.49%        -1.91%      0       24    58%   -0.215%   -0.215%
OI-divergens 2x                 +1.13%        -0.46%      0        8    62%   +0.001%   -0.065%
Breakout stramt 2x              +0.40%        -2.46%      0       59    41%   -0.179%   -0.524%
Short squeeze 2x                -0.08%        -0.56%      0        3    33%   +0.002%   -0.027%
Scalper 3x                      -0.55%        -1.89%      0      189    48%   -0.059%   -2.483%
Breakout + OI 3x                -0.85%        -1.31%      0        2     0%   -0.025%   -0.030%
Shorter trængte longs 2x        -4.73%        -5.42%      0       48    23%   +0.234%   -0.443%
Funding-carry 1x                -5.35%        -8.63%      0       38    18%   +0.401%   -0.383%
Kontrær positionering 1x        -6.63%        -7.83%      0       57    26%   +0.301%   -0.449%
Funding-carry 2x                -8.73%       -10.63%      0       55    20%   +0.343%   -0.541%

Skyggehandler (1x, begge sider af hvert signal):
  long  score 50-59:    16 stk, gns. +0.645%, træf 50%
  long  score 60-69:    46 stk, gns. +0.819%, træf 54%
  long  score   70+:     1 stk, gns. -3.100%, træf 0%
  long  score   <50:    35 stk, gns. -0.718%, træf 34%
  short score 50-59:    21 stk, gns. +1.055%, træf 62%
  short score 60-69:     7 stk, gns. +0.971%, træf 57%
  short score   70+:     3 stk, gns. -0.600%, træf 33%
  short score   <50:    75 stk, gns. -0.818%, træf 33%

```
