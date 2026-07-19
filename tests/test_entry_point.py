from __future__ import annotations

from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_repl.__main__ import main


class EntryPointTests(unittest.TestCase):
    def test_demo_entry_point_renders_default_state_and_agent_reply(self) -> None:
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["agent-repl", "--demo", "--data-dir", str(Path(directory))]), patch(
                "builtins.input", side_effect=["Inspect the runtime.", ":quit"]
            ), patch("sys.stdout", output):
                main()

        rendered = output.getvalue()
        self.assertIn("Queued for the agent.", rendered)
        self.assertIn("latest_input:", rendered)
        self.assertIn("agent> Received your message", rendered)


if __name__ == "__main__":
    unittest.main()
