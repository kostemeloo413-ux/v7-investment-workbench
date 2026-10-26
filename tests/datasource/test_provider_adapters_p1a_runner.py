"""Stdlib unittest bridge for the existing assertion-style P1A adapter tests."""

import unittest

from tests.datasource import test_provider_adapters as p1a_tests


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for name, value in sorted(vars(p1a_tests).items()):
        if name.startswith("test_") and callable(value):
            suite.addTest(unittest.FunctionTestCase(value, description=f"P1A::{name}"))
    return suite
