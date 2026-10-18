"""Tests for following commands sent by other bus masters (wall buttons) on line 0."""
from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def setup(make_client, add_light, bus):
    client = make_client()
    lights = [add_light(client, 0, address, groups=[1] if address in (1, 2) else []) for address in range(4)]
    return client, lights, bus(client)


def state(light):
    return light.is_on, light.brightness


def test_recall_max_marks_on_then_refreshes_level(setup):
    client, lights, feed = setup
    client.levels[0] = 100

    async def scenario():
        await feed.frame(0, [0x01, 5])
        assert state(lights[0]) == (True, None)
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert state(lights[0]) == (True, 100)
    assert client.queried == [0]


def test_off_is_applied_immediately_without_query(setup):
    client, lights, feed = setup
    lights[0].is_on, lights[0].brightness = True, 80

    async def scenario():
        await feed.frame(0, [0x01, 0])
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert state(lights[0]) == (False, 0)
    assert client.queried == []


def test_long_press_dimming_is_debounced_to_one_query_per_light(setup):
    client, lights, feed = setup
    client.levels.update({1: 60, 2: 60})

    async def scenario():
        for _ in range(6):
            await feed.frame(0, [0x83, 1])  # UP to group 1, repeated while held
            await asyncio.sleep(0.01)
        assert client.queried == []
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert sorted(client.queried) == [1, 2]
    assert state(lights[1]) == (True, 60)


def test_broadcast_and_dapc(setup):
    client, lights, feed = setup

    asyncio.run(feed.frame(0, [0xFE, 254]))
    assert all(state(light) == (True, 100) for light in lights)

    asyncio.run(feed.frame(0, [0xFF, 0]))
    assert all(state(light) == (False, 0) for light in lights)


def test_dapc_mask_is_ignored(setup):
    client, lights, feed = setup
    lights[3].is_on, lights[3].brightness = True, 100

    asyncio.run(feed.frame(0, [0x06, 255]))
    assert state(lights[3]) == (True, 100)


def test_scene_triggers_refresh(setup):
    client, lights, feed = setup
    client.levels[3] = 30

    async def scenario():
        await feed.frame(0, [0x07, 16 + 2])  # GO TO SCENE 2, address 3
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert client.queried == [3]
    assert state(lights[3]) == (True, 30)


@pytest.mark.parametrize(
    ("data", "kwargs"),
    [
        ([0x01, 0], {"external": False}),  # our own frame
        ([0x01, 0x90], {"query": True}),  # a query
        ([0xA3, 0], {}),  # special command (DTR0)
        ([0xC1, 8], {}),  # special command (enable device type)
        ([0x01, 0x21], {}),  # configuration command
    ],
)
def test_ignored_frames_change_nothing(setup, data, kwargs):
    client, lights, feed = setup
    lights[0].is_on, lights[0].brightness = True, 100

    async def scenario():
        await feed.frame(0, data, **kwargs)
        await asyncio.sleep(0.15)

    asyncio.run(scenario())
    assert state(lights[0]) == (True, 100)
    assert client.updates == [] and client.queried == []
