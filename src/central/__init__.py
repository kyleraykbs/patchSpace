"""patchSpace's central daemon core: graph model, protocol, persistence.

Everything here is PipeWire-agnostic.  The PipeWire machinery lives in
``modules/pipewire`` and, from phase 1 of agents/PLAN.md on, talks to
this core through the module API instead of being imported by it.
"""
