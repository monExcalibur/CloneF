from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests
from PIL import Image

from clonef.answers import clean_answer
from clonef.config import Config
from clonef.gemini import GeminiClient, GeminiError
from clonef.typing_state import ManualTyper
from clonef.__main__ import doctor


def image_bytes(mode="RGB"):
    output = io.BytesIO()
    Image.new(mode, (100, 80)).save(output, format="PNG")
    return output.getvalue()


def response(status=200, text="===ОТВЕТЫ===\n1:A", data=None):
    result = Mock(status_code=status)
    result.json.return_value = data if data is not None else {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}
    return result


class ConfigTests(unittest.TestCase):
    def test_environment_overrides_file_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('GEMINI_API_KEY="file-key"\nGEMINI_MODELS=model-a,model-a,model-b\n', encoding="utf-8")
            config = Config.load({"GEMINI_API_KEY": "env-key"}, path)
            self.assertEqual(config.api_keys, ("env-key",))
            self.assertEqual(config.models, ("model-a", "model-b"))

    def test_materials_are_independent_of_working_directory(self):
        config = Config.load({}, Path("/nonexistent/clonef-test-env"))
        self.assertTrue(config.material_path("DB").is_absolute())
        self.assertTrue(config.material_path("DB").is_file())
        self.assertEqual(config.material_path("DB").parent.name, "MyAssistant")

    def test_invalid_settings_do_not_echo_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text('GEMINI_API_KEY="secret', encoding="utf-8")
            with self.assertRaises(ValueError) as raised:
                Config.load({}, path)
            self.assertNotIn("secret", str(raised.exception))
        with self.assertRaises(ValueError):
            Config.load({"CLONEF_PASTE_SHORTCUT": "wrong"}, Path("/nonexistent"))

    def test_multiple_keys_and_terminal_shortcut(self):
        config = Config.load({"GEMINI_API_KEYS": " a, b,a ", "CLONEF_PASTE_SHORTCUT": "ctrl+shift+v"}, Path("/nonexistent"))
        self.assertEqual(config.api_keys, ("a", "b"))
        self.assertEqual(config.paste_shortcut, "ctrl+shift+v")

    def test_environment_single_key_overrides_file_multiple_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("GEMINI_API_KEYS=file-one,file-two\n", encoding="utf-8")
            config = Config.load({"GEMINI_API_KEY": "environment-key"}, path)
            self.assertEqual(config.api_keys, ("environment-key",))
            self.assertNotIn("environment-key", repr(config))

    def test_doctor_never_prints_secret_or_calls_api(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(doctor(Config(api_keys=("private-key",))), 0)
        self.assertNotIn("private-key", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["api_keys_configured"], 1)


class GeminiTests(unittest.TestCase):
    def client(self, responses, keys=("dummy-key",), models=("model-a",)):
        session = Mock()
        session.post.side_effect = responses
        sleep = Mock()
        return GeminiClient(Config(api_keys=keys, models=models), session=session, sleep=sleep)

    def test_missing_key_fails_before_image_or_network(self):
        client = self.client([], keys=())
        with self.assertRaisesRegex(GeminiError, "GEMINI_API_KEY"):
            client.ask(b"not-an-image")
        client.session.post.assert_not_called()

    def test_multipart_answer_ignores_thoughts_and_key_is_in_header(self):
        data = {"candidates": [{"content": {"parts": [
            {"text": "internal thought", "thought": True},
            {"text": "public class Main {\n"},
            {"text": "public static void main(String[] args) {}\n}"},
        ]}, "finishReason": "STOP"}]}
        client = self.client([response(data=data)])
        answer = client.ask(image_bytes("RGBA"))
        self.assertIn("main(String[] args)", answer)
        self.assertNotIn("internal thought", answer)
        url = client.session.post.call_args.args[0]
        self.assertNotIn("dummy-key", url)
        self.assertEqual(client.session.post.call_args.kwargs["headers"], {"x-goog-api-key": "dummy-key"})

    def test_cache_includes_material_contents_and_type(self):
        client = self.client([response(text="1:A"), response(text="1:B"), response(text="1:C")])
        image = image_bytes()
        self.assertEqual(client.ask(image, "lecture-one", "DB"), "1:A")
        self.assertEqual(client.ask(image, "lecture-one", "DB"), "1:A")
        self.assertEqual(client.ask(image, "lecture-two", "DB"), "1:B")
        self.assertEqual(client.ask(image, "lecture-two", "Java"), "1:C")
        self.assertEqual(client.session.post.call_count, 3)

    def test_404_moves_to_next_model_without_retrying_every_key(self):
        client = self.client([response(404), response()], keys=("one", "two"), models=("old", "new"))
        self.assertEqual(client.ask(image_bytes()), "1:A")
        self.assertEqual(client.session.post.call_count, 2)
        self.assertIn("/models/new:", client.session.post.call_args.args[0])

    def test_rate_limit_retries_with_delay(self):
        client = self.client([response(429), response()])
        self.assertEqual(client.ask(image_bytes()), "1:A")
        client.sleep.assert_called_once_with(1)

    def test_connection_failures_preserve_cause(self):
        cause = requests.Timeout("connection timeout")
        client = self.client([cause, cause])
        with self.assertRaises(GeminiError) as raised:
            client.ask(image_bytes())
        self.assertIs(raised.exception.__cause__, cause)
        self.assertIn("Timeout", str(raised.exception))

    def test_bad_key_uses_next_key_and_error_is_redacted(self):
        client = self.client([response(403), response()], keys=("bad", "good"))
        self.assertEqual(client.ask(image_bytes()), "1:A")
        self.assertEqual(client.session.post.call_args.kwargs["headers"]["x-goog-api-key"], "good")
        client = self.client([response(403, data={"error": {"message": "invalid dummy-key"}})])
        with self.assertRaises(GeminiError) as raised:
            client.ask(image_bytes())
        self.assertIn("HTTP 403", str(raised.exception))
        self.assertNotIn("dummy-key", str(raised.exception))

    def test_empty_and_truncated_answers_are_not_cached_as_success(self):
        for data in (
            {"promptFeedback": {"blockReason": "SAFETY"}},
            {"candidates": [{"content": {"parts": [{"text": "partial"}]}, "finishReason": "MAX_TOKENS"}]},
            {"candidates": {"bad": "shape"}},
        ):
            with self.subTest(data=data):
                client = self.client([response(data=data)])
                with self.assertRaises(GeminiError):
                    client.ask(image_bytes())
                self.assertEqual(client._cache, {})

    def test_code_and_test_answer_parsing(self):
        self.assertEqual(clean_answer("```\n===ОТВЕТЫ===\n1 : TRUE\n2:FALSE\n```"), "1:T\n2:F")
        self.assertEqual(clean_answer('```python\ndef main():\n    print("Привет")\n```'), 'def main():\n    print("Привет")')


class TypingTests(unittest.TestCase):
    def test_pause_does_not_skip_unsent_characters(self):
        typer = ManualTyper()
        typer.load("abcd")
        typer.resume()
        typer.request_step()
        typer.request_step()
        old_step = typer.next_step()
        typer.pause()
        self.assertFalse(typer.accepts(old_step))
        self.assertEqual(typer.index, 0)
        typer.resume()
        typer.request_step()
        step = typer.next_step()
        self.assertEqual(step.character, "a")
        typer.complete(step)
        self.assertEqual(typer.index, 1)

    def test_new_buffer_invalidates_pending_old_steps(self):
        typer = ManualTyper()
        typer.load("OLD")
        typer.resume()
        typer.request_step()
        old = typer.next_step()
        typer.load("NEW")
        self.assertFalse(typer.accepts(old))
        self.assertIsNone(typer.next_step())
        typer.resume()
        typer.request_step()
        self.assertEqual(typer.next_step().character, "N")

    def test_commit_follows_send_and_end_stops(self):
        typer = ManualTyper()
        typer.load("x")
        typer.resume()
        for _ in range(10):
            typer.request_step()
        self.assertEqual(typer.pending, 1)
        step = typer.next_step()
        self.assertEqual(typer.index, 0)
        typer.complete(step)
        self.assertEqual(typer.index, 1)
        self.assertFalse(typer.active)
        self.assertFalse(typer.resume())

    def test_unicode_and_line_endings(self):
        typer = ManualTyper()
        typer.load("Я😀\r\n\t")
        typer.resume()
        output = []
        while typer.active:
            typer.request_step()
            step = typer.next_step()
            output.append(step.character)
            typer.complete(step)
        self.assertEqual("".join(output), "Я😀\n\t")
