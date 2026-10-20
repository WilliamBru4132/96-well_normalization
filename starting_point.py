import asyncio
from pylabrobot.liquid_handling import LiquidHandler
from pylabrobot.liquid_handling.backends import LiquidHandlerChatterboxBackend
from pylabrobot.resources import STARLetDeck
from pylabrobot.resources import (
    TIP_CAR_480_A00, PLT_CAR_L5AC_A00, DeepWell_96_Well,
    hamilton_96_tiprack_1000uL_filter, Liquid,
)
from pylabrobot.visualizer import Visualizer
from pylabrobot.resources import set_tip_tracking, set_volume_tracking


set_tip_tracking(True), set_volume_tracking(True)



async def main():
    lh = LiquidHandler(backend=LiquidHandlerChatterboxBackend(), deck=STARLetDeck())
    await lh.setup()

    tip_car = TIP_CAR_480_A00(name="tip carrier")
    tip_car[0] = tips = hamilton_96_tiprack_1000uL_filter(name="tips")
    lh.deck.assign_child_resource(tip_car, rails=1)

    plt_car = PLT_CAR_L5AC_A00(name="plate carrier")
    plt_car[0] = sample_plate = DeepWell_96_Well(name="samples")
    plt_car[1] = buffer_plate = DeepWell_96_Well(name="buffer")
    lh.deck.assign_child_resource(plt_car, rails=10)

    buffer_plate["A1"][0].set_liquids([(Liquid.WATER, 2000)])

    vis = Visualizer(resource=lh)
    await vis.setup()  # open http://127.0.0.1:1337 in your browser
    sample_plate["A1"][0].set_liquids([(Liquid.WATER, 2000)])
    await asyncio.sleep(3)

    buffer_plate["A1"][0].set_liquids([(Liquid.WATER, 2000)])
    sample_plate["A1"][0].set_liquids([(Liquid.WATER, 100)])
    await asyncio.sleep(3)



    await lh.pick_up_tips(tips["A1"])
    for i in range(40):
        await lh.aspirate(buffer_plate["A1"], vols=[50])
        await lh.dispense(sample_plate[i], vols=[50])
        await asyncio.sleep(0.5)
    await lh.drop_tips(tips["A1"])

    await asyncio.sleep(60)  # keep the visualizer open
    await lh.stop()

asyncio.run(main())