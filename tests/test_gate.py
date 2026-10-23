"""Who uses the model next (gate.py): your messages before what Athena does by itself, in the order you sent them,
but never in the middle of something it's doing; and each model on its own."""

import asyncio

from athena.gate import Gate


async def test_your_messages_wait_for_whats_running_then_go_first():
    gate, order = Gate(), []
    started = asyncio.Event()

    async def background(name):
        async with gate.background("m", name):
            order.append(f"{name} starts")
            started.set()
            await asyncio.sleep(0.05)
            order.append(f"{name} ends")

    async def yours(name):
        async with gate.yours("m"):
            order.append(name)

    first = asyncio.create_task(background("review"))
    await started.wait()
    assert gate.busy("m") and gate.doing == {"m": "review"}
    second = asyncio.create_task(background("another"))
    await asyncio.sleep(0)
    messages = [asyncio.create_task(yours("hello")), asyncio.create_task(yours("are you there?"))]
    await asyncio.gather(first, second, *messages)
    assert order == ["review starts", "review ends", "hello", "are you there?", "another starts", "another ends"]
    assert not gate.busy("m") and gate.doing == {}


async def test_each_model_has_its_own_turn():
    gate = Gate()
    async with gate.background("big", "a long task"):
        async with asyncio.timeout(1):
            async with gate.yours("small"):  # doesn't wait for the other model
                assert gate.busy("big") and gate.busy("small")
