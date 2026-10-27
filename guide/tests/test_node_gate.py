"""Тесты проверки «без node тесты логики страницы не пропускаются молча, если это запрещено».

Логику страницы гоняет `node`. Локально без него тесты пропускаются с пометкой, а в CI
(переменная окружения REQUIRE_NODE=1) отсутствие node — провал: иначе при зелёном CI
сотни проверок страницы тихо не выполнялись бы. Проверяется через настоящий запуск
unittest в дочернем процессе, у которого node нет на пути поиска.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NODE_TESTS = "guide.tests.test_page_logic.ParseBoldTest"
PRICE_TESTS = "guide.tests.test_page_price.SearchItemsTest"


def run_unittest(target, require_node=None, with_node=False):
    """Запускает один класс тестов в дочернем процессе; node на пути поиска есть только при with_node."""
    with tempfile.TemporaryDirectory() as empty_dir:
        env = dict(os.environ)
        env.pop("REQUIRE_NODE", None)
        if require_node is not None:
            env["REQUIRE_NODE"] = require_node
        if not with_node:
            env["PATH"] = empty_dir
        done = subprocess.run(
            [sys.executable, "-m", "unittest", target],
            cwd=str(ROOT), env=env, capture_output=True, timeout=120, check=False,
        )
    return done.returncode, (done.stdout + done.stderr).decode("utf-8", errors="replace")


class WithoutNodeTests(unittest.TestCase):
    def test_by_default_tests_are_skipped_with_a_note(self):
        for target in (NODE_TESTS, PRICE_TESTS):
            with self.subTest(target=target):
                code, out = run_unittest(target)
                self.assertEqual(code, 0, out)
                self.assertIn("skipped", out)

    def test_zero_or_empty_value_does_not_require_node(self):
        for value in ("", "0"):
            with self.subTest(value=value):
                code, out = run_unittest(NODE_TESTS, require_node=value)
                self.assertEqual(code, 0, out)
                self.assertIn("skipped", out)

    def test_require_node_turns_the_skip_into_a_failure(self):
        for target in (NODE_TESTS, PRICE_TESTS):
            with self.subTest(target=target):
                code, out = run_unittest(target, require_node="1")
                self.assertNotEqual(code, 0, out)
                self.assertIn("REQUIRE_NODE", out)
                self.assertNotIn("skipped", out)


@unittest.skipUnless(shutil.which("node"), "node не найден")
class WithNodeTests(unittest.TestCase):
    def test_require_node_with_node_present_runs_the_tests(self):
        for target in (NODE_TESTS, PRICE_TESTS):
            with self.subTest(target=target):
                code, out = run_unittest(target, require_node="1", with_node=True)
                self.assertEqual(code, 0, out)
                self.assertNotIn("skipped", out)


if __name__ == "__main__":
    unittest.main()
