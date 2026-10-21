from __future__ import annotations

from unittest import mock
import time
import unittest

from agent_repl import OpenAIAgentDriver, SingleAgentSession
from agent_repl import openai_driver

from test_openai_driver import SequenceClient


class KernelTimeoutTests(unittest.TestCase):
    def test_a_late_result_is_not_paired_with_the_next_evaluation(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)

        with self.assertRaises(TimeoutError):
            session.evaluate("import time\ntime.sleep(0.5)\n_result = 'late'", timeout=0.1)
        result = session.evaluate("_result = 'current'", timeout=5)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value, "current")

    def test_a_slow_model_program_fails_the_turn_without_breaking_later_turns(self) -> None:
        client = SequenceClient([
            "import time\ntime.sleep(0.5)",
            "inbox.reply_to_latest('done')",
        ])
        driver = OpenAIAgentDriver(model="test-model", client=client)
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("Run something slow.")

        with mock.patch.object(openai_driver, "PROGRAM_TIMEOUT_SECONDS", 0.1):
            slow = session.run_openai_turn(driver)
            time.sleep(0.6)  # the slow program finishes and its late result is waiting in the queue
            recovered = session.run_openai_turn(driver)

        self.assertEqual(slow.status, "error")
        self.assertIn("did not finish", slow.error)
        self.assertEqual(recovered.status, "ok")
        self.assertEqual(session.user_messages()[0].text, "done")


if __name__ == "__main__":
    unittest.main()
