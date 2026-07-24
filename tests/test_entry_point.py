from __future__ import annotations

from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_repl.__main__ import main


class EntryPointTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
