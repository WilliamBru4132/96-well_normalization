import asyncio
import csv
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend
from pylabrobot.resources import (
    STARLetDeck, TIP_CAR_480_A00, PLT_CAR_L5AC_A00,
    cor_96_wellplate_360uL_Fb, hamilton_96_tiprack_300uL_filter,
    Liquid, set_tip_tracking, set_volume_tracking,
)
from pylabrobot.visualizer import Visualizer

from normalization_logic import (
    SIM_BUFFER_WELL_UL,
    MIN_REMOVE_UL,
    TARGET_CONCENTRATION_NM,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REJECTED_INSUFFICIENT,
    STATUS_SKIPPED_BELOW_TARGET,
    STATUS_SKIPPED_EXCESS_TOO_SMALL,
    WELLS_COLUMN_MAJOR,
    WELLS_ROW_MAJOR,
    WellResult,
    load_concentrations,
    make_simulated_volumes,
    plan_well,
)

# Resolve paths relative to this script, rather than the launch directory.
BASE = Path(__file__).resolve().parent
INPUT = BASE / "plate_concentrations.csv"
RESULTS_ROOT = BASE / "run_results"
VISUALIZER_URL = "http://127.0.0.1:1337"

# Columns recorded for each processed well.
FIELDS = [
    "well",
    "initial_concentration_nM",
    "measured_volume_uL",
    "removed_volume_uL",
    "buffer_volume_uL",
    "status",
    "error",
]


@dataclass
class Deck:
    """Named handles for the deck resources used by the protocol."""

    lh: LiquidHandler
    remove_tips: object
    buffer_tips: object
    samples_plate: object
    buffer_plate: object
    archive_plate: object


# ---------------------------------------------------------------------------
# Logging and results output
# ---------------------------------------------------------------------------
def setup_logging(run_dir):
    """Configure console and file logging for one run folder.

    Creates the folder if needed and appends INFO-level events to the log
    file and the console. force=True replaces any existing root handlers.
    """
    run_dir.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(run_dir / "normalization.log",
                                encoding="utf-8", mode="a"),
            logging.StreamHandler(),
        ],
        force=True,
    )


def save_result(writer, file, well, concentration, result):
    """Write and flush one well's outcome. Flushing keeps partial runs usable."""
    writer.writerow({
        "well": well,
        "initial_concentration_nM": concentration,
        "measured_volume_uL": result.measured,
        "removed_volume_uL": result.removed,
        "buffer_volume_uL": result.buffer,
        "status": result.status,
        "error": result.error,
    })
    file.flush()


# ---------------------------------------------------------------------------
# Deck setup
# ---------------------------------------------------------------------------
async def setup_deck(simulated_volumes):
    """Create the liquid handler, tip racks, and plates with simulated liquids.

    Plates:
        samples_plate: samples to normalize in place.
        buffer_plate: diluent source.
        archive_plate: destination for removed excess sample.

    Buffer wells hold SIM_BUFFER_WELL_UL each. In a real-world scenario,
    buffer could be poured into a bulk container, which saves preparation
    time. Before hardware use, verify the deck layout, labware definitions,
    starting volumes, liquid properties, and tip compatibility.
    """
    logging.info("Setting up liquid handler and deck")

    lh = LiquidHandler(
        backend=LiquidHandlerChatterboxBackend(),
        deck=STARLetDeck(),
    )
    await lh.setup()

    # Separate tip racks keep removal and buffer tips apart.
    tips = TIP_CAR_480_A00(name="tips")
    tips[0] = remove_tips = hamilton_96_tiprack_300uL_filter(
        name="remove_tips"
    )
    tips[1] = buffer_tips = hamilton_96_tiprack_300uL_filter(
        name="buffer_tips"
    )
    lh.deck.assign_child_resource(tips, rails=1)

    plates = PLT_CAR_L5AC_A00(name="plates")
    plates[0] = samples_plate = cor_96_wellplate_360uL_Fb(name="samples_plate")
    plates[1] = buffer_plate = cor_96_wellplate_360uL_Fb(name="buffer_plate")
    plates[2] = archive_plate = cor_96_wellplate_360uL_Fb(name="archive_plate")
    lh.deck.assign_child_resource(plates, rails=7)

    # Initialize sample volumes in PyLabRobot's expected plate order.
    # Confirm the order against the installed PyLabRobot version.
    samples_plate.set_well_liquids(
        [(Liquid.WATER, simulated_volumes[well]) for well in WELLS_COLUMN_MAJOR]
    )
    buffer_plate.set_well_liquids([(Liquid.WATER, SIM_BUFFER_WELL_UL)] * 96)

    logging.info("Deck setup completed")

    return Deck(
        lh=lh,
        remove_tips=remove_tips,
        buffer_tips=buffer_tips,
        samples_plate=samples_plate,
        buffer_plate=buffer_plate,
        archive_plate=archive_plate,
    )


# ---------------------------------------------------------------------------
# Liquid handling primitives
# ---------------------------------------------------------------------------
async def transfer(lh, well, tips, source, target, volume, label):
    """Move volume from source[well] to target[well] using one fresh tip.

    Raises any PyLabRobot exception to the caller, which decides how to
    record the failure.
    """
    logging.info(
        "%s %s transfer: source=%s[%s], target=%s[%s], volume=%.2f uL",
        well, label, source.name, well, target.name, well, volume,
    )

    await lh.pick_up_tips(tips[well])
    await lh.aspirate(source[well], vols=[volume])
    await lh.dispense(target[well], vols=[volume])

    # Discard the tip, which has contacted the sample.
    await lh.discard_tips()

    logging.info(
        "%s %s transfer completed: %s[%s] -> %s[%s], %.2f uL",
        well, label, source.name, well, target.name, well, volume,
    )

    # Sleep to see animation in visualizer
    await asyncio.sleep(0.5)


async def discard_after_failure(lh, well, label):
    """Best-effort tip discard after a failed transfer. Never raises."""
    try:
        await lh.discard_tips()
    except Exception:
        logging.exception("%s %s tip cleanup failed", well, label)


# ---------------------------------------------------------------------------
# Per-well normalization
# ---------------------------------------------------------------------------
async def process_well(lh, well, concentration, measured_volume, deck):
    """Normalize one well by removing excess sample and adding buffer.

    Processing sequence:
        1. Skip samples below the target concentration.
        2. Plan the keep, remove, and buffer volumes.
        3. Reject wells with insufficient starting volume before pipetting.
        4. Skip wells where the planned removal is below MIN_REMOVE_UL.
        5. Transfer the excess sample to the archive well.
        6. Add buffer to the original well to reach the final volume.

    A reported failure does not guarantee that no liquid moved. If buffer
    addition fails after removal, the well is left below its intended final
    volume. Inspect the actual state before any recovery or retry.
    """
    # Dilution cannot increase concentration, so these wells are not processed.
    if concentration <= TARGET_CONCENTRATION_NM:
        logging.warning(
            "%s skipped: concentration %.2f nM <= target %.2f nM",
            well, concentration, TARGET_CONCENTRATION_NM,
        )
        return WellResult(
            STATUS_SKIPPED_BELOW_TARGET,
            error="concentration at or below target",
        )

    # Plan every volume before any liquid-handling operation starts.
    try:
        keep, remove, buffer = plan_well(concentration, measured_volume)
    except ValueError as exc:
        logging.warning("%s rejected: %s", well, exc)
        return WellResult(
            STATUS_REJECTED_INSUFFICIENT,
            error=str(exc),
            measured=measured_volume,
        )

    keep = round(keep, 1)
    remove = round(remove, 1)
    buffer = round(buffer, 1)

    # Very small excess volumes may be unreliable to pipette.
    if remove < MIN_REMOVE_UL:
        logging.warning(
            "%s skipped: excess %.2f uL below minimum %.2f uL",
            well, remove, MIN_REMOVE_UL,
        )
        return WellResult(
            STATUS_SKIPPED_EXCESS_TOO_SMALL,
            error=f"excess {remove:.2f} uL below minimum {MIN_REMOVE_UL} uL",
            measured=measured_volume,
        )

    logging.info(
        "%s starting: C0=%.2f nM, measured=%.2f uL, "
        "keep=%.2f uL, remove=%.2f uL, buffer=%.2f uL",
        well, concentration, measured_volume, keep, remove, buffer,
    )

    # Step 1: Remove the excess sample to the archive plate.
    try:
        await transfer(
            lh, well, deck.remove_tips,
            deck.samples_plate, deck.archive_plate, remove, "removal",
        )
    except Exception as exc:
        logging.exception(
            "%s removal failed: source=%s[%s], target=%s[%s]",
            well, deck.samples_plate.name, well,
            deck.archive_plate.name, well,
        )
        await discard_after_failure(lh, well, "removal")
        return WellResult(
            STATUS_FAILED,
            error=f"Removal failed: {exc}",
            measured=measured_volume,
            removed=remove,
        )

    # Step 2: Add buffer to the original well to reach the target volume.
    try:
        await transfer(
            lh, well, deck.buffer_tips,
            deck.buffer_plate, deck.samples_plate, buffer, "buffer",
        )
    except Exception as exc:
        logging.exception(
            "%s buffer transfer failed: source=%s[%s], target=%s[%s]",
            well, deck.buffer_plate.name, well,
            deck.samples_plate.name, well,
        )
        await discard_after_failure(lh, well, "buffer")

        # Removal already succeeded, so the well may hold only the retained
        # sample volume and not the intended final volume.
        return WellResult(
            STATUS_FAILED,
            error=f"Buffer addition failed after removal: {exc}",
            measured=measured_volume,
            removed=remove,
        )

    logging.info("%s normalization completed", well)

    return WellResult(
        STATUS_COMPLETED,
        measured=measured_volume,
        removed=remove,
        buffer=buffer,
    )


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------
async def main():
    """Run the simulated 96-well normalization workflow.

    Workflow:
        1. Ask for a run name and set up the run folder and logging.
        2. Load and validate the concentration CSV.
        3. Generate simulated starting volumes and initialize the deck.
        4. Start the PyLabRobot Visualizer.
        5. Process wells in row-major order, saving each result immediately.
        6. Log outcome counts and stop the liquid handler.

    Per-well failures are recorded and processing continues. Errors that
    abort the whole workflow are logged and re-raised.
    """
    # Tip and volume tracking are simulation aids, enabled for each run.
    set_tip_tracking(True)
    set_volume_tracking(True)

    run_name = input("Run name: ").strip()
    if not run_name:
        raise ValueError("Run name must not be empty.")

    run_dir = RESULTS_ROOT / run_name
    output_csv = run_dir / "normalization_results.csv"

    setup_logging(run_dir)
    logging.info("Normalization run started (in-place with removal)")

    lh = None

    try:
        concentrations = load_concentrations(INPUT)
        logging.info("Loaded 96 well concentrations from %s", INPUT)

        simulated_volumes = make_simulated_volumes()
        print("sim", simulated_volumes)

        deck = await setup_deck(simulated_volumes)
        lh = deck.lh

        await Visualizer(resource=lh).setup()
        logging.info("Visualizer started at %s", VISUALIZER_URL)
        print(f"Visualizer: {VISUALIZER_URL}")

        counts = Counter()

        with open(output_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            f.flush()

            for well in WELLS_ROW_MAJOR:
                concentration = concentrations[well]
                measured = simulated_volumes[well]

                result = await process_well(
                    lh, well, concentration, measured, deck,
                )

                save_result(writer, f, well, concentration, result)
                counts[result.status] += 1
                logging.info("%s status: %s", well, result.status)

        logging.info(
            "Run summary: completed=%d, below_target=%d, "
            "excess_too_small=%d, rejected_insufficient=%d, failed=%d",
            counts[STATUS_COMPLETED],
            counts[STATUS_SKIPPED_BELOW_TARGET],
            counts[STATUS_SKIPPED_EXCESS_TOO_SMALL],
            counts[STATUS_REJECTED_INSUFFICIENT],
            counts[STATUS_FAILED],
        )
        logging.info("Results saved to %s", output_csv)
        logging.info("Normalization run finished")

    except Exception:
        logging.exception("Normalization run aborted")
        raise

    finally:
        if lh is not None:
            try:
                await lh.stop()
                logging.info("Liquid handler stopped")
            except Exception:
                logging.exception("Failed to stop liquid handler")


if __name__ == "__main__":
    asyncio.run(main())