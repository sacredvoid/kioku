# Image tokens: 280 vs 560 (2026-11-08 03:43)

Sample: 600 indexed stills (seed 7); 27 label queries with 3+ matches.
Ground truth: Apple Photos labels. Ranking: visual similarity only.

| Label | matches | AP 560 | AP 280 | P@10 560 | P@10 280 |
|---|---|---|---|---|---|
| Document | 51 | 0.516 | 0.568 | 0.70 | 0.80 |
| Foliage | 53 | 0.390 | 0.406 | 0.70 | 0.70 |
| Automobile | 26 | 0.331 | 0.389 | 0.30 | 0.20 |
| Sunset | 22 | 0.936 | 0.917 | 1.00 | 1.00 |
| Food | 18 | 0.986 | 1.000 | 1.00 | 1.00 |
| Skyline | 27 | 0.330 | 0.308 | 0.30 | 0.20 |
| Mountain | 31 | 0.400 | 0.405 | 0.30 | 0.30 |
| Lake | 19 | 0.441 | 0.490 | 0.50 | 0.60 |
| Crowd | 25 | 0.683 | 0.624 | 0.80 | 0.70 |
| Bicycle | 11 | 1.000 | 0.984 | 1.00 | 0.90 |
| Stadium | 17 | 0.918 | 0.809 | 0.90 | 0.80 |
| Statue | 13 | 0.876 | 0.922 | 0.80 | 0.90 |
| Drink | 14 | 0.574 | 0.652 | 0.60 | 0.60 |
| Maple Tree | 18 | 0.413 | 0.511 | 0.60 | 0.60 |
| Eyeglasses | 36 | 0.556 | 0.625 | 0.80 | 0.80 |
| Luggage | 29 | 0.208 | 0.191 | 0.50 | 0.30 |
| Bench | 16 | 0.443 | 0.353 | 0.50 | 0.60 |
| Darkness | 15 | 0.350 | 0.292 | 0.30 | 0.20 |
| Water Body | 39 | 0.658 | 0.679 | 0.90 | 0.90 |
| Interior Room | 35 | 0.254 | 0.253 | 0.40 | 0.40 |
| Street | 28 | 0.593 | 0.731 | 0.50 | 0.90 |
| Sunglasses | 25 | 0.751 | 0.765 | 0.80 | 0.90 |
| Dish | 10 | 0.730 | 0.842 | 0.60 | 0.70 |
| Backpack | 10 | 0.403 | 0.293 | 0.50 | 0.40 |
| Forest | 20 | 0.136 | 0.169 | 0.00 | 0.00 |
| Performance | 10 | 0.240 | 0.222 | 0.30 | 0.30 |
| Sign | 25 | 0.090 | 0.125 | 0.10 | 0.10 |

**Mean AP:** 560 = 0.526, 280 = 0.538 (280 better on 16 of 27 labels)
**Mean P@10:** 560 = 0.581, 280 = 0.585
**Speed (same 64 files, batch 8):** 280 = 1.12 items/s, 560 = 0.42 items/s (2.66x)
**Vector agreement:** mean cosine(560, 280) = 0.978

## Decision (2026-11-08)

Use 280. Search quality is the same (mean AP 0.538 vs 0.526, P@10 0.585 vs 0.581, 280 ahead on 16 of 27 labels)
and it indexes 2.66x faster. Vectors from both settings agree closely (mean cosine 0.978), so the Aug to Nov 2026
items already indexed at 560 stay as they are; the rest of the library is indexed at 280.
Caveat: Photos labels are an imperfect ground truth and the sample is one library's last 3 months.
