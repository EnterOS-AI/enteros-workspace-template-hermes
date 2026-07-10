"""ADR-004 §4 adapter-socket conformance — inherited from the SDK.

The SDK ships the single, SSOT conformance battery
(``molecule_plugin.adapter_conformance.AdapterConformance``); this template
opts in with a ~5-line subclass that points it at this repo's ``Adapter``.
pytest then collects every ``test_*`` the base class defines against the hermes
adapter, asserting it satisfies the socket: identity/lifecycle present, the
MCP-config seam renders -> reads/present-probes in lockstep on hermes' OWN
``~/.hermes/config.yaml`` (byte-stable + additive + idempotent), enumerate honours
the tri-state with a stubbed spawn, and unmapped runtimes fail closed. See
``adapter.py`` for the socket methods (path/render/present/enumerate/persona)
this proves.
"""
from molecule_plugin.adapter_conformance import AdapterConformance

from adapter import Adapter


class TestHermesAdapterConformance(AdapterConformance):
    adapter_class = Adapter
