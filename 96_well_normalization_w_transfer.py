import asyncio
import csv
import logging
from pathlib import Path

from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend
from pylabrobot.resources import (
    STARLetDeck, TIP_CAR_480_A00, PLT_CAR_L5AC_A00,
    DeepWell_96_Well, hamilton_96_tiprack_1000uL_filter,
    Liquid, set_tip_tracking, set_volume_tracking,
)
from pylabrobot.visualizer import Visualizer


# Configuration
TARGET_CONCENTRATION = 50.0
TARGET_VOLUME = 100.0
ROWS = "ABCDEFGH"
COLS = range(1, 13)

BASE = Path(__file__).resolve().parent
INPUT = BASE / "plate_concentrations.csv"
OUTPUT_DIR = BASE / "run_results"
OUTPUT = OUTPUT_DIR / "normalization_results.csv"
LOG_PATH = OUTPUT_DIR / "normalization.log"

FIELDS = [
    "well", "initial_concentration_nM", "sample_volume_uL",
    "buffer_volume_uL", "status", "error",
]

set_tip_tracking(True)
set_volume_tracking(True)


# Logging
def setup_logging():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, encoding="utf-8", mode="a"),
            logging.StreamHandler(),
        ],
        force=True,
    )


# CSV handling
def load_concentrations():
    with open(INPUT, newline="", encoding="utf-8") as f:
        concentrations = {
            row["well"].strip().upper(): float(row["concentration_nM"])
            for row in csv.DictReader(f)
        }

    expected = {f"{r}{c}" for r in ROWS for c in COLS}
    if concentrations.keys() != expected:
        raise ValueError("Input CSV must contain exactly all 96 well names.")

    for well, concentration in concentrations.items():
        if not 40 <= concentration <= 300:
            raise ValueError(
                f"Concentration for {well} must be between 40 and 300 nM; "
                f"got {concentration} nM."
            )

    return concentrations


def save_result(writer, file, well, concentration,
                sample="", buffer="", status="", error=""):
    writer.writerow({
        "well": well,
        "initial_concentration_nM": concentration,
        "sample_volume_uL": sample,
        "buffer_volume_uL": buffer,
        "status": status,
        "error": error,
    })
    file.flush()


# Deck setup
async def setup_deck():
    logging.info("Setting up liquid handler and deck")

    lh = LiquidHandler(
        backend=LiquidHandlerChatterboxBackend(),
        deck=STARLetDeck(),
    )
    await lh.setup()

    tips = TIP_CAR_480_A00(name="tips")
    tips[0] = sample_tips = hamilton_96_tiprack_1000uL_filter(
        name="sample_tips"
    )
    tips[1] = buffer_tips = hamilton_96_tiprack_1000uL_filter(
        name="buffer_tips"
    )
    lh.deck.assign_child_resource(tips, rails=1)

    plates = PLT_CAR_L5AC_A00(name="plates")
    plates[0] = samples_plate = DeepWell_96_Well(name="samples_plate")
    plates[1] = buffer_plate = DeepWell_96_Well(name="buffer_plate")
    plates[2] = destination_plate = DeepWell_96_Well(name="destination_plate")
    lh.deck.assign_child_resource(plates, rails=10)

    # Simulated placeholder liquids; replace with suitable liquid
    # definitions and volumes when preparing a real protocol.
    samples_plate.set_well_liquids([(Liquid.WATER, 50)] * 96)
    buffer_plate.set_well_liquids([(Liquid.WATER, 200)] * 96)

    logging.info("Deck setup completed")
    return lh, sample_tips, buffer_tips, samples_plate, buffer_plate, destination_plate


# Well processing
async def process_well(lh, well, concentration, sample_tips, buffer_tips,
                       samples_plate, buffer_plate, destination_plate):
    if concentration <= TARGET_CONCENTRATION:
        logging.warning(
            "%s skipped: concentration %.2f nM <= target %.2f nM",
            well, concentration, TARGET_CONCENTRATION,
        )
        return "", "", "skipped_below_target", ""

    sample = round(TARGET_CONCENTRATION * TARGET_VOLUME / concentration, 2)
    diluent = round(TARGET_VOLUME - sample, 2)

    logging.info(
        "%s starting: concentration=%.2f nM, sample=%.2f uL, buffer=%.2f uL",
        well, concentration, sample, diluent,
    )

    # Transfer sample
    try:
        logging.info(
            "%s sample transfer: source=%s[%s], target=%s[%s], volume=%.2f uL",
            well, samples_plate.name, well, destination_plate.name, well, sample,
        )

        await lh.pick_up_tips(sample_tips[well])
        await lh.aspirate(samples_plate[well], vols=[sample])
        await lh.dispense(destination_plate[well], vols=[sample])
        await lh.discard_tips()

        await asyncio.sleep(1)

        logging.info(
            "%s sample transfer completed: %s[%s] -> %s[%s]",
            well, samples_plate.name, well, destination_plate.name, well,
        )

    except Exception as exc:
        logging.exception(
            "%s sample transfer failed: source=%s[%s], target=%s[%s]",
            well, samples_plate.name, well, destination_plate.name, well,
        )

        try:
            await lh.discard_tips()
        except Exception:
            logging.exception("%s sample transfer tip cleanup failed", well)

        return sample, diluent, "failed", f"Sample transfer failed: {exc}"

    # Transfer buffer
    try:
        logging.info(
            "%s buffer_plate transfer: source=%s[%s], target=%s[%s], volume=%.2f uL",
            well, buffer_plate.name, well, destination_plate.name, well, diluent,
        )

        await lh.pick_up_tips(buffer_tips[well])
        await lh.aspirate(buffer_plate[well], vols=[diluent])
        await lh.dispense(destination_plate[well], vols=[diluent])
        await lh.discard_tips()

        await asyncio.sleep(1)

        logging.info(
            "%s buffer transfer completed: %s[%s] -> %s[%s]",
            well, buffer_plate.name, well, destination_plate.name, well,
        )

    except Exception as exc:
        logging.exception(
            "%s buffer transfer failed: source=%s[%s], target=%s[%s]",
            well, buffer_plate.name, well, destination_plate.name, well,
        )

        try:
            await lh.discard_tips()
        except Exception:
            logging.exception("%s buffer transfer tip cleanup failed", well)

        return sample, diluent, "failed", f"Buffer transfer failed: {exc}"

    logging.info("%s normalization completed", well)
    return sample, diluent, "completed", ""


# Main workflow
async def main():
    setup_logging()
    logging.info("Normalization run started")

    lh = None

    try:
        concentrations = load_concentrations()
        logging.info("Loaded 96 well concentrations from %s", INPUT)

        lh, sample_tips, buffer_tips, samples_plate, buffer_plate, destination_plate = (
            await setup_deck()
        )

        await Visualizer(resource=lh).setup()
        logging.info("Visualizer started at http://127.0.0.1:1337")
        print("Visualizer: http://127.0.0.1:1337")

        counts = {
            "completed": 0,
            "skipped_below_target": 0,
            "failed": 0,
        }

        with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            f.flush()

            for row in ROWS:
                for col in COLS:
                    well = f"{row}{col}"
                    concentration = concentrations[well]

                    sample, diluent, status, error = await process_well(
                        lh, well, concentration, sample_tips, buffer_tips,
                        samples_plate, buffer_plate, destination_plate,
                    )

                    save_result(
                        writer, f, well, concentration,
                        sample, diluent, status, error,
                    )
                    counts[status] += 1

                    logging.info("%s status: %s", well, status)

        logging.info(
            "Run summary: completed=%d, skipped=%d, failed=%d",
            counts["completed"],
            counts["skipped_below_target"],
            counts["failed"],
        )
        logging.info("Results saved to %s", OUTPUT)
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