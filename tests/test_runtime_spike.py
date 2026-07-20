from __future__ import annotations

from pathlib import Path
import json
import tempfile
import threading
import time
import unittest

from agent_repl import InboxJournal, IsolatedExecution, ObservableStateRegistry, SingleAgentSession, Supervisor


class RuntimeSpikeTests(unittest.TestCase):
    def test_agent_can_announce_take_and_wait_for_observable_task_state(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)

        created = session.evaluate("task = tasks.announce('Check deployment')\ntasks.take(task['id'])\n_result = tasks.wait_for(task['id'], 'deployment', 'ready')")
        session.evaluate("observable.publish('deployment', 'ready')")

        self.assertEqual(created.status, "ok")
        self.assertEqual(created.value["state"], "waiting")
        task = session.supervisor.tasks.list("agent")[0]
        self.assertEqual(task.state, "ready")
        self.assertEqual(task.taken_by, "agent")
        self.assertIsNotNone(task.announced_at)
        self.assertIsNotNone(task.taken_at)
        self.assertEqual([event["event"] for event in session.supervisor.tasks.events(task.id)], ["announced", "working", "waiting", "ready"])
        self.assertIn("Task 1 is ready", session.supervisor.journal.pending("agent")[0].text)

    def test_task_errors_create_challenges_and_promote_recurring_trouble(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)

        result = session.evaluate(
            "task = tasks.announce('Repair integration')\n"
            "tasks.take(task['id'])\n"
            "challenge = tasks.challenge(task['id'], 'Provider response is malformed')\n"
            "tasks.report_error(task['id'], 'SyntaxError: invalid syntax')\n"
            "tasks.report_error(task['id'], 'SyntaxError: invalid syntax')\n"
            "_result = tasks.report_error(task['id'], 'SyntaxError: invalid syntax')"
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value["count"], 3)
        self.assertIsNotNone(result.value["trouble_task_id"])
        self.assertEqual([task.title for task in session.supervisor.tasks.list("agent")], [
            "Repair integration", "Challenge: Repair integration", "Trouble: recurring error in task 1",
        ])

    def test_non_collection_loops_have_a_default_budget_and_one_shot_override(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)

        limited = session.evaluate("while True:\n    pass")
        raised = session.evaluate("loop_limit(3)\ncount = 0\nwhile count < 3:\n    count += 1\n_result = count")
        collection = session.evaluate("count = 0\nfor _ in range(1_500):\n    count += 1\n_result = count")

        self.assertEqual(limited.status, "error")
        self.assertIn("LoopLimitExceeded", limited.error)
        self.assertEqual(raised.status, "ok")
        self.assertEqual(raised.value, 3)
        self.assertEqual(collection.status, "ok")
        self.assertEqual(collection.value, 1500)

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

    def test_only_selected_presentable_state_is_shown_by_default(self) -> None:
        registry = ObservableStateRegistry()
        self.addCleanup(registry.close)
        registry.publish("agent", "debug", {"trace": True}, show_by_default=False)
        visible = registry.publish(
            "agent",
            "summary",
            {"phase": "working"},
            label="Current work",
            priority=10,
        )

        self.assertEqual(registry.list("agent", default_only=True), [visible])
        self.assertEqual(visible.label, "Current work")
        self.assertEqual(visible.priority, 10)

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

    def test_agent_can_reply_to_latest_in_one_durable_operation(self) -> None:
        journal = InboxJournal()
        registry = ObservableStateRegistry()
        self.addCleanup(journal.close)
        self.addCleanup(registry.close)
        supervisor = Supervisor(journal, registry)
        self.addCleanup(supervisor.close)
        supervisor.create_repl("agent")
        supervisor.append_user_message("agent", "Say hello.")
        kernel = supervisor.start_agent_kernel("agent")

        result = kernel.evaluate("_result = inbox.reply_to_latest('Hello!')")

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.value)
        self.assertEqual(journal.pending("agent"), [])
        self.assertEqual(journal.pending("user")[0].text, "Hello!")

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
        self.assertEqual(report.restored_observable_values, 2)
        self.assertTrue(report.lost_ephemeral_kernel_state)
        self.assertEqual(restored.value[0], None)
        self.assertEqual(restored.value[1], [{"id": 1, "sender": "user", "text": "Continue after restart."}])
        self.assertEqual(registry.get("agent", "progress").value, {"phase": "running"})

    def test_single_agent_session_exercises_the_user_visible_flow(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)

        message = session.send("Investigate the runtime.")
        self.assertEqual(message.sender, "user")
        self.assertEqual(session.run_demo_turn(), 1)
        self.assertEqual(session.observe()[0].value, {"text": "Investigate the runtime.", "message_id": 1})
        self.assertEqual(session.user_messages()[0].text, "Received your message and recorded it in observable state.")
        self.assertEqual(session.restart().restored_inbox_messages, 0)

    def test_user_repl_can_inspect_presentable_state_and_message_the_agent(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)

        sent = session.user_evaluate("_result = agent.send('Inspect the project structure.')")
        self.assertEqual(sent.status, "ok")
        self.assertEqual(sent.value, {"id": 1, "sender": "user", "text": "Inspect the project structure."})
        self.assertEqual(session.run_demo_turn(), 1)

        inspected = session.user_evaluate(
            "_result = (presentable.list(), presentable['latest_input'], agent.inbox.pending())"
        )
        self.assertEqual(inspected.status, "ok")
        self.assertEqual(inspected.value[0]["latest_input"], {"text": "Inspect the project structure.", "message_id": 1})
        self.assertEqual(inspected.value[0]["runtime"]["status"], "completed")
        self.assertEqual(inspected.value[1], {"text": "Inspect the project structure.", "message_id": 1})
        self.assertEqual(inspected.value[2], [])

    def test_debug_conversation_log_is_append_only_and_user_inspectable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inbox_path = str(Path(directory) / "inbox.sqlite")
            session = SingleAgentSession.open(inbox_path, str(Path(directory) / "state.sqlite"))
            try:
                session.send("Keep the raw conversation for debugging.")
                session.run_demo_turn()
                entries = session.conversation_log()
                from_user_repl = session.user_evaluate("_result = conversation.messages()")
            finally:
                session.close()

            with (Path(directory) / "conversation.jsonl").open(encoding="utf-8") as log_file:
                file_entries = [json.loads(line) for line in log_file]

        self.assertEqual([entry.sender for entry in entries], ["user", "agent"])
        self.assertEqual([entry["sender"] for entry in file_entries], ["user", "agent"])
        self.assertEqual(from_user_repl.value[0]["text"], "Keep the raw conversation for debugging.")

    def test_agent_can_opt_into_later_inbox_event_delivery(self) -> None:
        journal = InboxJournal()
        registry = ObservableStateRegistry()
        self.addCleanup(journal.close)
        self.addCleanup(registry.close)
        supervisor = Supervisor(journal, registry)
        self.addCleanup(supervisor.close)
        supervisor.create_repl("agent")
        kernel = supervisor.start_agent_kernel("agent")
        registration = kernel.evaluate(
            "def handle(message):\n"
            "    observable.publish('event_result', {'text': message['text']})\n"
            "    inbox.ack(message['id'])\n"
            "inbox.on_message(handle)"
        )
        self.assertEqual(registration.status, "ok")

        supervisor.append_user_message("agent", "Please handle this asynchronously.")
        deadline = time.monotonic() + 1
        while registry.get("agent", "event_result") is None and time.monotonic() < deadline:
            time.sleep(0.01)

        self.assertEqual(registry.get("agent", "event_result").value, {"text": "Please handle this asynchronously."})
        self.assertEqual(journal.pending("agent"), [])

    def test_unresponsive_kernel_can_be_recovered_with_durable_messages(self) -> None:
        journal = InboxJournal()
        registry = ObservableStateRegistry()
        self.addCleanup(journal.close)
        self.addCleanup(registry.close)
        supervisor = Supervisor(journal, registry)
        self.addCleanup(supervisor.close)
        supervisor.create_repl("agent")
        kernel = supervisor.start_agent_kernel("agent")

        with self.assertRaises(TimeoutError):
            kernel.evaluate("while True:\n    pass", timeout=0.1)
        self.assertEqual(registry.get("agent", "runtime").value["status"], "unresponsive")
        supervisor.append_user_message("agent", "Resume with a safe approach.")

        recovered, report = supervisor.recover_agent_kernel("agent")
        self.assertTrue(report.forced_termination)
        self.assertEqual(report.restored_inbox_messages, 1)
        self.assertEqual(registry.get("agent", "runtime").value, {"status": "idle"})
        self.assertEqual(
            recovered.evaluate("_result = inbox.pending()").value,
            [{"id": 1, "sender": "user", "text": "Resume with a safe approach."}],
        )


if __name__ == "__main__":
    unittest.main()
