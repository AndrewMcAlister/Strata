"""``service.conf``: the settings a person edits, including the optional ``engine=`` line that lets one folder of model
configs serve two PCs whose engines differ (a packaged engine on one, a hand build on the other).

The last two classes cover the model switch a shortcut asks for: the service is stopped, the choice is written, it
is started again, and only a model that has answered a chat request counts as serving.
"""

import contextlib
import http.server
import importlib.util
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("strata_service_conf", ROOT / "Scripts" / "Strata_Service.py")
service = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(service)

READY = json.dumps({"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "length"}],
                    "usage": {"completion_tokens": 1}})
BROKEN = json.dumps({"error": {"message": "lending the prompt path its slots failed: prefill: relayout: the stream failed"}})


class ChatServer:
    """A stand-in for the server's ``/v1/chat/completions``: one answer per request, the last one repeats."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                raw = b'{"status": "ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append((self.path, self.rfile.read(length)))
                status, body = outer.answers[min(len(outer.requests) - 1, len(outer.answers) - 1)]
                raw = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class ServiceConf(unittest.TestCase):

    def test_the_model_and_the_port_are_read(self):
        conf = service.conf_values("config=strata-iq2_xs-20k.json\nport=8090\n")
        self.assertTrue(conf["config"].endswith("strata-iq2_xs-20k.json"))
        self.assertEqual(conf["port"], 8090)
        self.assertIsNone(conf["engine"])
        self.assertEqual(conf["unknown"], [])

    def test_engine_may_name_the_engine_installed_for_this_pc(self):
        conf = service.conf_values("config=strata-iq2_xs-20k.json\nengine=component:strata-engine\n")
        self.assertEqual(conf["engine"], "component:strata-engine")
        self.assertEqual(conf["unknown"], [])

    def test_engine_may_name_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "strata.exe"
            engine.write_bytes(b"MZ")
            conf = service.conf_values(f"config=strata-iq2_xs-20k.json\nengine={engine}\n")
        self.assertEqual(conf["engine"], str(engine))

    def test_a_missing_engine_file_is_one_plain_sentence(self):
        with self.assertRaises(service.ServiceError) as caught:
            service.conf_values("config=strata-iq2_xs-20k.json\nengine=D:\\nowhere\\strata.exe\n")
        self.assertIn("is neither a file nor a component reference", str(caught.exception))
        self.assertIn("component:strata-engine", str(caught.exception))

    def test_the_written_file_names_engine_as_a_commented_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "service.conf"
            service.write_conf(path, Path("D:/Strata/strata-iq2_xs-20k.json"), [])
            text = path.read_text(encoding="utf-8")
        self.assertIn("#engine=component:strata-engine", text)
        self.assertNotIn("\nengine=", text)               # a fresh install serves what its config names


class ModelAnswers(unittest.TestCase):
    """The readiness question the switch asks: one chat request, not just ``/health``."""

    def _config(self, folder: str, port: int | None = None) -> Path:
        data = {"exe": "component:strata-engine", "args": [], "model_name": "qwen3.8-flash-next"}
        if port is not None:
            data["port"] = port
        path = Path(folder) / "strata-test.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_the_port_comes_from_the_conf_then_the_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp, port=8091)
            self.assertEqual(service._endpoint({"config": str(config)}), ("127.0.0.1", 8091))
            self.assertEqual(service._endpoint({"config": str(config), "port": 8090, "host": "0.0.0.0"}),
                             ("127.0.0.1", 8090))

    def test_a_model_that_answers_one_token_is_ready(self):
        with tempfile.TemporaryDirectory() as tmp, ChatServer([(200, READY)]) as server:
            config = self._config(tmp)
            answered, detail = service.wait_until_the_model_answers(
                {"host": "127.0.0.1", "port": server.port, "config": str(config)}, 5)
        self.assertTrue(answered, detail)
        self.assertEqual(detail, "")
        self.assertEqual(server.requests[0][0], "/v1/chat/completions")
        self.assertEqual(json.loads(server.requests[0][1])["max_tokens"], 1)

    def test_a_model_that_fails_its_first_prompt_ends_the_wait_at_once(self):
        # the engine fault this machine hit on 2026-11-24: the weights load, /health is fine,
        # and the first prompt comes back 400 from the engine
        with tempfile.TemporaryDirectory() as tmp, ChatServer([(400, BROKEN)]) as server:
            config = self._config(tmp)
            started = time.monotonic()
            answered, detail = service.wait_until_the_model_answers(
                {"host": "127.0.0.1", "port": server.port, "config": str(config)}, 30)
            elapsed = time.monotonic() - started
        self.assertFalse(answered)
        self.assertIn("prefill: relayout: the stream failed", detail)
        self.assertLess(elapsed, 10)                       # the whole 30 s timeout was not sat out

    def test_a_busy_server_is_asked_again(self):
        with tempfile.TemporaryDirectory() as tmp, ChatServer([(503, '{"error": "busy"}'), (200, READY)]) as server:
            config = self._config(tmp)
            answered, detail = service.wait_until_the_model_answers(
                {"host": "127.0.0.1", "port": server.port, "config": str(config)}, 15)
        self.assertTrue(answered, detail)
        self.assertEqual(len(server.requests), 2)

    def test_a_keyed_server_is_not_called_broken(self):
        with tempfile.TemporaryDirectory() as tmp, ChatServer([(401, '{"error": {"message": "unauthorized"}}')]) as server:
            config = self._config(tmp)
            answered, detail = service.wait_until_the_model_answers(
                {"host": "127.0.0.1", "port": server.port, "config": str(config)}, 1)
        self.assertFalse(answered)
        self.assertIn("could not sign in", detail)

    def test_nothing_listening_is_a_timeout_and_says_so(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]                  # bound, then closed: nothing is there
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            answered, detail = service.wait_until_the_model_answers(
                {"host": "127.0.0.1", "port": port, "config": str(config)}, 1)
        self.assertFalse(answered)
        self.assertIn("no answer within 1 seconds", detail)


class ServeAction(unittest.TestCase):
    """``serve``: stop the service, write the choice, start it again, wait for that model to answer."""

    def _run(self, tmp: str, port: int):
        config = Path(tmp) / "strata-test.json"
        config.write_text(json.dumps({"exe": "component:strata-engine", "args": [],
                                      "model_name": "qwen3.8-flash-next"}), encoding="utf-8")
        args = service.parser().parse_args(["serve", "--config", str(config), "--start-wait", "10"])
        patches = (
            mock.patch.object(service, "require_windows"),
            mock.patch.object(service, "service_status",
                              return_value={"installed": True, "state": "running", "pid": 1}),
            mock.patch.object(service, "set_conf_config"),
            mock.patch.object(service, "grant_user_write"),
            mock.patch.object(service, "command_restart", return_value=0),
            mock.patch.object(service, "load_conf",
                              return_value={"host": "127.0.0.1", "port": port, "config": str(config)}),
            mock.patch.object(service, "LOG_DIR", Path(tmp) / "logs"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = service.command_serve(args)
        return code, out.getvalue(), err.getvalue()

    def test_serve_succeeds_only_after_the_model_answers(self):
        with tempfile.TemporaryDirectory() as tmp, ChatServer([(200, READY)]) as server:
            code, out, err = self._run(tmp, server.port)
        self.assertEqual(code, 0, err)
        self.assertIn("is serving", out)
        self.assertEqual(err, "")

    def test_serve_fails_with_the_reason_and_the_log_end(self):
        with tempfile.TemporaryDirectory() as tmp, ChatServer([(400, BROKEN)]) as server:
            logs = Path(tmp) / "logs"
            logs.mkdir()
            (logs / "service-2026-11-24.log").write_text(
                "2026-11-24 01:14:58 [Strata] server state is failed\n", encoding="utf-8")
            code, _out, err = self._run(tmp, server.port)
        self.assertEqual(code, 6)
        self.assertIn("the chosen model is not serving", err)
        self.assertIn("prefill: relayout: the stream failed", err)
        self.assertIn("service-2026-11-24.log", err)


if __name__ == "__main__":
    unittest.main()
