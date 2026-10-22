"""Pure calculations and input handling for the 96-well normalization protocol.

This module does not import PyLabRobot, so it can be tested without the
simulator or hardware.
"""
import csv
import random
from dataclasses import dataclass
from typing import Union

# Concentrations are expressed in nM and volumes in uL.
TARGET_CONCENTRATION_NM = 50.0
FINAL_VOLUME_UL = 100.0

# Skip wells where the excess volume is too small to remove reliably.
MIN_REMOVE_UL = 5.0

# Valid input concentration range specified in the task brief.
MIN_CONCENTRATION_NM = 40.0
MAX_CONCENTRATION_NM = 300.0

# Simulation-only assumptions. On a real instrument, replace the simulated
# starting volumes with a validated volume-measurement procedure.
SIM_VOLUME_RANGE_UL = (30.0, 150.0)
SIM_RANDOM_SEED = 42
SIM_BUFFER_WELL_UL = 150

# Standard 96-well plate coordinates.
ROWS = "ABCDEFGH"
COLS = range(1, 13)

# PyLabRobot plate initialization order: column by column.
WELLS_COLUMN_MAJOR = [f"{row}{col}" for col in COLS for row in ROWS]

# Protocol processing order: row by row, from A1 through H12.
WELLS_ROW_MAJOR = [f"{row}{col}" for row in ROWS for col in COLS]

# Standardized outcome labels used in the CSV and run summary.
STATUS_COMPLETED = "completed"
STATUS_SKIPPED_BELOW_TARGET = "skipped_below_target"
STATUS_SKIPPED_EXCESS_TOO_SMALL = "skipped_excess_below_min"
STATUS_REJECTED_INSUFFICIENT = "rejected_insufficient_volume"
STATUS_FAILED = "failed"


@dataclass
class WellResult:
    """Outcome of processing one well. Unused volume fields stay blank."""

    status: str
    error: str = ""
    measured: Union[float, str] = ""
    removed: Union[float, str] = ""
    buffer: Union[float, str] = ""


def load_concentrations(input_path):
    """Load and validate the 96 initial sample concentrations from CSV.

    The input file must contain the columns 'well' and 'concentration_nM'.
    Well identifiers are stripped and uppercased. All 96 expected wells must
    be present, and every concentration must be within the input range.

    Returns:
        dict[str, float]: Initial concentration indexed by well identifier.

    Raises:
        ValueError: If the well set or concentration range is invalid.

    Note:
        Duplicate well IDs are not rejected; later rows overwrite earlier
        ones. A production version should validate duplicates explicitly.
    """
    with open(input_path, newline="", encoding="utf-8") as f:
        concentrations = {
            row["well"].strip().upper(): float(row["concentration_nM"])
            for row in csv.DictReader(f)
        }

    if set(concentrations) != set(WELLS_ROW_MAJOR):
        raise ValueError("Input CSV must contain exactly all 96 well names.")

    for well, conc in concentrations.items():
        if not MIN_CONCENTRATION_NM <= conc <= MAX_CONCENTRATION_NM:
            raise ValueError(
                f"Concentration for {well} must be between "
                f"{MIN_CONCENTRATION_NM} and {MAX_CONCENTRATION_NM} nM; "
                f"got {conc} nM."
            )

    return concentrations


def make_simulated_volumes():
    """Generate reproducible simulated starting volumes for all 96 wells.

    The fixed random seed makes the scenario reproducible. These values
    feed the Chatterbox simulation and do not measure a physical plate.

    Returns:
        dict[str, float]: Simulated starting volume in uL for each well.
    """
    rng = random.Random(SIM_RANDOM_SEED)
    low, high = SIM_VOLUME_RANGE_UL

    return {
        well: round(rng.uniform(low, high), 1)
        for well in WELLS_COLUMN_MAJOR
    }


def plan_well(concentration, volume):
    """Calculate the retained sample, removed excess, and buffer volumes.

    The dilution equation is:

        concentration * keep_volume = target_concentration * final_volume

    Therefore:
        keep_volume   = target_concentration * final_volume / concentration
        remove_volume = starting_volume - keep_volume
        buffer_volume = final_volume - keep_volume

    Returns:
        tuple[float, float, float]:
            (keep_volume, remove_volume, buffer_volume), all in uL.

    Raises:
        ValueError: If the starting volume cannot supply the required
            retained sample.
    """
    keep = TARGET_CONCENTRATION_NM * FINAL_VOLUME_UL / concentration

    if volume < keep:
        raise ValueError(
            f"insufficient volume: measured {volume:.1f} uL, "
            f"need {keep:.1f} uL"
        )

    remove = volume - keep
    buffer = FINAL_VOLUME_UL - keep

    return keep, remove, buffer