from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from agent_repl import OpenAIAgentDriver, SingleAgentSession
from agent_repl.openai_driver import driver_from_policy
from agent_repl.opencode_driver import OpencodeAgentDriver, OpencodeClient, OpencodeCompletion, QuotaExceeded, _resets_in

from test_openai_driver import SequenceClient


class FakeOpencode(OpencodeClient):
    def __init__(self, work_dir: Path, outputs: list[str]) -> None:
        super().__init__(work_dir)
        self.outputs = outputs
        self.calls: list[dict] = []
        self.deleted: list[str] = []

    def available(self) -> bool:
        return True

    def complete(self, model: str, agent: str, message: str, session_id: str | None = None) -> OpencodeCompletion:
        self.calls.append({"model": model, "agent": agent, "message": message, "session_id": session_id})
        usage = {"input_tokens": 40, "uncached_input_tokens": 10, "cache_read_tokens": 30, "cache_write_tokens": 0,
                 "output_tokens": 5, "reasoning_tokens": 0, "total_tokens": 45}
        return OpencodeCompletion(self.outputs.pop(0), f"ses_{len(self.calls)}", usage, 0.1)

    def delete_session(self, session_id: str) -> None:
        self.deleted.append(session_id)


class OpencodeDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work_dir = Path(directory.name)

    def test_each_activation_is_a_fresh_session_with_the_runtime_instructions(self) -> None:
        client = FakeOpencode(self.work_dir, ["```python\ninbox.reply_to_latest('Done.')\n```"])
        driver = OpencodeAgentDriver("provider/model", client)
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("Please answer.")

        result = session.run_openai_turn(driver)

        self.assertEqual(result.status, "ok")
        self.assertEqual(session.user_messages()[0].text, "Done.")
        call = client.calls[0]
        self.assertIsNone(call["session_id"])
        self.assertEqual(call["model"], "provider/model")
        self.assertIn("Please answer.", json.loads(call["message"])["activation"][0]["text"])
        agent_file = self.work_dir / ".opencode" / "agent" / f"{call['agent']}.md"
        self.assertIn("persistent Python REPL", agent_file.read_text(encoding="utf-8"))
        self.assertIn('"*": false', agent_file.read_text(encoding="utf-8"))
        self.assertEqual(client.deleted, ["ses_1"])

    def test_provider_usage_includes_prompt_cache_reads(self) -> None:
        client = FakeOpencode(self.work_dir, ["inbox.reply_to_latest('ok')"])
        planned = OpencodeAgentDriver("provider/model", client).plan([{"kind": "inbox_message", "text": "hi"}], None)
        self.assertEqual(planned.usage["input_tokens"], 40)
        self.assertEqual(planned.usage["cache_read_tokens"], 30)

    def test_policy_accepts_opencode_provider(self) -> None:
        driver = driver_from_policy({"provider": "opencode", "model": "openai/gpt-6-luna"})
        self.assertIsInstance(driver, OpencodeAgentDriver)
        self.assertEqual(driver.model, "openai/gpt-6-luna")


class QuotaTests(unittest.TestCase):
    def test_reset_time_is_read_from_the_provider_error_body(self) -> None:
        error = {"type": "provider.quota", "message": "The usage limit has been reached",
                 "response": {"body": json.dumps({"error": {"type": "usage_limit_reached", "resets_in_seconds": 16406}})}}
        self.assertEqual(_resets_in(error), 16406)
        self.assertIn("resets in 16406s", str(QuotaExceeded(error["message"], _resets_in(error))))

    def test_missing_reset_time_is_tolerated(self) -> None:
        self.assertIsNone(_resets_in({"response": {"body": "not json"}}))


class ProgramObservationTests(unittest.TestCase):
    def test_printed_output_is_returned_with_the_kernel_result(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        result = session.evaluate("print('rows:', 3)")
        self.assertEqual(result.output, "rows: 3\n")

    def test_observed_output_reaches_the_next_activation_for_the_same_message(self) -> None:
        client = SequenceClient(["print('total', 42)", "inbox.reply_to_latest('The total is 42.')"])
        driver = OpenAIAgentDriver(model="test-model", client=client)
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("What is the total?")

        session.run_openai_turn(driver, observe_program_output=True)
        session.run_openai_turn(driver, observe_program_output=True)

        second = json.loads(client.responses.calls[1]["input"])["activation"][0]
        self.assertEqual(second["previous_program"]["output"], "total 42\n")
        self.assertEqual(second["previous_program"]["source"], "print('total', 42)")
        self.assertEqual(session.user_messages()[0].text, "The total is 42.")

    def test_observation_is_off_by_default(self) -> None:
        client = SequenceClient(["print('total', 42)", "inbox.reply_to_latest('ok')"])
        driver = OpenAIAgentDriver(model="test-model", client=client)
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("What is the total?")

        session.run_openai_turn(driver)
        session.run_openai_turn(driver)

        self.assertNotIn("previous_program", json.loads(client.responses.calls[1]["input"])["activation"][0])


if __name__ == "__main__":
    unittest.main()
