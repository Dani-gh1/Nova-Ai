# Backtest 2026-08-01 → 2026-10-30

Kilde: **synthetic** (seed 7). Alle tal er procent af startkapital pr. strategi.

> **Læs dette først:** Kørslen bruger *syntetiske* data (`desk/synthetic.py`). De tester, at mekanikken virker
> (stops, spread, swap, valutalofter, sessioner og nyheder), men siger intet om rigtig rentabilitet.
> Kun 7 af 17 strategier slår møntkast-kontrollen, og skyggehandlerne viser ingen sammenhæng mellem score og udfald.
> Det er det forventede resultat på data uden indbygget fordel. Mean reversion i Asien og breakout i London fanger
> de mønstre, generatoren faktisk indeholder. Kør igen på rigtige data (`--source csv:<mappe>`) og papirhandl live i flere måneder.

| # | Strategi | Afkast | Maks. DD | Handler | Hitrate | Gns. R | Profit factor | Sharpe | Swap | Spread |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | Carry | +11.71% | 26.10% | 206 | 40% | +0.08 | 1.09 | 0.65 | +1.99% | 9.70% |
| 2 | Breakout London | +11.01% | 12.82% | 171 | 37% | +0.07 | 1.11 | 0.97 | -0.29% | 8.88% |
| 3 | Scalp overlap | +7.74% | 17.23% | 334 | 44% | +0.03 | 1.04 | 0.71 | +0.00% | 32.28% |
| 4 | Tilfældig (kontrol) | +4.74% | 15.87% | 400 | 42% | +0.02 | 1.02 | 0.32 | -2.18% | 16.04% |
| 5 | Kun råvarer | +4.49% | 14.50% | 108 | 40% | +0.04 | 1.06 | 0.64 | -0.08% | 3.70% |
| 6 | Mean reversion Asien | +3.84% | 8.17% | 145 | 52% | +0.03 | 1.07 | 0.63 | +0.00% | 6.24% |
| 7 | Kræsen | +2.10% | 10.03% | 217 | 43% | +0.03 | 1.05 | 0.52 | +0.02% | 4.18% |
| 8 | Breakout New York | +1.15% | 24.08% | 185 | 34% | +0.02 | 1.01 | 0.10 | -0.37% | 10.15% |
| 9 | Langsom trend | -12.38% | 20.27% | 245 | 40% | -0.04 | 0.89 | -0.87 | -0.45% | 5.94% |
| 10 | Aggressiv | -16.89% | 27.00% | 117 | 38% | -0.07 | 0.87 | -1.08 | -0.68% | 9.48% |
| 11 | Korrelation | -17.36% | 25.32% | 82 | 32% | -0.22 | 0.64 | -1.59 | +0.16% | 2.99% |
| 12 | Kun majors | -18.23% | 25.96% | 168 | 37% | -0.11 | 0.81 | -1.87 | -0.55% | 6.91% |
| 13 | Nyhedsfilter | -18.96% | 26.66% | 153 | 37% | -0.13 | 0.76 | -2.12 | -0.18% | 5.55% |
| 14 | Trend | -19.61% | 25.47% | 224 | 39% | -0.09 | 0.82 | -1.65 | -0.23% | 7.83% |
| 15 | Standard | -20.52% | 25.85% | 243 | 39% | -0.08 | 0.83 | -1.60 | -0.52% | 8.59% |
| 16 | Momentum | -20.98% | 25.32% | 137 | 37% | -0.16 | 0.73 | -2.05 | -0.20% | 5.13% |
| 17 | Swing | -21.84% | 25.37% | 224 | 43% | -0.10 | 0.79 | -1.96 | -0.31% | 5.35% |
| 18 | Mange små | -21.96% | 25.85% | 1122 | 46% | -0.09 | 0.84 | -3.92 | -0.24% | 14.11% |

## Skyggehandler pr. scoreinterval

Hver scoret signal (bedste retning) fulgt med 2 ATR stop og 2R mål.

| Score | Antal | Hitrate | Gns. R |
|---|---:|---:|---:|
| 50-59 | 1118 | 36% | -0.011 |
| 60-69 | 2755 | 34% | -0.067 |
| 70-79 | 2332 | 38% | +0.038 |
| 80-89 | 1534 | 35% | -0.059 |
| 90-99 | 396 | 24% | -0.340 |

## Hyppigste afvisninger (Risk/Executor)

- strategien er stoppet i denne epoke (maks. drawdown): 126659
- har allerede en position i instrumentet: 90345
- dagligt tabsloft nået: 19379
- maks. 5 åbne positioner: 16257
- maks. 2 åbne positioner: 6600
- for stor USD-eksponering (4.0% risiko > 3.0%): 5234
- 2 stærkt korrelerede positioner i samme retning: 1937
- for tæt på weekendlukning: 1341
