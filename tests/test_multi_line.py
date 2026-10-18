"""Tests for multi-line (multi-bus) support."""
from __future__ import annotations

import asyncio


def test_address_helpers(api):
    assert api.split_address(4) == (0, 4)
    assert api.split_address(68) == (1, 4)
    assert api.split_address(4, 2) == (2, 4)  # explicit line only applies below 64
    assert api.line_address(1, 4) == 68
    assert api.device_label("DALI", 4) == "DALI Address 4"
    assert api.device_label("DALI", 68) == "DALI Line 1 Address 4"
    assert api.bus_model(68, "DALI DT6") == "Bus 1 · DALI DT6"


def test_device_line_properties(api, make_client, add_light):
    client = make_client()
    light = add_light(client, 1, 4)
    assert (light.line, light.short_address) == (1, 4)


def test_commands_go_to_the_devices_line(make_client):
    client = make_client(lines=(0, 1))

    asyncio.run(client.turn_off(68))
    assert client.sent == [(1, [(4 << 1) | 1, 0])]

    client.sent.clear()
    asyncio.run(client.set_brightness(4, 100))
    assert client.sent == [(0, [4 << 1, 254])]


def test_group_and_broadcast_commands_only_update_their_line(make_client, add_light):
    client = make_client(lines=(0, 1))
    ground = add_light(client, 0, 4, groups=[0])
    upstairs = add_light(client, 1, 4, groups=[0])
    other = add_light(client, 1, 13)

    asyncio.run(client.set_group_brightness(0, 254, 1))
    assert client.sent[-1] == (1, [0x80, 254])
    assert upstairs.is_on and not ground.is_on

    asyncio.run(client.set_broadcast_brightness(254, 1))
    assert other.is_on and not ground.is_on


def test_wall_button_frames_resolve_to_the_frames_line(make_client, add_light, bus):
    client = make_client(lines=(0, 1))
    ground = add_light(client, 0, 4, groups=[0])
    upstairs = add_light(client, 1, 4, groups=[0])
    feed = bus(client)

    async def scenario():
        await feed.frame(1, [4 << 1, 254])  # DAPC A4 on line 1
        assert upstairs.is_on and not ground.is_on

        ground.is_on, ground.brightness = True, 100
        await feed.frame(1, [0x81, 0])  # OFF group 0 on line 1
        assert not upstairs.is_on and ground.is_on

    asyncio.run(scenario())


def test_frames_on_unselected_lines_are_ignored(make_client, add_light, bus):
    client = make_client(lines=(0,))
    light = add_light(client, 0, 4)

    asyncio.run(bus(client).frame(2, [4 << 1, 254]))
    assert not light.is_on


def test_duplicate_detection_includes_the_line(make_client, add_light, bus):
    client = make_client(lines=(0, 1))
    ground = add_light(client, 0, 4)
    upstairs = add_light(client, 1, 4)
    feed = bus(client)

    async def scenario():
        # The same frame on two lines with the same timestamp is two commands, not a duplicate
        await feed.frame(0, [4 << 1, 254], timestamp=5000.0)
        await feed.frame(1, [4 << 1, 254], timestamp=5000.0)

    asyncio.run(scenario())
    assert ground.is_on and upstairs.is_on


def test_refresh_after_level_command_queries_the_key_address(make_client, add_light, bus):
    client = make_client(lines=(0, 1))
    light = add_light(client, 1, 13)
    client.levels[77] = 40

    async def scenario():
        await bus(client).frame(1, [(13 << 1) | 1, 5])  # RECALL MAX
        await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert client.queried == [77]
    assert (light.is_on, light.brightness) == (True, 40)


def test_dali2_event_resolves_to_the_frames_line(make_client, add_light, bus):
    client = make_client(lines=(0, 1))
    coupler = add_light(client, 1, 0, protocol="DALI2")
    coupler.instances = {0: {"type": 1}}
    events = []
    client.add_callback(lambda event, *args: events.append((event, args[0].address)) if event == "dali2_event" else None)

    asyncio.run(bus(client).frame(1, [1, 0x80, 2], bits=24))
    assert ("dali2_event", 64) in events


def test_scanning_a_line_keeps_other_lines(api, make_client, add_light, monkeypatch):
    client = make_client(lines=(0, 1))
    kept = add_light(client, 0, 7)
    present_on_line_1 = {4, 18}

    async def scan_send(line, data, num_bits=16, send_twice=False, wait_for_answer=False):
        is_device_type_query = num_bits == 16 and len(data) == 2 and data[1] == 153
        if line == 1 and is_device_type_query and (data[0] >> 1) in present_on_line_1:
            return {"daliData": 6}
        return {}

    async def no_sleep(*args, **kwargs):
        return None

    client._send_dali_frame = scan_send
    monkeypatch.setattr(api.asyncio, "sleep", no_sleep)
    asyncio.run(client.scan_devices(1))

    assert client._devices[("DALI", 7)] is kept
    assert sorted(client._devices) == [("DALI", 7), ("DALI", 68), ("DALI", 82)]
