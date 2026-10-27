"""Тесты рабочих процессов GitHub Actions (`.github/workflows/`): права, порядок и node в CI.

YAML разбирается построчно, без PyYAML (зависимостей нет): файлы простые, ключи верхнего уровня
стоят без отступа, задачи — с отступом в два пробела.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

# Одна команда проверки из interfaces.md; CI и деплой обязаны запускать именно её.
# Последний шаг — актуальность графика docs/img/steps-by-stage.svg.
CHECK_COMMAND = (
    "python -m unittest discover -s guide/tests -t . "
    "&& python guide/build.py --check --lenient-vars "
    "&& python tools/check_public.py "
    "&& python tools/make_charts.py --check"
)


def read(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def top_block(text, key):
    """Строки блока верхнего уровня `key:` (без заголовка), пустые и комментарии отброшены."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.fullmatch(re.escape(key) + r":\s*", line):
            block = []
            for follow in lines[index + 1:]:
                if follow.strip() and not follow.startswith((" ", "#")):
                    break
                if follow.strip() and not follow.lstrip().startswith("#"):
                    block.append(follow)
            return block
    return []


def jobs(text):
    """{имя задачи: текст блока} из раздела jobs."""
    result, current = {}, None
    for line in top_block(text, "jobs"):
        header = re.fullmatch(r"  ([A-Za-z0-9_-]+):\s*", line)
        if header:
            current = header.group(1)
            result[current] = []
        elif current:
            result[current].append(line)
    return {name: "\n".join(lines) for name, lines in result.items()}


def needs_of(block):
    match = re.search(r"(?m)^    needs:\s*(.+)$", block)
    if not match:
        return []
    return re.findall(r"[A-Za-z0-9_-]+", match.group(1).strip("[]"))


def reachable_needs(all_jobs, name):
    seen, todo = set(), list(needs_of(all_jobs[name]))
    while todo:
        job = todo.pop()
        if job not in seen:
            seen.add(job)
            todo.extend(needs_of(all_jobs[job]))
    return seen


def normalised(text):
    return " ".join(text.split())


class CiWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = read("ci.yml")
        self.jobs = jobs(self.text)

    def test_node_is_installed_with_a_pinned_action_and_a_fixed_version(self):
        self.assertRegex(self.text, r"uses:\s*actions/setup-node@v\d+\.\d+\.\d+\s")
        self.assertRegex(self.text, r"node-version:\s*[\"']?\d+(\.\d+)*[\"']?\s")

    def test_missing_node_fails_the_job_instead_of_skipping_page_tests(self):
        block = self.jobs["check"]
        self.assertRegex(block, r"REQUIRE_NODE:\s*[\"']?1[\"']?\s")

    def test_node_is_set_up_before_the_check_command_runs(self):
        block = self.jobs["check"]
        self.assertLess(block.index("actions/setup-node@"), block.index("python -m unittest"))

    def test_check_command_is_still_the_agreed_one(self):
        self.assertIn(CHECK_COMMAND, normalised(self.text))


class PagesWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.text = read("pages.yml")
        self.jobs = jobs(self.text)

    def test_workflow_level_permissions_are_read_only(self):
        permissions = [line.strip() for line in top_block(self.text, "permissions")]
        self.assertEqual(permissions, ["contents: read"])

    def test_page_and_token_write_rights_belong_to_the_deploy_job_only(self):
        for right in ("pages: write", "id-token: write"):
            with self.subTest(right=right):
                holders = [name for name, block in self.jobs.items() if right in block]
                self.assertEqual(holders, ["deploy"])

    def test_deploy_job_has_its_own_permissions_block(self):
        block = self.jobs["deploy"]
        self.assertRegex(block, r"(?m)^    permissions:\s*$")
        self.assertRegex(block, r"(?m)^      pages: write\s*$")
        self.assertRegex(block, r"(?m)^      id-token: write\s*$")

    def test_other_jobs_get_no_write_rights(self):
        for name, block in self.jobs.items():
            if name != "deploy":
                with self.subTest(job=name):
                    self.assertNotRegex(block, r"(?m):\s*write\s*$")

    def test_deploy_waits_for_a_job_with_the_full_check_command(self):
        checking = [name for name, block in self.jobs.items() if CHECK_COMMAND in normalised(block)]
        self.assertTrue(checking, "в pages.yml нет задачи с полной командой проверки")
        self.assertTrue(set(checking) & reachable_needs(self.jobs, "deploy"), checking)

    def test_the_checking_job_has_node_and_requires_it(self):
        checking = [name for name, block in self.jobs.items() if CHECK_COMMAND in normalised(block)]
        for name in checking:
            with self.subTest(job=name):
                block = self.jobs[name]
                self.assertRegex(block, r"uses:\s*actions/setup-node@v\d+\.\d+\.\d+\s")
                self.assertRegex(block, r"REQUIRE_NODE:\s*[\"']?1[\"']?\s")

    def test_the_site_is_built_only_after_the_check(self):
        checking = {name for name, block in self.jobs.items() if CHECK_COMMAND in normalised(block)}
        self.assertTrue(checking & reachable_needs(self.jobs, "build"), "сборка страницы не ждёт проверку")


if __name__ == "__main__":
    unittest.main()
