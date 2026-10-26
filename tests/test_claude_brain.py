"""The Claude brain, exercised against a fake Anthropic client (no network / API key needed).

These tests pin the wire contract: tool-name mapping, tool_use/tool_result pairing when the stateless
brain rebuilds history, no forced tool_choice, and tolerant handling of prose or refusals.
"""
from types import SimpleNamespace as NS

import pytest

from centralign.brains.base import PlanRequest, StepContext
from centralign.brains.claude import BrainError, ClaudeBrain
from centralign.memory import Memory
from centralign.models import Action, Check, HistoryItem, Observation, Plan, Step


class FakeClient:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []
        self.messages = NS(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return self.responses.pop(0)


def tool_use(name, **inp):
    return NS(stop_reason="tool_use", stop_details=None, content=[NS(type="tool_use", id="x", name=name, input=inp)])


def prose(text="I think we should..."):
    return NS(stop_reason="end_turn", stop_details=None, content=[NS(type="text", text=text)])


@pytest.fixture
def ctx():
    plan = Plan(understanding="u", steps=[Step(id="s1", description="Open the form", expected="form visible")])
    hist = [
        HistoryItem(action=Action(tool="browser.goto", args={"url": "http://x"}, thought="open it"), observation=Observation(ok=True, summary="loaded")),
        HistoryItem(action=Action(tool="browser.click", args={"target": 3}), observation=Observation(ok=False, error="element not found")),
    ]
    return StepContext(goal="g", plan=plan, step=plan.steps[0], step_idx=0, history=hist, step_results=[], guidance=["be careful"],
                       memory_text="ctx", tool_specs=[{"name": "browser.goto", "description": "d", "input_schema": {"type": "object"}}])


def test_next_decision_maps_names_pairs_history_and_never_forces_tools(ctx):
    client = FakeClient(tool_use("browser__read_page"))
    d = ClaudeBrain("claude-opus-5-5", client).next_decision(ctx)
    assert d.kind == "act" and d.action.tool == "browser.read_page"
    call = client.calls[0]
    assert call["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert "temperature" not in call and "thinking" not in call and "budget_tokens" not in str(call)
    assert all("." not in t["name"] for t in call["tools"])
    msgs = call["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    for a, r in zip(msgs[1::2], msgs[2::2]):
        use = next(b for b in a["content"] if b["type"] == "tool_use")
        assert r["content"][0]["tool_use_id"] == use["id"] and use["name"] == use["name"].replace(".", "__")
    assert msgs[4]["content"][0]["is_error"] is True
    assert "be careful" in msgs[0]["content"] and "Open the form" in msgs[0]["content"]


def test_control_tools_become_decisions(ctx):
    brain = lambda r: ClaudeBrain("m", FakeClient(r))
    assert brain(tool_use("step_done", summary="ok", facts={"id": 7}, skipped=True)).next_decision(ctx).skipped
    assert brain(tool_use("step_failed", reason="no")).next_decision(ctx).kind == "failed"
    d = brain(tool_use("ask_human", question="which one?")).next_decision(ctx)
    assert d.kind == "ask" and d.question == "which one?"


def test_prose_answer_is_reprompted_then_gives_up(ctx):
    client = FakeClient(prose(), tool_use("step_failed", reason="r"))
    assert ClaudeBrain("m", client).next_decision(ctx).kind == "failed"
    assert "exactly one of the provided tools" in client.calls[1]["messages"][-1]["content"]
    with pytest.raises(BrainError):
        ClaudeBrain("m", FakeClient(prose(), prose(), prose())).next_decision(ctx)


def test_refusal_is_surfaced(ctx):
    r = NS(stop_reason="refusal", stop_details=NS(category="cyber"), content=[])
    with pytest.raises(BrainError, match="declined"):
        ClaudeBrain("m", FakeClient(r)).next_decision(ctx)


def test_plan_parsing_and_replan_prompt():
    from centralign.config import Settings
    from centralign.store import Store
    import tempfile, pathlib

    mem = Memory(Settings.load().knowledge_dir, Store(pathlib.Path(tempfile.mkdtemp()) / "d.db"))
    c = mem.recall("onboard")
    steps = [{"id": "s1", "description": "do", "expected": "done"}]
    acc = [{"id": "c1", "description": "file there", "type": "file_exists", "params": {"path": "a"}}]
    client = FakeClient(tool_use("submit_plan", understanding="U", steps=steps, acceptance=acc, missing_info=[{"key": "k", "question": "q?"}]))
    req = PlanRequest(goal="g", memory_text=Memory.render(c), memory=c, tool_names=["fs.read"], failure="step x failed",
                      prior_plan=Plan(understanding="old"))
    plan = ClaudeBrain("m", client).plan(req)
    assert plan.steps[0].id == "s1" and plan.acceptance[0].type == "file_exists" and plan.missing_info[0].key == "k"
    text = client.calls[0]["messages"][0]["content"]
    assert "REPLAN" in text and "step x failed" in text and "csv_has_row" in text


def test_judge_and_reflect():
    j = ClaudeBrain("m", FakeClient(tool_use("verdict", passed=True, reasoning="fine")))
    assert j.judge(Check(id="c", description="d", type="llm_judge"), "evidence") == (True, "fine")
    from centralign.models import RunState

    r = ClaudeBrain("m", FakeClient(tool_use("save_lessons", lessons=["a", "b"])))
    assert r.reflect(RunState(goal="g"), "trail") == ["a", "b"]


def test_derive_checks_is_a_separate_call_that_never_sees_a_plan():
    acc = [{"id": "q1", "description": "record active", "type": "http_json", "params": {"url": "http://127.0.0.1/x", "expect": {"a": 1}}}]
    client = FakeClient(tool_use("submit_checks", acceptance=acc))
    out = ClaudeBrain("m", client).derive_checks("Onboard X", "SOP: completion criteria ...", {"department": "Design"})
    assert [c.id for c in out] == ["q1"]
    prompt = client.calls[0]["messages"][0]["content"]
    assert "have NOT seen any plan" in prompt and "completion criteria" in prompt and "department: Design" in prompt
    assert [t["name"] for t in client.calls[0]["tools"]] == ["submit_checks"]
