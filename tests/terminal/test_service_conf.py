"""``service.conf``: the settings a person edits, including the optional ``engine=`` line that lets one folder of model
configs serve two PCs whose engines differ (a packaged engine on one, a hand build on the other)."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("strata_service_conf", ROOT / "Scripts" / "Strata_Service.py")
service = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(service)


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


if __name__ == "__main__":
    unittest.main()
