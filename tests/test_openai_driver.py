from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_repl import OpenAIAgentDriver, OpenRouterAgentDriver, SingleAgentSession
from agent_repl.openai_driver import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
)


class FakeResponses:
    def __init__(self, output_text: str) -> None:
        self.output_text = output_text
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.output_text)


class FakeClient:
    def __init__(self, output_text: str) -> None:
        self.responses = FakeResponses(output_text)


class StreamResponses:
    def __init__(self, chunks: list[str]) -> None:
        self.chunks = chunks
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return iter(SimpleNamespace(type="response.output_text.delta", delta=chunk) for chunk in self.chunks)


class StreamClient:
    def __init__(self, chunks: list[str]) -> None:
        self.responses = StreamResponses(chunks)


class OpenAIDriverTests(unittest.TestCase):
    def test_default_model_is_the_cost_sensitive_experiment_tier(self) -> None:
        self.assertEqual(DEFAULT_OPENAI_MODEL, "gpt-5.6-luna")
        self.assertEqual(DEFAULT_REQUEST_TIMEOUT_SECONDS, 30.0)

    def test_openrouter_uses_the_free_router_by_default(self) -> None:
        client = FakeClient("_result = None")
        driver = OpenRouterAgentDriver(client=client)
        driver.plan([], {})
        self.assertEqual(DEFAULT_OPENROUTER_MODEL, "openrouter/free")
        self.assertEqual(client.responses.calls[0]["model"], "openrouter/free")

    def test_plan_uses_configured_model_when_stream_does_not_report_resolution(self) -> None:
        planned = OpenAIAgentDriver(model="test-model", client=FakeClient("_result = None")).plan([], {})

        self.assertEqual(planned.resolved_model, "test-model")

    def test_driver_uses_current_state_without_a_transcript(self) -> None:
        source = (
            "message = inbox.pending()[0]\n"
            "observable.publish('result', {'text': message['text']})\n"
            "inbox.ack(message['id'])\n"
            "user.inbox.add('Handled your request.')\n"
        )
        client = FakeClient(f"```python\n{source}```")
        driver = OpenAIAgentDriver(model="test-model", client=client)
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("Please assess this design.")

        result = session.run_openai_turn(driver)

        self.assertEqual(result.status, "ok")
        self.assertEqual(session.observe()[0].name, "result")
        self.assertEqual(session.user_messages()[0].text, "Handled your request.")
        request = client.responses.calls[0]
        self.assertEqual(request["model"], "test-model")
        self.assertIn("Please assess this design.", request["input"])
        self.assertNotIn("history", request["input"])

    def test_empty_model_program_is_presented_without_losing_the_message(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("Please handle this later.")

        result = session.run_openai_turn(OpenAIAgentDriver(client=FakeClient("")))

        self.assertEqual(result.status, "error")
        self.assertIn("empty agent program", result.error)
        self.assertIn("model_error", {value.name for value in session.observe()})
        self.assertEqual(session.supervisor.journal.pending("agent")[0].text, "Please handle this later.")

    def test_non_python_model_output_is_rejected_before_kernel_evaluation(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("Please handle this later.")

        result = session.run_openai_turn(OpenAIAgentDriver(client=FakeClient("User Safety: safe")))

        self.assertEqual(result.status, "error")
        self.assertIn("SyntaxError", result.error)
        runtime = next(value for value in session.observe() if value.name == "runtime")
        self.assertEqual(runtime.value["status"], "idle")
        self.assertEqual(session.supervisor.journal.pending("agent")[0].text, "Please handle this later.")

    def test_rejected_program_is_sent_back_as_explicit_model_feedback(self) -> None:
        client = FakeClient("User Safety: safe")
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("Please handle this later.")

        session.run_openai_turn(OpenAIAgentDriver(client=client))
        client.responses.output_text = "inbox.ack(inbox.pending()[0]['id'])"
        session.run_openai_turn(OpenAIAgentDriver(client=client))

        feedback = __import__("json").loads(client.responses.calls[1]["input"])["model_feedback"]
        self.assertEqual(feedback["rejected_output"], "User Safety: safe")
        self.assertIn("SyntaxError", feedback["error"])

    def test_legacy_console_commands_do_not_enter_model_context(self) -> None:
        source = "inbox.ack(inbox.pending()[-1]['id'])\n"
        client = FakeClient(source)
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send(":help")
        session.send("Say hello.")

        result = session.run_openai_turn(OpenAIAgentDriver(client=client))

        self.assertEqual(result.status, "ok")
        request = client.responses.calls[0]["input"]
        self.assertIn("Say hello.", request)
        self.assertNotIn(":help", request)

    def test_driver_uses_final_program_when_model_returns_multiple_fenced_alternatives(self) -> None:
        first = "inbox.ack(inbox.pending()[0]['id'])"
        final = "user.inbox.add('Hello, OS user!')\ninbox.ack(inbox.pending()[0]['id'])"
        driver = OpenAIAgentDriver(client=FakeClient(f"```python\n{first}\n```\n\n```python\n{final}\n```"))

        planned = driver.plan([], {})

        self.assertEqual(planned.source, final)

    def test_driver_streams_program_deltas_and_preserves_raw_program_log(self) -> None:
        source = "inbox.ack(inbox.pending()[0]['id'])\n"
        client = StreamClient(["```python\n", source, "```"])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            session = SingleAgentSession.open(str(root / "inbox.sqlite"), str(root / "state.sqlite"))
            try:
                session.send("Handle this.")
                deltas: list[str] = []

                result = session.run_openai_turn(OpenAIAgentDriver(client=client), on_delta=deltas.append)

                self.assertEqual(result.status, "ok")
                self.assertEqual("".join(deltas), f"```python\n{source}```")
                self.assertTrue(client.responses.calls[0]["stream"])
                self.assertEqual(session.model_program_log()[0]["raw_output"], "".join(deltas))
                self.assertEqual([entry["event"] for entry in session.repl_log()], ["model_program", "repl_result"])
                self.assertEqual(session.repl_log()[1]["status"], "ok")
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
