from __future__ import annotations

from types import SimpleNamespace
import unittest

from agent_repl import OpenAIAgentDriver, SingleAgentSession


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
