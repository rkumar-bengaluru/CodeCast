import json
import logging

from codecast import runlog
from codecast.agent.capture import LLMCapture
from codecast.agent.loop import DirectorAgent, initial_messages
from codecast.agent.submit import Submitter
from codecast.agent.tools import ToolBox
from codecast.config import load_config
from codecast.errors import CodecastError
from codecast.grounding.validator import ValidationContext
from codecast.llm.fake import FakeProvider, tool_call
from codecast.report import ReportBuilder

from tests.unit.storyboards import valid_submission


def test_request_and_response_written(tmp_path):
    c = LLMCapture(tmp_path / "llm")
    c.system_prompt("SYS")
    c.system_prompt("ignored second time")
    p = c.request(3, {"messages": []})
    c.response(3, {"ok": True})
    assert p.name == "step-003.request.json"
    assert json.loads((tmp_path / "llm" / "step-003.response.json").read_text(encoding="utf-8")) == {"ok": True}
    assert (tmp_path / "llm" / "system_prompt.md").read_text(encoding="utf-8") == "SYS"
    assert c.last_request_path == p


def test_no_request_body_clears_the_pointer(tmp_path):
    c = LLMCapture(tmp_path / "llm")
    c.request(1, {"messages": []})
    assert c.request(2, None) is None and c.last_request_path is None


def test_agent_captures_every_step_without_secrets(tiny_repo, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-123")
    provider = FakeProvider([tool_call("list_dir", {"path": "."}, "a"),
                             tool_call("submit_storyboard", {"storyboard": valid_submission()}, "b")])
    submitter = Submitter(ValidationContext(repomap=tiny_repo, llm={}, output_allowlist=["*.log"]))
    cap = LLMCapture(tmp_path / "llm")
    agent = DirectorAgent(provider, ToolBox(tiny_repo, submitter), max_steps=5, max_total_tokens=10**7,
                          wall_clock_s=600, sleep=lambda s: None, capture=cap)
    agent.run(initial_messages(tiny_repo, "THE SYSTEM PROMPT"))
    names = sorted(p.name for p in (tmp_path / "llm").iterdir())
    assert names == ["step-001.request.json", "step-001.response.json", "step-002.request.json",
                     "step-002.response.json", "system_prompt.md"]
    assert (tmp_path / "llm" / "system_prompt.md").read_text(encoding="utf-8") == "THE SYSTEM PROMPT"
    assert "sk-secret-123" not in "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "llm").iterdir())


def test_runlog_writes_and_releases_the_file(tmp_path):
    detach = runlog.attach(tmp_path, verbose=False)
    logging.getLogger("codecast.test").info("hello %s", "log")
    detach()
    text = (tmp_path / "run.log").read_text(encoding="utf-8")
    assert "INFO codecast.test: hello log" in text
    (tmp_path / "run.log").unlink()  # closed: Windows would refuse to delete an open file
    logging.getLogger("codecast.test").info("after detach")
    assert not (tmp_path / "run.log").exists()


def test_report_keeps_traceback_for_codecast_errors():
    rb = ReportBuilder("r", "x", load_config(env={}))
    try:
        try:
            raise ValueError("root cause")
        except ValueError as e:
            raise CodecastError("wrapped", stage="agent") from e
    except CodecastError as e:
        rb.fail(e, stage="agent")
    tb = rb.finish().failure.traceback
    assert tb and "root cause" in tb and "wrapped" in tb


def test_image_data_is_not_copied_into_dumps(tmp_path):
    big = "QUFB" * 5000
    body = {"messages": [
        {"content": [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{big}"}}]},
        {"content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": big}}]},
        {"content": "short text stays"}]}
    p = LLMCapture(tmp_path / "llm").request(1, body)
    text = p.read_text(encoding="utf-8")
    assert big not in text and "short text stays" in text
    assert text.count("20000 chars of base64 omitted") == 2
    assert body["messages"][0]["content"][0]["image_url"]["url"].endswith(big)   # caller's body untouched
