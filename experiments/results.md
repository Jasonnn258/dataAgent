# Ablation results

- errored runs: 10 (see results.jsonl `error` field)
- total runs: 36  scored: 20

## locate

| mode | n | file_recall_at_k | file_precision_at_k |
|---|---|---|---|
| lexical | 2 | 1.0 | 0.6666 |
| semantica | 2 | 1.0 | 0.6666 |
| structural | 2 | 1.0 | 0.6666 |
| structural_git | 2 | 1.0 | 0.6666 |

## impact

| mode | n | precision | recall | f1 |
|---|---|---|---|---|
| lexical | 1 | 1.0 | 0.5 | 0.6667 |
| semantica | 1 | 0.6667 | 1.0 | 0.8 |
| structural | 1 | 0.6667 | 1.0 | 0.8 |
| structural_git | 1 | 0.6667 | 1.0 | 0.8 |

## rollback

| mode | n | precision | recall | preservation_rate | collateral_damage_rate |
|---|---|---|---|---|---|
| lexical | 2 | 1.0 | 0.75 | 1.0 | 0.0 |
| semantica | 2 | 1.0 | 1.0 | 1.0 | 0.0 |
| structural | 2 | 1.0 | 1.0 | 1.0 | 0.0 |
| structural_git | 2 | 1.0 | 1.0 | 1.0 | 0.0 |

## system

| mode | mean latency ms | mean context chars | mean tool calls | n |
|---|---|---|---|---|
| lexical | 108 | 7870 | 4.2 | 8 |
| semantica | 324 | 146 | 8.0 | 5 |
| structural | 167 | 7700 | 4.0 | 8 |
| structural_git | 82 | 146 | 6.4 | 5 |
