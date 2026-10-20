# Quick first pass: normalize a 96-well plate to 50 nM. Generated from one prompt.
import asyncio
from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend
from pylabrobot.resources import STARLetDeck
from pylabrobot.resources import (
    TIP_CAR_480_A00, PLT_CAR_L5AC_A00, Cos_96_DW_1mL, HTF_L,
)

TARGET = 50        # nM
FINAL_VOLUME = 100 # uL

async def main():
    lh = LiquidHandler(backend=LiquidHandlerChatterboxBackend(), deck=STARLetDeck())
    await lh.setup()

    tip_car = TIP_CAR_480_A00(name="tip carrier")
    tip_car[0] = tips = HTF_L(name="tips")
    lh.deck.assign_child_resource(tip_car, rails=1)

    plt_car = PLT_CAR_L5AC_A00(name="plate carrier")
    plt_car[0] = sample_plate = Cos_96_DW_1mL(name="samples")
    plt_car[1] = buffer_plate = Cos_96_DW_1mL(name="buffer")
    lh.deck.assign_child_resource(plt_car, rails=10)

    # add buffer to each well to dilute to target
    await lh.pick_up_tips(tips["A1"])
    for i in range(96):
        await lh.aspirate(buffer_plate["A1"], vols=[50])
        await lh.dispense(sample_plate[i], vols=[50])
    await lh.drop_tips(tips["A1"])

    await lh.stop()

asyncio.run(main())