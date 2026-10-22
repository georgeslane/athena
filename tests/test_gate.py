"""Who uses the model next (gate.py): your messages between tasks, tasks after you've been quiet for a moment, and
your messages while a task waits for your OK."""

import asyncio

from athena.gate import Gate


async def started(coroutine):
    task = asyncio.create_task(coroutine)
    await asyncio.sleep(0.02)
    return task


async def test_your_message_waits_for_the_task_and_the_next_task_waits_for_you():
    gate = Gate(grace=0.0)
    order = []

    async def task(name, hold):
        async with gate.task(name):
            order.append(f"{name} starts")
            await hold.wait()
            order.append(f"{name} ends")

    async def yours():
        async with gate.yours():
            order.append("you")

    first, second = asyncio.Event(), asyncio.Event()
    a = await started(task("a", first))
    assert gate.held and gate.working == "a"
    you = await started(yours())
    b = await started(task("b", second))
    assert order == ["a starts"] and not you.done()
    first.set()
    await asyncio.wait_for(you, 1)
    second.set()
    await asyncio.wait_for(asyncio.gather(a, b), 1)
    assert order == ["a starts", "a ends", "you", "b starts", "b ends"]  # yours before the next task


async def test_a_task_waits_until_youve_been_quiet_for_the_grace_period():
    gate = Gate(grace=0.2)
    async with gate.yours():
        pass
    loop = asyncio.get_running_loop()
    began = loop.time()
    async with gate.task("a"):
        waited = loop.time() - began
    assert waited >= 0.15
    async with gate.task("b", grace=False):  # one you asked for doesn't wait
        pass


async def test_while_a_task_waits_for_your_ok_your_messages_go_ahead():
    gate = Gate(grace=0.0)
    asked, answered = asyncio.Event(), []

    async def task():
        async with gate.task("a"):
            async with gate.aside():
                await asked.wait()
            answered.append("task carries on")

    a = await started(task())
    assert not gate.held  # it's waiting for your OK, not using the model
    async with gate.yours():
        answered.append("you")
    asked.set()
    await asyncio.wait_for(a, 1)
    assert answered == ["you", "task carries on"]


async def test_a_message_stopped_while_it_waits_leaves_the_gate_as_it_was():
    gate = Gate(grace=0.0)
    hold = asyncio.Event()

    async def task():
        async with gate.task("a"):
            await hold.wait()

    a = await started(task())

    async def yours():
        async with gate.yours():
            pass

    you = await started(yours())
    you.cancel()
    await asyncio.sleep(0.01)
    assert gate.users == 0
    hold.set()
    await a
    async with gate.task("b"):  # nothing left waiting holds it up
        pass
