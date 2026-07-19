from __future__ import annotations

from pathlib import Path
import threading
import time
import unittest

from agent_repl import InboxJournal, IsolatedExecution, Supervisor


class RuntimeSpikeTests(unittest.TestCase):
    def test_message_is_durable_without_an_agent_handler(self) -> None:
        path = Path(self._testMethodName + ".sqlite")
        self.addCleanup(path.unlink, missing_ok=True)
        journal = InboxJournal(str(path))
        supervisor = Supervisor(journal)
        supervisor.create_repl("agent")

        message = supervisor.append_user_message("agent", "Use Postgres, not SQLite.")
        supervisor.close()
        journal.close()

        restored = InboxJournal(str(path))
        self.addCleanup(restored.close)
        self.assertEqual(restored.pending("agent"), [message])
        self.assertEqual(restored.event_kinds(), ["inbox.message_added"])

    def test_delivery_is_scheduled_after_append_not_run_reentrantly(self) -> None:
        journal = InboxJournal()
        self.addCleanup(journal.close)
        supervisor = Supervisor(journal)
        self.addCleanup(supervisor.close)
        supervisor.create_repl("agent")
        entered_handler = threading.Event()
        release_handler = threading.Event()

        def handler(_message) -> None:
            entered_handler.set()
            release_handler.wait(timeout=1)

        supervisor.subscribe_inbox("agent", handler)
        message = supervisor.append_user_message("agent", "change direction")
        self.assertEqual(journal.pending("agent"), [message])
        self.assertTrue(entered_handler.wait(timeout=1))
        release_handler.set()

        deadline = time.monotonic() + 1
        while journal.pending("agent") and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(journal.pending("agent"), [])

    def test_repl_queue_serializes_execution_units(self) -> None:
        journal = InboxJournal()
        self.addCleanup(journal.close)
        supervisor = Supervisor(journal)
        self.addCleanup(supervisor.close)
        repl = supervisor.create_repl("agent")
        order: list[str] = []
        first_started = threading.Event()
        allow_first_to_finish = threading.Event()

        def first() -> None:
            order.append("first-start")
            first_started.set()
            allow_first_to_finish.wait(timeout=1)
            order.append("first-end")

        def second() -> None:
            order.append("second")

        first_future = repl.submit(first)
        self.assertTrue(first_started.wait(timeout=1))
        second_future = repl.submit(second)
        self.assertFalse(second_future.done())
        allow_first_to_finish.set()
        first_future.result(timeout=1)
        second_future.result(timeout=1)
        self.assertEqual(order, ["first-start", "first-end", "second"])

    def test_isolated_execution_supports_imports_and_cancellation(self) -> None:
        import_execution = IsolatedExecution("import math\n_result = math.sqrt(81)")
        import_execution.start()
        self.assertEqual(import_execution.result(), ("ok", 9.0))

        runaway = IsolatedExecution("while True:\n    pass")
        runaway.start()
        time.sleep(0.05)
        runaway.cancel()
        self.assertFalse(runaway.alive)


if __name__ == "__main__":
    unittest.main()
