from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from time import sleep

from agent_repl import FallbackAgentDriver, GeminiAgentDriver, OpenAIAgentDriver, OpenRouterAgentDriver, SingleAgentSession
from agent_repl.token_budget import TokenBudget, TokenBudgetExceeded
from agent_repl.session import ModelTurnWorker, _NOT_READY
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


class FailingClient:
    def __init__(self) -> None:
        self.responses = SimpleNamespace(create=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("rate limit")))


class UsageClient:
    def __init__(self, output_text: str) -> None:
        self.responses = SimpleNamespace(create=lambda **kwargs: SimpleNamespace(
            output_text=output_text,
            usage=SimpleNamespace(input_tokens=12, output_tokens=7, total_tokens=19),
        ))


class FakeChatClient:
    def __init__(self, output_text: str) -> None:
        self.calls: list[dict] = []
        def create(**kwargs):
            self.calls.append(kwargs)
            return iter([SimpleNamespace(model="gemini-test", choices=[SimpleNamespace(delta=SimpleNamespace(content=output_text))])])
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))


class SequenceResponses:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.outputs[len(self.calls) - 1])


class SequenceClient:
    def __init__(self, outputs: list[str]) -> None:
        self.responses = SequenceResponses(outputs)


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


class SlowResponse:
    def __iter__(self):
        sleep(0.05)
        raise RuntimeError("stream interrupted after client close")


class ClosableSlowClient:
    def __init__(self) -> None:
        self.responses = SimpleNamespace(create=lambda **kwargs: SlowResponse())
        self.closed = False

    def close(self) -> None:
        self.closed = True


class DelayedStreamResponse:
    def __iter__(self):
        yield SimpleNamespace(type="response.output_text.delta", delta="first")
        sleep(0.03)
        yield SimpleNamespace(type="response.output_text.delta", delta=" second")


class DelayedStreamClient:
    def __init__(self) -> None:
        self.responses = SimpleNamespace(create=lambda **kwargs: DelayedStreamResponse())


class OpenAIDriverTests(unittest.TestCase):
    def test_default_model_is_the_cost_sensitive_experiment_tier(self) -> None:
        self.assertEqual(DEFAULT_OPENAI_MODEL, "gpt-5.6-luna")
        self.assertEqual(DEFAULT_REQUEST_TIMEOUT_SECONDS, 30.0)

    def test_openrouter_uses_the_free_router_by_default(self) -> None:
        client = FakeClient("_result = None")
        driver = OpenRouterAgentDriver(client=client)
        driver.plan([], {})
        self.assertEqual(DEFAULT_OPENROUTER_MODEL, "nvidia/nemotron-3-super-120b-a12b:free")
        self.assertEqual(client.responses.calls[0]["model"], "nvidia/nemotron-3-super-120b-a12b:free")

    def test_plan_uses_configured_model_when_stream_does_not_report_resolution(self) -> None:
        planned = OpenAIAgentDriver(model="test-model", client=FakeClient("_result = None")).plan([], {})

        self.assertEqual(planned.resolved_model, "test-model")

    def test_gemini_uses_chat_completions_transport(self) -> None:
        client = FakeChatClient("inbox.ack(inbox.pending()[0]['id'])")
        planned = GeminiAgentDriver(model="gemini-test", client=client).plan([], None)

        self.assertEqual(planned.resolved_model, "gemini-test")
        self.assertEqual(client.calls[0]["model"], "gemini-test")
        self.assertTrue(client.calls[0]["stream"])

    def test_fallback_uses_the_next_provider_and_records_the_failure(self) -> None:
        primary = OpenRouterAgentDriver(model="first", client=FailingClient())
        fallback = OpenAIAgentDriver(model="second", client=FakeClient("_result = None"))
        driver = FallbackAgentDriver([primary, fallback])

        planned = driver.plan([], None)

        self.assertEqual(planned.resolved_model, "second")
        self.assertEqual(driver.provider_name, "OpenAI")
        self.assertEqual(driver.last_failures[0]["provider"], "OpenRouter")

    def test_http_trace_records_stream_open_and_close_times(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "http.jsonl"
            driver = OpenAIAgentDriver(client=FakeClient("_result = None"))
            driver.set_http_log_path(str(path))
            driver.plan([], None)
            entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual([entry["event"] for entry in entries], ["stream_opened", "stream_closed"])
        self.assertIn("opened_at", entries[1])
        self.assertIn("closed_at", entries[1])
        self.assertIn("duration_seconds", entries[1])

    def test_stream_idle_timeout_normalizes_socket_race_to_timeout_error(self) -> None:
        client = ClosableSlowClient()
        driver = OpenAIAgentDriver(client=client, request_timeout=0.01)

        with self.assertRaisesRegex(TimeoutError, "provider stream was idle"):
            driver.plan([], None)

        self.assertTrue(client.closed)

    def test_stream_idle_timeout_resets_after_each_token(self) -> None:
        driver = OpenAIAgentDriver(client=DelayedStreamClient(), request_timeout=0.04)

        planned = driver.plan([], None)

        self.assertEqual(planned.source, "first second")

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
        self.assertIn("result", {value.name for value in session.observe()})
        self.assertEqual(session.user_messages()[0].text, "Handled your request.")
        request = client.responses.calls[0]
        self.assertEqual(request["model"], "test-model")
        self.assertIn("\"message_id\"", request["input"])
        self.assertIn("Please assess this design.", request["input"])

    def test_activation_includes_a_bounded_recent_conversation_window(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("Build a writing tool with a research pane.")
        session.evaluate("inbox.reply_to_latest('I will start with the editor.')")
        session.send("Add version history too.")
        client = FakeClient("inbox.reply_to_latest('Working on it.')")

        result = session.run_openai_turn(OpenAIAgentDriver(model="test-model", client=client))

        self.assertEqual(result.status, "ok")
        activation = json.loads(client.responses.calls[0]["input"])["activation"][0]
        self.assertEqual([entry["text"] for entry in activation["context_window"]], [
            "Build a writing tool with a research pane.",
            "I will start with the editor.",
            "Add version history too.",
        ])
        self.assertEqual(activation["pending_work"][0]["text"], "Add version history too.")

    def test_default_context_window_can_be_disabled(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("Earlier project requirement.")
        session.evaluate("inbox.ack(inbox.pending()[0]['id'])")
        session.send("Current request.")
        client = FakeClient("inbox.ack(inbox.pending()[0]['id'])")

        result = session.run_openai_turn(
            OpenAIAgentDriver(model="test-model", client=client), default_context_window=False,
        )

        self.assertEqual(result.status, "ok")
        activation = json.loads(client.responses.calls[0]["input"])["activation"][0]
        self.assertEqual(activation["text"], "Current request.")
        self.assertNotIn("context_window", activation)
        self.assertNotIn("pending_work", activation)
        self.assertNotIn("active_tasks", activation)

    def test_driver_includes_only_opted_in_local_context_for_active_message(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        message = session.send("Use my preferred format.")
        session.evaluate(
            f"context.local.set('format', 'brief', lifetime='message', scope_id='{message.id}', model_visible=True)"
        )
        client = FakeClient("inbox.ack(inbox.pending()[0]['id'])")

        result = session.run_openai_turn(OpenAIAgentDriver(model="test-model", client=client))

        self.assertEqual(result.status, "ok")
        request = json.loads(client.responses.calls[0]["input"])
        self.assertEqual(request["activation"][0]["working_context"][0]["value"], "brief")

    def test_empty_model_program_is_presented_without_losing_the_message(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("Please handle this later.")

        result = session.run_openai_turn(OpenAIAgentDriver(client=FakeClient("")))

        self.assertEqual(result.status, "error")
        self.assertIn("empty agent program", result.error)
        self.assertIn("model_error", {value.name for value in session.observe()})
        self.assertEqual(session.supervisor.journal.pending("agent")[0].text, "Please handle this later.")

    def test_token_budget_blocks_provider_call_before_planning(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.supervisor.token_budget.set_limit(10)
        session.send("Please handle this later.")
        client = FakeClient("inbox.ack(inbox.pending()[0]['id'])")

        result = session.run_openai_turn(OpenAIAgentDriver(client=client))

        self.assertEqual(result.status, "error")
        self.assertIn("token budget exhausted", result.error)
        self.assertEqual(client.responses.calls, [])
        budget = session.supervisor.token_budget.snapshot()
        self.assertEqual(budget["status"], "available")
        self.assertEqual(budget["used_tokens"], 0)

    def test_provider_usage_is_used_when_available(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("Handle this.")

        result = session.run_openai_turn(OpenAIAgentDriver(client=UsageClient("inbox.ack(inbox.pending()[0]['id'])")))

        self.assertEqual(result.status, "ok")
        self.assertEqual(session.supervisor.token_budget.snapshot()["used_tokens"], 19)
        budget_state = session.supervisor.observable_state.get("agent", "token_budget").value
        self.assertEqual(budget_state["last_turn_accounting"], "provider_reported")


class TokenBudgetTests(unittest.TestCase):
    def test_reservations_prevent_parallel_overspend_and_settle_to_actual_usage(self) -> None:
        budget = TokenBudget(limit_tokens=100)
        self.addCleanup(budget.close)
        reservation = budget.reserve(20, 70)
        with self.assertRaises(TokenBudgetExceeded):
            budget.reserve(20, 10)
        budget.settle(reservation, 35)

        self.assertEqual(budget.snapshot()["used_tokens"], 35)
        self.assertEqual(budget.snapshot()["remaining_tokens"], 65)

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

    def test_importing_a_runtime_global_is_rejected_before_kernel_evaluation(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("Please handle this later.")

        result = session.run_openai_turn(OpenAIAgentDriver(client=FakeClient("import inbox")))

        self.assertEqual(result.status, "error")
        self.assertIn("injected globals", result.error)

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

    def test_successful_program_clears_prior_model_feedback(self) -> None:
        client = FakeClient("User Safety: safe")
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("Please handle this later.")
        session.run_openai_turn(OpenAIAgentDriver(client=client))
        client.responses.output_text = "inbox.ack(inbox.pending()[0]['id'])"
        session.run_openai_turn(OpenAIAgentDriver(client=client))
        session.send("One more thing.")
        session.run_openai_turn(OpenAIAgentDriver(client=client))

        request = __import__("json").loads(client.responses.calls[2]["input"])
        self.assertIsNone(request["model_feedback"])

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
        self.assertIn('"message_id": 2', request)
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

    def test_worker_retries_one_failed_evaluation_with_error_feedback(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.send("Handle this.")
        client = SequenceClient([
            "raise RuntimeError('first attempt failed')",
            "inbox.reply_to_latest('Recovered after retry.')",
        ])
        worker = ModelTurnWorker(session, OpenAIAgentDriver(client=client))
        self.addCleanup(worker.close)

        self.assertTrue(worker.request_turn())
        deadline = __import__("time").monotonic() + 2
        result = None
        while __import__("time").monotonic() < deadline:
            collected = worker.collect()
            if collected is not _NOT_READY:
                result = collected
                break
            sleep(0.01)

        self.assertEqual(result.status, "ok")
        self.assertEqual(len(client.responses.calls), 2)
        retry_input = json.loads(client.responses.calls[1]["input"])
        self.assertIn("first attempt failed", retry_input["model_feedback"]["error"])
        self.assertEqual(session.user_messages()[0].text, "Recovered after retry.")

    def test_activation_includes_active_task_details(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.evaluate("task = tasks.announce('Implement file', {'path': 'app.py', 'content': 'print(1)'})\ntasks.take(task)")
        session.send("Continue.")
        client = FakeClient("inbox.ack(inbox.pending()[-1]['id'])")
        session.run_openai_turn(OpenAIAgentDriver(client=client))
        activation = json.loads(client.responses.calls[0]["input"])["activation"][0]
        self.assertEqual(activation["active_tasks"][0]["details"]["path"], "app.py")

    def test_task_wakeup_dispatches_a_model_turn_without_user_input(self) -> None:
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        worker = ModelTurnWorker(session, OpenAIAgentDriver(client=FakeClient("inbox.ack(inbox.pending()[-1]['id'])")))
        self.addCleanup(worker.close)
        session.evaluate("task = tasks.announce('Check later')\ntasks.schedule_after(task['id'], 0)")
        import time
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and session.supervisor.journal.pending("agent"):
            time.sleep(0.02)

        self.assertEqual(session.supervisor.journal.pending("agent"), [])


if __name__ == "__main__":
    unittest.main()
