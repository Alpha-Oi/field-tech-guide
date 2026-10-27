"""Общий доступ к `node` для тестов логики страницы (test_page_logic, test_page_price).

Логику страницы гоняет `node`. Если его нет:

* локально — тесты пропускаются с пометкой;
* при заданной переменной окружения REQUIRE_NODE (любое значение, кроме пустого и «0») —
  тесты падают с понятным сообщением. В CI она включена, чтобы пропавший node не давал
  зелёный результат при непроверенной странице.
"""

import functools
import os
import shutil
import unittest

NODE = shutil.which("node")

REQUIRE_NODE = os.environ.get("REQUIRE_NODE", "").strip().lower() not in ("", "0", "false", "no")

SKIP_REASON = "node не найден: проверка логики страницы пропущена"
FAIL_MESSAGE = "node не найден, а REQUIRE_NODE задана: проверка логики страницы обязательна (поставь node)"


def _need_node(target):
    """Декоратор класса или метода теста: без node — пропуск, а при REQUIRE_NODE — провал."""
    if NODE:
        return target
    if not REQUIRE_NODE:
        return unittest.skip(SKIP_REASON)(target)
    if isinstance(target, type):
        @classmethod
        def setUpClass(cls):
            raise AssertionError(FAIL_MESSAGE)

        target.setUpClass = setUpClass
        return target

    @functools.wraps(target)
    def failing(*args, **kwargs):
        raise AssertionError(FAIL_MESSAGE)

    return failing


NEED_NODE = _need_node
