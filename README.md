# 96-Well Protein Concentration Normalization

Normalizes a 96-well plate of expressed protein to a uniform concentration (50 nM) and a uniform final volume (100 µL) by diluting each sample in its own well. Written in Python with PyLabRobot for a Hamilton STAR. Currently runs on PyLabRobot's Chatterbox simulator.

## Task

Each well contains protein at a different concentration. Every well must be diluted to **50 nM** in a fixed final volume of **100 µL**, by adding buffer. Input concentrations are provided in `plate_concentrations.csv` and fall between 40 and 300 nM.

## How It Works

Each sample stays in its own well. Diluting it to a fixed 100 µL requires two steps: first remove the excess sample, then add buffer.

The dilution equation `C₀ · V_keep = C_target · V_final` gives the volume of sample to keep:

```
keep   = 50 × 100 / C₀
remove = measured_volume − keep
buffer = 100 − keep
```

Removing the excess leaves the retained sample at the same concentration, C₀. Adding buffer then brings it to 100 µL at 50 nM.

For each well, the script:

1. Skips the well if its concentration is below 50 nM, since dilution cannot raise a concentration.
2. Calculates `keep`, `remove`, and `buffer`. If the measured volume is less than `keep`, the well is rejected before any liquid moves.
3. Skips the well if `remove` is under 5 µL, because volumes that small are unreliable to pipette.
4. Aspirates `remove` µL from `samples_plate` and dispenses it into `archive_plate`, using its own tip.
5. Aspirates `buffer` µL from `buffer_plate` and dispenses it back into the original sample well, using a separate tip.

## Requirements

- Python 3.9 or later 
- PyLabRobot 0.2.2 or later, installed with:

```bash
py -m pip install pylabrobot
```

## Simulation Assumptions

Starting volumes are simulated. Each well receives a value between 30 and 150 µL, drawn from a fixed random seed (`42`) so runs are reproducible. Each buffer well holds 200 µL. Water is used as the buffer liquid. Volume and tip tracking are enabled. These values are for the simulator only. A real instrument would need a validated volume measurement in place of the simulated values or volumes known in advance.

## Files

- `96_well_normalization_in_place_dilution.py`: the deck and liquid-handling protocol.
- `normalization_logic.py`: the calculations and input validation. Keep it in the same folder as the script.
- `plate_concentrations.csv`: the measured input concentrations.

## Running

```bash
py 96_well_normalization_in_place_dilution.py
```

Enter a run name when prompted. The script creates `run_results/<run_name>/` and writes:

- `normalization_results.csv`: one row per well with its concentration, measured, removed, and buffer volumes, status, and any error.
- `normalization.log`: the full event log for the run.

The PyLabRobot Visualizer opens at `http://127.0.0.1:1337` so you can watch the simulated deck.

## Status Values

| Status | Meaning |
|---|---|
| `completed` | Excess removed and buffer added successfully. |
| `skipped_below_target` | Concentration is below 50 nM. |
| `skipped_excess_below_min` | Excess to remove is under 5 µL. |
| `rejected_insufficient_volume` | Starting volume cannot supply the required sample. |
| `failed` | A liquid-handling step raised an error. |

A `failed` well may be partially processed. For example, removal can succeed and buffer addition can then fail, leaving the well below 100 µL. Check the actual state before retrying.

## Notes

Before any run on physical hardware, verify the deck layout, labware definitions, liquid properties, tip compatibility, and starting volumes.