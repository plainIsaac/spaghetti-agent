from __future__ import annotations

from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_repl.__main__ import main


class EntryPointTests(unittest.TestCase):
    class _DummyServer:
        def __init__(self, interrupt: bool = True) -> None:
            self._interrupt = interrupt
            self.closed = False

        def serve_forever(self) -> None:
            if self._interrupt:
                raise KeyboardInterrupt

        def server_close(self) -> None:
            self.closed = True

    class _DummyLocalProjectUI:
        instances: list["EntryPointTests._DummyLocalProjectUI"] = []

        def __init__(self, session, host: str = "127.0.0.1", port: int = 0, on_message=None) -> None:
            self.session = session
            self.host = host
            self.port = port
            self.on_message = on_message
            self.url = f"http://{host}:{port or 8765}"
            self.server = EntryPointTests._DummyServer()
            type(self).instances.append(self)

    class _DummyLocalProjectManagerUI:
        instances: list["EntryPointTests._DummyLocalProjectManagerUI"] = []

        def __init__(self, manager, host: str = "127.0.0.1", port: int = 0) -> None:
            self.manager = manager
            self.host = host
            self.port = port
            self.url = f"http://{host}:{port or 8765}"
            self.server = EntryPointTests._DummyServer()
            type(self).instances.append(self)

    def test_default_presentation_renders_state_only_on_revision_change(self) -> None:
        from agent_repl.__main__ import _render_default_presentation
        from agent_repl import SingleAgentSession

        output = StringIO()
        session = SingleAgentSession.open()
        self.addCleanup(session.close)
        session.supervisor.publish_state("agent", "status", "ready")
        with patch("sys.stdout", output):
            _render_default_presentation(session, set(), {})
            _render_default_presentation(session, set(), {"status": 1})

        self.assertEqual(output.getvalue().count("state> status: ready"), 1)

    def test_model_log_ignores_trailing_repl_result(self) -> None:
        from agent_repl.__main__ import _print_model_log
        from unittest.mock import Mock

        output = StringIO()
        session = Mock()
        session.model_program_log.return_value = [
            {"event": "model_program", "raw_output": "_result = None"},
            {"event": "repl_result", "status": "ok"},
        ]
        with patch("sys.stdout", output):
            _print_model_log(session)

        self.assertEqual(output.getvalue(), "_result = None\n")

    def test_demo_entry_point_renders_default_state_and_agent_reply(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["spaghetti-agent", "--demo", "--data-dir", str(Path(directory))]), patch(
                "builtins.input", side_effect=["Inspect the runtime.", ":quit"]
            ), patch("sys.stdout", output):
                main()

        rendered = output.getvalue()
        self.assertNotIn("Queued for the agent.", rendered)
        self.assertIn("latest_input:", rendered)
        self.assertIn("reply[agent]> Received your message", rendered)

    def test_openai_mode_explains_missing_configuration(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["spaghetti-agent", "--openai", "--data-dir", str(Path(directory))]), patch(
                "builtins.input", side_effect=["Inspect the runtime.", ":quit"]
            ), patch.dict("os.environ", {}, clear=True), patch("sys.stdout", output):
                main()

        self.assertIn("Model setup required: Set OPENAI_API_KEY", output.getvalue())

    def test_ctrl_c_gracefully_shuts_down_terminal_mode(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["spaghetti-agent", "--demo", "--data-dir", str(Path(directory))]), patch(
                "builtins.input", side_effect=KeyboardInterrupt
            ), patch("sys.stdout", output):
                main()

        self.assertIn("Shutting down Spaghetti Agent.", output.getvalue())

    def test_closed_stdin_gracefully_shuts_down_terminal_mode(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["spaghetti-agent", "--demo", "--data-dir", str(Path(directory))]), patch(
                "builtins.input", side_effect=EOFError
            ), patch("sys.stdout", output):
                main()

        self.assertIn("Shutting down Spaghetti Agent.", output.getvalue())

    def test_forced_tui_uses_alternate_screen_and_restores_it(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["spaghetti-agent", "--demo", "--tui", "--data-dir", str(Path(directory))]), patch(
                "builtins.input", side_effect=[":quit"]
            ), patch("sys.stdout", output):
                main()

        rendered = output.getvalue()
        self.assertIn("\x1b[?1049h", rendered)
        self.assertIn("\x1b[?1049l", rendered)

    def test_tui_python_inspection_returns_to_dashboard_without_crashing(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["spaghetti-agent", "--demo", "--tui", "--data-dir", str(Path(directory))]), patch(
                "builtins.input", side_effect=["/python", "presentable.list()", ":back", ":quit"]
            ), patch("sys.stdout", output):
                main()

        self.assertIn("Python inspection mode", output.getvalue())
        self.assertIn("runtime", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_demo_web_mode_starts_ui_and_wires_demo_message_handler(self) -> None:
        output = StringIO()
        self._DummyLocalProjectUI.instances.clear()
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["spaghetti-agent", "--demo", "--web", "--web-port", "9999", "--data-dir", str(Path(directory))]), patch(
                "agent_repl.__main__.LocalProjectUI", self._DummyLocalProjectUI
            ), patch("sys.stdout", output):
                main()

        self.assertIn("Spaghetti Agent web UI: http://127.0.0.1:9999", output.getvalue())
        self.assertEqual(len(self._DummyLocalProjectUI.instances), 1)
        self.assertIsNotNone(self._DummyLocalProjectUI.instances[0].on_message)
        self.assertTrue(self._DummyLocalProjectUI.instances[0].server.closed)

    def test_project_manager_mode_starts_ui_and_shuts_down_cleanly(self) -> None:
        output = StringIO()
        self._DummyLocalProjectManagerUI.instances.clear()
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "sys.argv",
                [
                    "spaghetti-agent",
                    "--project-manager",
                    "--openai",
                    "--web-port",
                    "9998",
                    "--data-dir",
                    str(Path(directory) / "session"),
                    "--projects-dir",
                    str(Path(directory) / "projects"),
                ],
            ), patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=True), patch(
                "agent_repl.__main__.LocalProjectManagerUI", self._DummyLocalProjectManagerUI
            ), patch("sys.stdout", output):
                main()

        self.assertIn("Spaghetti Agent project manager: http://127.0.0.1:9998", output.getvalue())
        self.assertEqual(len(self._DummyLocalProjectManagerUI.instances), 1)
        self.assertTrue(self._DummyLocalProjectManagerUI.instances[0].server.closed)

    def test_default_coordinator_runtime_requires_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["spaghetti-agent", "--data-dir", str(Path(directory))]), self.assertRaises(SystemExit) as error:
                main()

        self.assertEqual(error.exception.code, 2)

    def test_project_manager_requires_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "sys.argv",
                ["spaghetti-agent", "--project-manager", "--projects-dir", str(Path(directory) / "projects"), "--data-dir", str(Path(directory) / "session")],
            ), self.assertRaises(SystemExit) as error:
                main()

        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
