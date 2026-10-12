"""patchSpace's central daemon core: protocol, registry, graph, persistence.

Everything here is PipeWire-agnostic.  The PipeWire machinery lives in
``modules/pipewire`` (including the node model, ``modules/pipewire/pwnodes.py``)
and, from phase 1 of agents/PLAN.md on, talks to this core through the module
API instead of being imported by it.  Until the extraction's report step lands
(agents/EXTRACTION.md), ``central/main.py`` still reaches into that model
in-process.
"""
