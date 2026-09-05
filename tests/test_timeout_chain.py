import importlib
import os
import unittest
from contextlib import contextmanager

from caty_gateway import caty_gateway as cg


@contextmanager
def patched_environment(**values):
    original = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class TimeoutChainTest(unittest.TestCase):
    def test_gateway_default_timeout_order(self):
        try:
            with patched_environment(CATY_PTT_BRAIN_TIMEOUT=None, CATY_PTT_JOB_TTL=None):
                gateway = importlib.reload(cg)
                self.assertEqual(gateway.CATY_PTT_BRAIN_TIMEOUT, 1800)
                self.assertEqual(gateway.CATY_PTT_JOB_TTL, 2100)
                self.assertLess(gateway.CATY_PTT_BRAIN_TIMEOUT + 60, gateway.CATY_PTT_JOB_TTL)
        finally:
            importlib.reload(cg)

    def test_timeout_environment_overrides(self):
        try:
            with patched_environment(CATY_PTT_BRAIN_TIMEOUT="12", CATY_PTT_JOB_TTL="34"):
                gateway = importlib.reload(cg)
                self.assertEqual(gateway.CATY_PTT_BRAIN_TIMEOUT, 12)
                self.assertEqual(gateway.CATY_PTT_JOB_TTL, 34)
        finally:
            importlib.reload(cg)
