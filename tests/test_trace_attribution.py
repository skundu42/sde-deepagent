"""Subagent attribution in the event trace. Observed live (deepagents 0.6.8):
subgraph namespaces don't expose the task tool-call id, so attribution falls
back to the single in-flight delegation."""

from sde_deepagent.runner import TaskRunner


def _runner() -> TaskRunner:
    return TaskRunner.__new__(TaskRunner)  # _agent_for_ns needs no state


def test_empty_ns_is_orchestrator():
    assert _runner()._agent_for_ns((), {}, {}) == "orchestrator"


def test_ns_with_known_call_id_maps_directly():
    r = _runner()
    assert r._agent_for_ns(("tools:tc_1",), {"tc_1": "coder"}, {}) == "coder"


def test_single_active_delegation_fallback():
    r = _runner()
    # ns suffix unknown, but exactly one delegation in flight -> that one
    assert r._agent_for_ns(("subgraph:xyz",), {}, {"tc_9": "reviewer"}) == "reviewer"
    # same subagent delegated twice in parallel is still unambiguous
    assert r._agent_for_ns(("subgraph:xyz",), {},
                           {"a": "tester", "b": "tester"}) == "tester"


def test_ambiguous_or_no_delegation_stays_generic():
    r = _runner()
    assert r._agent_for_ns(("subgraph:xyz",), {}, {}) == "subagent"
    assert r._agent_for_ns(("subgraph:xyz",), {},
                           {"a": "coder", "b": "tester"}) == "subagent"
