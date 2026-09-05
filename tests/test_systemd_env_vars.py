from importlib import resources
import unittest


class ServiceTemplateContractTest(unittest.TestCase):
    def template(self, name):
        return resources.files("caty_gateway").joinpath("templates", name).read_text(encoding="utf-8")

    def test_launchd_uses_public_label(self):
        text = self.template("launchd.plist")
        self.assertIn("ai.caty.gateway.__MEMBER_ID__", text)
        self.assertNotIn("ai." + "openclaw", text)

    def test_systemd_runs_installed_module(self):
        text = self.template("systemd.service")
        self.assertIn("python", text.lower())
        self.assertIn("caty_gateway.caty_gateway", text)
