"""Test helpers for the Lunatone DALI client.

The client module (lunatone_api.py) does not depend on Home Assistant, so it is
loaded on its own here instead of through the integration package, whose
__init__ imports Home Assistant.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

COMPONENT_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "dali_lunatone"


def _load_api():
    try:
        import websockets  # noqa: F401
    except ImportError:  # the client imports websockets; tests never open a connection
        websockets_stub = types.ModuleType("websockets")
        websockets_stub.ConnectionClosed = Exception
        websockets_stub.connect = None
        client_stub = types.ModuleType("websockets.client")
        client_stub.WebSocketClientProtocol = object
        sys.modules["websockets"] = websockets_stub
        sys.modules["websockets.client"] = client_stub

    package = types.ModuleType("dali_lunatone")
    package.__path__ = [str(COMPONENT_DIR)]
    sys.modules.setdefault("dali_lunatone", package)
    return importlib.import_module("dali_lunatone.lunatone_api")


@pytest.fixture
def api(monkeypatch):
    """The lunatone_api module, with a short refresh delay for fast tests."""
    module = _load_api()
    monkeypatch.setattr(module, "EXTERNAL_REFRESH_DELAY", 0.05)
    return module


@pytest.fixture
def make_client(api):
    """Return a factory for a connected-looking client with fake bus I/O.

    The client records every frame it sends in ``client.sent`` as
    ``(line, data)`` and answers brightness queries from ``client.levels``
    (key address -> brightness %).
    """

    def factory(lines=(0,)):
        client = api.LunatoneClient("gateway.invalid")
        client._ws = object()
        client._connected = True
        client.lines = list(lines)
        client.sent = []
        client.queried = []
        client.levels = {}
        client.updates = []

        async def fake_send(line, data, num_bits=16, send_twice=False, wait_for_answer=False):
            client.sent.append((line, list(data)))
            return {}

        async def fake_query(address, line=0):
            client.queried.append(address)
            return client.levels.get(address)

        client._send_dali_frame = fake_send
        client.query_brightness = fake_query
        client.add_callback(
            lambda event, *args: client.updates.append(args[0].address) if event == "state_update" else None
        )
        return client

    return factory


@pytest.fixture
def add_light(api):
    """Add a DALI light (off) to a client and return it."""

    def add(client, line, short_address, groups=(), protocol="DALI"):
        address = api.line_address(line, short_address)
        device = api.DaliDevice(address, protocol, 6, "LED modules")
        device.brightness = 0
        device.is_on = False
        device.groups = list(groups)
        client._devices[(protocol, address)] = device
        return device

    return add


class Bus:
    """Feeds daliMonitor frames into a client, as the gateway would."""

    def __init__(self, client):
        self.client = client
        self.timestamp = 1000.0

    async def frame(self, line, data, external=True, query=False, bits=16, timestamp=None):
        if timestamp is None:
            self.timestamp += 1  # distinct timestamps so frames are not deduplicated
            timestamp = self.timestamp
        await self.client._handle_message(
            {
                "type": "daliMonitor",
                "data": {
                    "bits": bits,
                    "data": list(data),
                    "line": line,
                    "externalSource": external,
                    "query": query,
                    "timestamp": timestamp,
                },
            }
        )


@pytest.fixture
def bus():
    return Bus
