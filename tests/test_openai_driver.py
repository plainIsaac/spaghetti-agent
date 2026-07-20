from __future__ import annotations

from types import SimpleNamespace
import unittest

from agent_repl import OpenAIAgentDriver, OpenRouterAgentDriver, SingleAgentSession
from agent_repl.openai_driver import DEFAULT_OPENAI_MODEL, DEFAULT_OPENROUTER_MODEL


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


class OpenAIDriverTests(unittest.TestCase):
    def test_default_model_is_the_cost_sensitive_experiment_tier(self) -> None:
        self.assertEqual(DEFAULT_OPENAI_MODEL, "gpt-5.6-luna")

    def test_openrouter_uses_the_free_router_by_default(self) -> None:
        client = FakeClient("_result = None")
        driver = OpenRouterAgentDriver(client=client)
        driver.plan([], {})
        self.assertEqual(DEFAULT_OPENROUTER_MODEL, "openrouter/free")
        self.assertEqual(client.responses.calls[0]["model"], "openrouter/free")

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


if __name__ == "__main__":
    unittest.main()
