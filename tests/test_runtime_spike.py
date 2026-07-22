from __future__ import annotations

from pathlib import Path
import json
import tempfile
import threading
import time
import unittest

from agent_repl import InboxJournal, IsolatedExecution, ObservableStateRegistry, SingleAgentSession, Supervisor
from agent_repl.workspace import Workspace


class RuntimeSpikeTests(unittest.TestCase):
    def test_managed_workspace_claims_writes_and_rejects_stale_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            try:
                workspace.claim("builder", 1, "app.txt")
                written = workspace.write_text("builder", 1, "app.txt", "first")
                read = workspace.read_text("app.txt")

                self.assertEqual(read["text"], "first")
                self.assertEqual(workspace.changes(1)[0]["revision"], written["revision"])
                with self.assertRaisesRegex(RuntimeError, "expected"):
                    workspace.write_text("builder", 1, "app.txt", "second", "stale")
                with self.assertRaisesRegex(RuntimeError, "claimed"):
                    workspace.claim("reviewer", 2, "app.txt")
            finally:
                workspace.close()

    def test_workspace_branch_isolated_until_submitted_and_merged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(directory)
            try:
                Path(directory, "app.txt").write_text("main", encoding="utf-8")
                workspace.branch("builder", 1)
                workspace.claim("builder", 1, "app.txt")
                workspace.read_text("app.txt", "builder", 1)
                workspace.write_text("builder", 1, "app.txt", "branch")

                self.assertEqual(Path(directory, "app.txt").read_text(encoding="utf-8"), "main")
                self.assertIn("-main", workspace.diff(1)[0]["diff"])
                workspace.submit("builder", 1)
                workspace.merge(1)
                self.assertEqual(Path(directory, "app.txt").read_text(encoding="utf-8"), "branch")
            finally:
                workspace.close()

    def test_completed_task_acknowledges_its_bound_source_message(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        message = session.send("Build the first project file.")
        session.supervisor.set_turn_messages("agent", [message.id])
        try:
            result = session.evaluate("task = tasks.announce('Build file')\ntasks.take(task)\ntasks.complete(task)")
        finally:
            session.supervisor.clear_turn_messages("agent")

        self.assertEqual(result.status, "ok")
        self.assertEqual(session.supervisor.tasks.messages(1), [message.id])
        self.assertEqual(session.supervisor.journal.pending("agent"), [])

    def test_workspace_uses_active_task_without_explicit_task_or_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = SingleAgentSession.open()
            try:
                session.supervisor.workspace.root = Path(directory)
                result = session.evaluate(
                    "task = tasks.announce('Create managed file')\n"
                    "tasks.take(task)\n"
                    "workspace.write_text('app.txt', 'first')\n"
                    "_result = workspace.read_text('app.txt')['text']\n"
                    "tasks.complete(task)"
                )

                self.assertEqual(result.status, "ok")
                self.assertEqual(result.value, "first")
                self.assertEqual((Path(directory) / "app.txt").read_text(encoding="utf-8"), "first")
            finally:
                session.close()

    def test_static_workspace_watcher_runs_fixed_runtime_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = SingleAgentSession.open()
            try:
                session.supervisor.workspace.root = Path(directory)
                session.supervisor.create_repl("coordinator")
                session.supervisor.start_agent_kernel("coordinator")
                session.evaluate("static_agents.start_workspace_watcher(['*.txt'], 'coordinator', 'Review managed change')")
                session.evaluate("task=tasks.announce('Write')\ntasks.take(task)\nworkspace.write_text('note.txt', 'x')")
                self.assertEqual(session.supervisor.journal.pending("coordinator")[0].text, "Review managed change")
            finally:
                session.close()

    def test_static_workspace_watcher_is_durable_deduplicated_and_stoppable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = str(Path(directory) / "workspace.sqlite")
            root = Path(directory) / "project"; root.mkdir()
            workspace = Workspace(root, state)
            try:
                watcher = workspace.add_workspace_watcher(["*.txt"], "coordinator", "Review")
                self.assertEqual(workspace.workspace_watchers()[0]["id"], watcher["id"])
                self.assertTrue(workspace.record_workspace_watcher_delivery(watcher["id"], "note.txt", "revision"))
                self.assertFalse(workspace.record_workspace_watcher_delivery(watcher["id"], "note.txt", "revision"))
                self.assertTrue(workspace.stop_workspace_watcher(watcher["id"]))
                self.assertEqual(workspace.workspace_watchers(), [])
            finally:
                workspace.close()

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

    def test_agent_python_can_pull_durable_context(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)

        session.evaluate("task = tasks.announce('Investigate parser')\ntasks.report_error(task['id'], 'ValueError: bad input')\nobservable.publish('deployment', 'ready')")
        result = session.evaluate("_result = (context.tasks.get(1)['title'], context.errors.search('bad input')[0]['count'], context.observations.get('deployment')['value'])")

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value, ("Investigate parser", 1, "ready"))

    def test_agent_can_manage_scoped_local_context(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)

        result = session.evaluate(
            "context.local.set('approach', {'step': 1}, model_visible=True)\n"
            "_result = context.local.get('approach')"
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value, {"step": 1})
        self.assertEqual(session.supervisor.working_context.active("agent", [("session", "")])[0]["key"], "approach")

        result = session.evaluate(
            "context.local.set('scratch', 'temporary', lifetime='line', scope_id='current')\n"
            "_result = context.local.get('scratch', lifetime='line', scope_id='current')"
        )
        self.assertEqual(result.value, "temporary")
        self.assertIsNone(session.supervisor.working_context.get("agent", "scratch", "line", "current"))

    def test_file_backed_session_creates_its_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_path = Path(directory) / "new" / "nested" / "inbox.sqlite"
            session = SingleAgentSession.open(str(data_path), str(data_path.with_name("observable.sqlite")))
            try:
                self.assertTrue(data_path.exists())
            finally:
                session.close()

    def test_agent_can_announce_conflict_and_message_a_peer(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.supervisor.create_repl("peer")

        result = session.evaluate(
            "conflict = conflicts.announce('src/service.py', 'Competing edits')\n"
            "agents.message('peer', 'Which invariant are you preserving?')\n"
            "_result = (context.agents.list(), context.conflicts.related('src/service.py')[0]['summary'])"
        )

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value, (["agent", "peer"], "Competing edits"))
        self.assertEqual(session.supervisor.journal.pending("peer")[0].text, "Which invariant are you preserving?")

    def test_coordinator_can_delegate_durable_task_to_specialist(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.supervisor.create_repl("builder")
        builder = session.supervisor.start_agent_kernel("builder")

        result = session.evaluate("_result = tasks.delegate('builder', 'Build editor shell', {'files': ['index.html']})")

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value["owner"], "builder")
        self.assertIn("Task 1 assigned", session.supervisor.journal.pending("builder")[0].text)
        self.assertEqual(builder.evaluate("task = tasks.list()[0]\ntasks.take(task)\n_result = tasks.complete(task)").value["state"], "completed")
        self.assertIn("Task 1 completed by builder", session.supervisor.journal.pending("agent")[0].text)

    def test_coordinator_can_pull_its_delegated_tasks(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.supervisor.create_repl("builder")
        session.supervisor.start_agent_kernel("builder")
        session.evaluate("tasks.delegate('builder', 'Build file', {'path': 'app.py'})")
        result = session.evaluate("_result = context.tasks.delegated(active_only=True)")
        self.assertEqual(result.value[0]["title"], "Build file")

    def test_agent_can_spawn_or_be_denied_a_dynamic_subagent(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)

        def spawn(name: str, _role: str) -> None:
            session.supervisor.create_repl(name)
            session.supervisor.start_agent_kernel(name)

        session.supervisor.set_agent_spawner(spawn)
        result = session.evaluate("_result = agents.spawn('reviewer', 'review', 'Review the editor branch')")
        self.assertEqual(result.value["agent"], "reviewer")
        self.assertIn("Task 1 assigned", session.supervisor.journal.pending("reviewer")[0].text)

        retried = session.evaluate("_result = agents.spawn('reviewer', 'review', 'Review the editor branch')")
        self.assertTrue(retried.value["reused"])
        self.assertEqual(retried.value["task_id"], result.value["task_id"])

        malformed = session.evaluate("agents.spawn('bad', 'review', tasks.announce('wrong shape'))")
        self.assertEqual(malformed.status, "error")
        self.assertIn("title string", malformed.error)

        session.supervisor.allow_subagents = False
        denied = session.evaluate("agents.spawn('blocked', 'review', 'This must not start')")
        self.assertEqual(denied.status, "error")
        self.assertIn("disabled", denied.error)

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

    def test_kernel_failure_is_automatically_recorded_on_the_active_task(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)

        session.evaluate("task = tasks.announce('Parse input')\ntasks.take(task['id'])")
        result = session.evaluate("raise ValueError('bad input')")

        self.assertEqual(result.status, "error")
        events = session.supervisor.tasks.events(1)
        self.assertEqual(events[-1]["event"], "error")
        self.assertIn("ValueError: bad input", events[-1]["details"]["error"])

    def test_supervisor_wakes_a_due_task_without_agent_polling(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)

        session.evaluate("task = tasks.announce('Check later')\n_result = tasks.schedule_after(task['id'], 0)")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not session.supervisor.journal.pending("agent"):
            time.sleep(0.02)

        self.assertEqual(session.supervisor.tasks.list("agent")[0].state, "ready")
        self.assertIn("Task 1 is due", session.supervisor.journal.pending("agent")[0].text)

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

    def test_task_references_and_inbox_message_attributes_are_accepted(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("Hello")

        result = session.evaluate("task = tasks.announce('Respond')\ntasks.take(task)\nmessage = inbox.pending()[0]\n_result = (message.id, message.message_id, tasks.complete(task)['state'])")

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.value[0], result.value[1])
        self.assertEqual(result.value[2], "completed")

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
