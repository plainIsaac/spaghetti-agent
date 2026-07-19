from __future__ import annotations

from pathlib import Path
import threading
import time
import unittest

from agent_repl import InboxJournal, IsolatedExecution, ObservableStateRegistry, Supervisor


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

    def test_persistent_kernel_keeps_state_and_receives_durable_messages(self) -> None:
        journal = InboxJournal()
        self.addCleanup(journal.close)
        supervisor = Supervisor(journal)
        self.addCleanup(supervisor.close)
        supervisor.create_repl("agent")
        supervisor.append_user_message("agent", "Use Postgres.")
        kernel = supervisor.start_agent_kernel("agent")

        self.assertEqual(kernel.evaluate("answer = 40 + 2").status, "ok")
        self.assertEqual(kernel.evaluate("_result = answer").value, 42)
        self.assertEqual(kernel.evaluate("_result = inbox.pending()").value, [{"id": 1, "sender": "user", "text": "Use Postgres."}])

    def test_observable_state_is_explicit_and_revisioned(self) -> None:
        registry = ObservableStateRegistry()
        self.addCleanup(registry.close)
        supervisor = Supervisor(InboxJournal(), registry)
        self.addCleanup(supervisor.close)
        self.addCleanup(supervisor.journal.close)

        first = supervisor.publish_state("agent", "progress", {"phase": "research", "percent": 10})
        second = supervisor.publish_state("agent", "progress", {"phase": "build", "percent": 30})
        self.assertEqual((first.revision, second.revision), (1, 2))
        self.assertEqual(registry.get("agent", "progress").value, {"phase": "build", "percent": 30})

    def test_kernel_capabilities_publish_acknowledge_and_message_the_user(self) -> None:
        journal = InboxJournal()
        registry = ObservableStateRegistry()
        self.addCleanup(journal.close)
        self.addCleanup(registry.close)
        supervisor = Supervisor(journal, registry)
        self.addCleanup(supervisor.close)
        supervisor.create_repl("agent")
        supervisor.append_user_message("agent", "Please investigate the runtime.")
        kernel = supervisor.start_agent_kernel("agent")

        result = kernel.evaluate(
            "message = inbox.pending()[0]\n"
            "published = observable.publish('status', {'phase': 'investigating'})\n"
            "acknowledged = inbox.ack(message['id'])\n"
            "sent = user.inbox.add('I have started investigating.')\n"
            "_result = (published, acknowledged, sent)"
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value[0], {"name": "status", "revision": 1, "presenter": "json"})
        self.assertTrue(result.value[1])
        self.assertEqual(result.value[2], {"id": 2, "recipient": "user"})
        self.assertEqual(registry.get("agent", "status").value, {"phase": "investigating"})
        self.assertEqual(journal.pending("agent"), [])
        self.assertEqual(journal.pending("user")[0].text, "I have started investigating.")

    def test_restart_rehydrates_durable_state_and_reports_lost_kernel_locals(self) -> None:
        journal = InboxJournal()
        registry = ObservableStateRegistry()
        self.addCleanup(journal.close)
        self.addCleanup(registry.close)
        supervisor = Supervisor(journal, registry)
        self.addCleanup(supervisor.close)
        supervisor.create_repl("agent")
        kernel = supervisor.start_agent_kernel("agent")

        self.assertEqual(
            kernel.evaluate(
                "scratch = 'ephemeral'\n"
                "observable.publish('progress', {'phase': 'running'})"
            ).status,
            "ok",
        )
        supervisor.append_user_message("agent", "Continue after restart.")

        restored_kernel, report = supervisor.restart_agent_kernel("agent")
        restored = restored_kernel.evaluate("_result = (globals().get('scratch'), inbox.pending())")

        self.assertEqual(report.agent, "agent")
        self.assertEqual(report.restored_inbox_messages, 1)
        self.assertEqual(report.restored_observable_values, 1)
        self.assertTrue(report.lost_ephemeral_kernel_state)
        self.assertEqual(restored.value[0], None)
        self.assertEqual(restored.value[1], [{"id": 1, "sender": "user", "text": "Continue after restart."}])
        self.assertEqual(registry.get("agent", "progress").value, {"phase": "running"})


if __name__ == "__main__":
    unittest.main()
