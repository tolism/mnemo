---
title: Risk Assessment - Electrical Safety
client: demo-retail
---

## Hazard Identification

The medical device presents the following hazards during normal use:

1. Electrical shock from exposed contacts
2. Thermal injury from overheating battery
3. Software malfunction leading to incorrect readings

## Risk Estimation

| Hazard | Severity | Probability | Risk Level |
|--------|----------|-------------|------------|
| Electrical shock | 5 | 2 | 10 (Medium) |
| Thermal injury | 4 | 3 | 12 (High) |
| Software malfunction | 3 | 4 | 12 (High) |

## Risk Control

- Double insulation barrier for electrical contacts
- Thermal cutoff at 45C with redundant sensor
- Watchdog timer with safe-state fallback
