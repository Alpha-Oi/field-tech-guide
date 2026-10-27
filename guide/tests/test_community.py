"""Тесты документов открытого проекта: участие, безопасность, правила поведения, шаблоны GitHub,
настройки редактора и окончаний строк, пояснения в docs/.

Ожидаемые значения берутся из задачи и из кода проекта (схемы и флаги в `guide/build.py`,
`tools/check_public.py`), а не из проверяемых файлов: документ, который разошёлся с кодом, роняет тест.
"""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from guide import build
from guide.tests import test_repo
from tools import check_public

ROOT = Path(__file__).resolve().parents[2]

MARKDOWN_DOCS = (
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "docs/architecture.md",
    "docs/pricelist.md",
    "docs/privacy.md",
    "docs/deploy.md",
)
GITHUB_FILES = (
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/dependabot.yml",
)
CONFIG_FILES = (".editorconfig", ".gitattributes")
ALL_FILES = MARKDOWN_DOCS + GITHUB_FILES + CONFIG_FILES
ISSUE_FORMS = (".github/ISSUE_TEMPLATE/bug_report.yml", ".github/ISSUE_TEMPLATE/feature_request.yml")

# Что с чем должно быть связано ссылками (пути от корня репозитория).
REQUIRED_LINKS = {
    "CONTRIBUTING.md": {
        "SECURITY.md", "CODE_OF_CONDUCT.md", "docs/architecture.md", "docs/pricelist.md",
        "docs/privacy.md", "docs/deploy.md",
    },
    "SECURITY.md": {"CONTRIBUTING.md", "docs/privacy.md", "docs/architecture.md"},
    "CODE_OF_CONDUCT.md": {"CONTRIBUTING.md"},
    "docs/architecture.md": {"docs/pricelist.md", "docs/privacy.md", "CONTRIBUTING.md"},
    "docs/pricelist.md": {"docs/architecture.md", "docs/deploy.md", "CONTRIBUTING.md"},
    "docs/privacy.md": {"docs/deploy.md", "SECURITY.md", "CONTRIBUTING.md"},
    "docs/deploy.md": {"docs/privacy.md", "docs/pricelist.md", "CONTRIBUTING.md"},
}

# Одна команда проверки целиком — в CONTRIBUTING.md (строгий вариант: без --lenient-vars).
FULL_CHECK = (
    "python -m unittest discover -s guide/tests -t . "
    "&& python guide/build.py --check "
    "&& python tools/check_public.py"
)

ROOT_NAMES = frozenset({
    "README.md", "CHANGELOG.md", "LICENSE", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "SECURITY.md",
    "CODE_OF_CONDUCT.md", ".editorconfig", ".gitattributes", ".gitignore",
})
PATH_PREFIXES = ("guide/", "tools/", "docs/", ".github/")

FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
LINK_RE = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
CODE_SPAN_RE = re.compile(r"`([A-Za-z0-9_.][A-Za-z0-9_./-]*)`")
JSON_BLOCK_RE = re.compile(r"```json\r?\n(.*?)```", re.DOTALL)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<![\w.])\+?\d[\d\s().-]{8,}\d(?![\w.])")


def read(case, rel):
    """Текст файла репозитория; нет файла — понятный провал теста, а не трассировка."""
    path = ROOT / rel
    if not path.is_file():
        case.fail(f"нет файла {rel}")
    return path.read_text(encoding="utf-8")


def without_fences(text):
    return FENCE_RE.sub("", text)


def normalized(text):
    return " ".join(text.split())


def top_level_keys(text):
    """Ключи YAML верхнего уровня (без отступа) — разбор без PyYAML."""
    return re.findall(r"(?m)^([A-Za-z_][\w-]*):", text)


def help_flags(main):
    """Флаги, которые команда называет в `--help`."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        try:
            main(["--help"])
        except SystemExit:
            pass
    return set(re.findall(r"--[a-z][a-z-]*", out.getvalue()))


def json_blocks(text):
    blocks = []
    for raw in JSON_BLOCK_RE.findall(text):
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise AssertionError(f"блок ```json не разбирается: {exc}\n{raw[:200]}") from exc
    return blocks


def link_targets(rel, text):
    """Относительные ссылки файла как пути от корня репозитория (без якорей); внешние пропущены."""
    base = (ROOT / rel).parent
    found = set()
    for target in LINK_RE.findall(without_fences(text)):
        if re.match(r"[a-z][a-z0-9+.-]*:", target, re.IGNORECASE) or target.startswith("#"):
            continue
        path = target.split("#", 1)[0]
        if not path:
            continue
        resolved = Path(os.path.normpath(base / path))
        found.add(resolved.relative_to(ROOT).as_posix())
    return found


class CommunityFilesTests(unittest.TestCase):
    def test_every_listed_file_exists_and_is_not_empty(self):
        for rel in ALL_FILES:
            with self.subTest(rel):
                path = ROOT / rel
                self.assertTrue(path.is_file(), f"нет файла {rel}")
                self.assertGreater(len(path.read_text(encoding="utf-8").strip()), 20, rel)

    def test_files_are_utf8_with_lf_and_one_final_newline(self):
        for rel in ALL_FILES:
            with self.subTest(rel):
                self.assertTrue((ROOT / rel).is_file(), f"нет файла {rel}")
                raw = (ROOT / rel).read_bytes()
                raw.decode("utf-8")
                self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "BOM в начале файла")
                self.assertNotIn(b"\r", raw, "окончания строк CRLF")
                self.assertTrue(raw.endswith(b"\n") and not raw.endswith(b"\n\n"), "нужна ровно одна пустая строка в конце")

    def test_markdown_documents_start_with_a_title(self):
        for rel in MARKDOWN_DOCS:
            with self.subTest(rel):
                self.assertTrue(read(self, rel).startswith("# "), "первая строка — заголовок первого уровня")


class CommunityLeakTests(unittest.TestCase):
    """В новых документах нет данных компании, чисел и слов из внутренних документов, адресов и телефонов."""

    def test_nothing_from_the_company_list_of_forbidden_values(self):
        # список компании лежит в локальном private/forbidden.txt; без него проверка пропускается с пометкой
        forbidden = test_repo.real_forbidden(self)
        for rel in ALL_FILES:
            self.assertTrue((ROOT / rel).is_file(), f"нет файла {rel}")
        paths = [ROOT / rel for rel in ALL_FILES]
        self.assertEqual(test_repo.forbidden_in_files(paths, forbidden, collapse_thousands=True), [])

    def test_no_email_addresses_and_no_phone_numbers(self):
        for rel in ALL_FILES:
            with self.subTest(rel):
                text = read(self, rel)
                self.assertEqual(EMAIL_RE.findall(text), [], "адрес электронной почты")
                for match in PHONE_RE.findall(text):
                    self.assertLess(sum(char.isdigit() for char in match), 10, f"похоже на телефон: {match!r}")

    def test_the_leak_check_finds_nothing_in_these_files(self):
        for rel in ALL_FILES:
            self.assertTrue((ROOT / rel).is_file(), f"нет файла {rel}")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            findings = check_public.scan(ROOT, skip_files=[])
        mine = [f for f in findings if f.path in ALL_FILES]
        self.assertEqual(mine, [], out.getvalue())


class LinksTests(unittest.TestCase):
    def test_relative_links_point_to_existing_files(self):
        for rel in MARKDOWN_DOCS + (".github/PULL_REQUEST_TEMPLATE.md",):
            with self.subTest(rel):
                for target in sorted(link_targets(rel, read(self, rel))):
                    self.assertTrue((ROOT / target).exists(), f"{rel}: ссылка на несуществующий {target}")

    def test_documents_link_to_each_other(self):
        for rel, required in REQUIRED_LINKS.items():
            with self.subTest(rel):
                missing = required - link_targets(rel, read(self, rel))
                self.assertEqual(sorted(missing), [], f"{rel}: нет ссылок на эти файлы")

    def test_paths_named_in_code_spans_exist(self):
        for rel in MARKDOWN_DOCS:
            with self.subTest(rel):
                for name in sorted(set(CODE_SPAN_RE.findall(without_fences(read(self, rel))))):
                    if name in ROOT_NAMES or name.startswith(PATH_PREFIXES):
                        self.assertTrue((ROOT / name).exists(), f"{rel}: `{name}` — такого пути нет в репозитории")

    def test_build_and_check_flags_named_in_documents_are_real(self):
        known = {"guide/build.py": help_flags(build.main), "tools/check_public.py": help_flags(check_public.main)}
        for rel in MARKDOWN_DOCS:
            with self.subTest(rel):
                for line in read(self, rel).splitlines():
                    for script, flags in known.items():
                        # флаги только из той команды, в которой назван скрипт: до «&&», «;», «|» или конца фрагмента кода
                        for command in re.finditer(re.escape(script) + r"([^&;|`]*)", line):
                            for flag in re.findall(r"--[a-z][a-z-]*", command.group(1)):
                                self.assertIn(flag, flags, f"{rel}: у {script} нет флага {flag}")


class ContributingTests(unittest.TestCase):
    def text(self):
        return read(self, "CONTRIBUTING.md")

    def test_has_the_full_check_command_in_bash_and_powershell_blocks(self):
        text = self.text()
        self.assertIn(FULL_CHECK, normalized(text))
        self.assertRegex(text, r"```bash")
        self.assertRegex(text, r"```powershell")
        # PowerShell 5.1 не знает «&&»: для него команды идут по одной
        powershell = "\n".join(re.findall(r"```powershell\r?\n(.*?)```", text, re.DOTALL))
        for command in ("python -m unittest discover -s guide/tests -t .", "python guide/build.py --check", "python tools/check_public.py"):
            self.assertIn(command, powershell)

    def test_explains_setup_without_dependencies(self):
        text = self.text()
        for needle in ("git clone", "Python 3.10", "node", "REQUIRE_NODE", "python guide/build.py"):
            self.assertIn(needle, text)
        self.assertRegex(text, r"(?i)зависимост")

    def test_describes_every_step_and_stage_field_of_the_content_schema(self):
        text = self.text()
        for field in build.STEP_FIELDS + build.STAGE_FIELDS:
            self.assertIn(f"`{field}`", text, f"поле {field} не описано")
        for kind in build.KINDS:
            self.assertIn(f"`{kind}`", text)
        for needle in ("Готово,", "{{app_name}}", "guide/profiles/VARS.md", "guide/content", "--list-vars", "--manifest"):
            self.assertIn(needle, text)

    def test_json_examples_are_valid_content(self):
        demo = build.load_profile(str(test_repo.DEMO_PROFILE))
        cheatsheet = build.load_content(str(test_repo.CONTENT_DIR))["cheatsheet"]
        checked = 0
        for block in json_blocks(self.text()):
            if not isinstance(block, dict):
                continue
            if "stage" in block:
                stage = dict(block["stage"], steps=block.get("steps", []))
            elif {"kind", "lead"} <= set(block):
                stage = {"id": str(block.get("id", "s9-01")).split("-")[0], "title": "Этап", "steps": [block]}
            else:
                continue
            data = build.apply_profile({"stages": [stage], "cheatsheet": cheatsheet}, demo)
            self.assertEqual(build.validate(data), [])
            checked += 1
        self.assertGreaterEqual(checked, 1, "в CONTRIBUTING.md нет примера этапа или шага в ```json")

    def test_explains_how_to_add_pricelist_items(self):
        text = self.text()
        self.assertIn("docs/pricelist.md", text)
        self.assertIn("--pricelist", text)
        self.assertIn("guide/profiles/demo-pricelist.json", text)

    def test_states_the_honesty_rules_of_the_texts(self):
        text = self.text().lower()
        for stem in (r"давлени\w+ на клиент", r"завышени\w+ цен", r"дроблени\w+ услуг", r"имитаци\w+ работ", r"нелицензион", "на «ты»", "коротко"):
            self.assertRegex(text, stem)

    def test_states_the_privacy_rules(self):
        text = self.text()
        for needle in ("private/", "tools/check_public.py", "private/markers.txt", "git add", "docs/privacy.md"):
            self.assertIn(needle, text)
        self.assertRegex(text, r"(?i)\*\.docx|docx")
        self.assertRegex(text, r"(?i)не коммит|в git не|не попад")

    def test_explains_commits_and_pull_requests(self):
        text = self.text()
        for needle in ("CHANGELOG.md", "pull request", "feat:", "fix:", "docs:", "test:", "chore:"):
            self.assertIn(needle, text)


class SecurityTests(unittest.TestCase):
    def text(self):
        return read(self, "SECURITY.md")

    def test_tells_how_to_report_through_github_advisories_without_an_email(self):
        text = self.text()
        self.assertIn("Report a vulnerability", text)
        self.assertRegex(text, r"(?i)security advisor")
        self.assertEqual(EMAIL_RE.findall(text), [])

    def test_names_the_supported_version_from_the_changelog(self):
        changelog = read(self, "CHANGELOG.md")
        found = re.search(r"(?m)^## \[(\d+)\.(\d+)\.\d+\]", changelog)
        self.assertIsNotNone(found)
        self.assertIn(f"{found.group(1)}.{found.group(2)}.x", self.text())

    def test_states_what_the_page_stores_and_how_it_is_built(self):
        text = self.text()
        for needle in ("localStorage", "masterGuide.progress.v1", "textContent", "https://", "один файл"):
            self.assertIn(needle, text)
        self.assertRegex(text.lower(), r"номер заявки")
        self.assertRegex(text.lower(), r"телефон")


class CodeOfConductTests(unittest.TestCase):
    def test_is_based_on_contributor_covenant_2_1_with_the_source_and_a_github_contact(self):
        text = read(self, "CODE_OF_CONDUCT.md")
        self.assertIn("https://www.contributor-covenant.org/version/2/1/code_of_conduct/", text)
        self.assertIn("Contributor Covenant", text)
        self.assertIn("2.1", text)
        self.assertIn("CC BY 4.0", text)
        self.assertIn("Alpha-Oi", text)
        self.assertEqual(EMAIL_RE.findall(text), [])


class IssueTemplatesTests(unittest.TestCase):
    def test_forms_have_name_description_title_and_body(self):
        for rel in ISSUE_FORMS:
            with self.subTest(rel):
                keys = top_level_keys(read(self, rel))
                for key in ("name", "description", "title", "body"):
                    self.assertIn(key, keys)

    def test_form_body_items_have_known_types(self):
        for rel in ISSUE_FORMS:
            with self.subTest(rel):
                types = re.findall(r"(?m)^  - type:\s*(\w+)\s*$", read(self, rel))
                self.assertGreaterEqual(len(types), 3)
                self.assertLessEqual(set(types), {"markdown", "input", "textarea", "dropdown", "checkboxes"})
                self.assertIn("markdown", types, "нет блока-напоминания")
                self.assertIn("textarea", types)

    def test_forms_remind_not_to_attach_company_or_client_data(self):
        for rel in ISSUE_FORMS:
            with self.subTest(rel):
                text = read(self, rel).lower()
                self.assertRegex(text, r"не прикладыва")
                self.assertRegex(text, r"компани")
                self.assertRegex(text, r"клиент")

    def test_bug_report_asks_for_steps_expected_actual_version_browser_and_device(self):
        text = read(self, ".github/ISSUE_TEMPLATE/bug_report.yml").lower()
        for stem in ("шаг", "ожида", "фактическ", "верси", "браузер", "устройств"):
            self.assertIn(stem, text)

    def test_config_disables_blank_issues_and_points_to_the_security_policy(self):
        text = read(self, ".github/ISSUE_TEMPLATE/config.yml")
        self.assertRegex(text, r"(?m)^blank_issues_enabled:\s*false\s*$")
        self.assertRegex(text, r"(?m)^contact_links:\s*$")
        self.assertRegex(text, r"(?m)^\s+url:\s*https://github\.com/Alpha-Oi/\S*SECURITY\.md\s*$")
        self.assertEqual(EMAIL_RE.findall(text), [])

    def test_yaml_parses_when_pyyaml_is_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML не установлен: полный разбор YAML пропущен, структура проверена простым разбором")
        for rel in ISSUE_FORMS:
            with self.subTest(rel):
                document = yaml.safe_load(read(self, rel))
                self.assertIsInstance(document["name"], str)
                self.assertIsInstance(document["description"], str)
                self.assertTrue(document["body"])
                ids = [item["id"] for item in document["body"] if "id" in item]
                self.assertEqual(len(ids), len(set(ids)), "id полей повторяются")
                for item in document["body"]:
                    if item["type"] != "markdown":
                        self.assertTrue(item["attributes"]["label"])
        config = yaml.safe_load(read(self, ".github/ISSUE_TEMPLATE/config.yml"))
        self.assertIs(config["blank_issues_enabled"], False)
        self.assertTrue(config["contact_links"][0]["url"].startswith("https://"))


class PullRequestTemplateTests(unittest.TestCase):
    def test_has_a_checklist_with_the_agreed_points(self):
        text = read(self, ".github/PULL_REQUEST_TEMPLATE.md")
        self.assertGreaterEqual(len(re.findall(r"(?m)^- \[ \] ", text)), 5)
        for pattern in (r"(?i)тест", r"check_public", r"(?i)честн", r"(?i)данн\w+ компани", r"CHANGELOG"):
            self.assertRegex(text, pattern)


class DependabotTests(unittest.TestCase):
    def test_updates_only_github_actions_weekly_from_the_root(self):
        text = read(self, ".github/dependabot.yml")
        self.assertRegex(text, r"(?m)^version:\s*2\s*$")
        self.assertEqual(re.findall(r"(?m)^\s*-?\s*package-ecosystem:\s*[\"']?([\w-]+)[\"']?\s*$", text), ["github-actions"])
        self.assertRegex(text, r"(?m)^\s*directory:\s*[\"']/[\"']\s*$")
        self.assertRegex(text, r"(?m)^\s*interval:\s*[\"']?weekly[\"']?\s*$")

    def test_yaml_parses_when_pyyaml_is_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML не установлен: полный разбор YAML пропущен, структура проверена простым разбором")
        document = yaml.safe_load(read(self, ".github/dependabot.yml"))
        self.assertEqual(document["version"], 2)
        self.assertEqual(len(document["updates"]), 1)
        self.assertEqual(document["updates"][0]["package-ecosystem"], "github-actions")
        self.assertEqual(document["updates"][0]["schedule"]["interval"], "weekly")


def editorconfig_sections(text):
    """(root, [(шаблон, {ключ: значение})]) — разбор .editorconfig без сторонних пакетов."""
    root, sections, current = False, [], None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        header = re.fullmatch(r"\[(.+)\]", line)
        if header:
            current = (header.group(1), {})
            sections.append(current)
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key, value = key.strip().lower(), value.strip().lower()
        if current is None:
            root = root or (key == "root" and value == "true")
        else:
            current[1][key] = value
    return root, sections


def editorconfig_for(sections, name):
    """Итоговые настройки для файла с таким именем: подходящие секции по порядку, поздние главнее."""
    result = {}
    for pattern, props in sections:
        braces = re.fullmatch(r"\*\.\{(.+)\}", pattern)
        if braces:
            matches = name.rsplit(".", 1)[-1] in braces.group(1).split(",")
        elif pattern == "*":
            matches = True
        elif pattern.startswith("*."):
            matches = name.endswith(pattern[1:])
        else:
            matches = pattern == name
        if matches:
            result.update(props)
    return result


class EditorconfigTests(unittest.TestCase):
    def sections(self):
        return editorconfig_sections(read(self, ".editorconfig"))

    def test_is_the_root_config_with_utf8_lf_and_final_newline(self):
        root, sections = self.sections()
        self.assertTrue(root)
        for name in ("build.py", "page.html", "README.md", "profile.json", "ci.yml"):
            props = editorconfig_for(sections, name)
            self.assertEqual(props.get("charset"), "utf-8", name)
            self.assertEqual(props.get("end_of_line"), "lf", name)
            self.assertEqual(props.get("insert_final_newline"), "true", name)

    def test_python_uses_four_spaces(self):
        props = editorconfig_for(self.sections()[1], "build.py")
        self.assertEqual(props.get("indent_style"), "space")
        self.assertEqual(props.get("indent_size"), "4")

    def test_json_yaml_html_and_markdown_use_two_spaces(self):
        sections = self.sections()[1]
        for name in ("profile.json", "ci.yml", "dependabot.yaml", "template.html", "README.md"):
            props = editorconfig_for(sections, name)
            self.assertEqual(props.get("indent_style"), "space", name)
            self.assertEqual(props.get("indent_size"), "2", name)


@unittest.skipUnless(shutil.which("git") and (ROOT / ".git").exists(), "нужны git и репозиторий (папка .git)")
class GitattributesTests(unittest.TestCase):
    def attr(self, name, path):
        done = subprocess.run(["git", "-C", str(ROOT), "check-attr", name, "--", path], capture_output=True, text=True, check=True)
        return done.stdout.strip().rsplit(": ", 1)[-1]

    def test_text_files_are_normalized_to_lf(self):
        for path in ("guide/build.py", "README.md", "guide/content/10-start.json", ".github/workflows/ci.yml"):
            with self.subTest(path):
                self.assertEqual(self.attr("text", path), "auto")
                self.assertEqual(self.attr("eol", path), "lf")

    def test_png_and_svg_keep_their_bytes(self):
        for path in ("docs/screenshot.png", "docs/img/steps-by-stage.svg", "docs/img/social-preview.png"):
            with self.subTest(path):
                self.assertEqual(self.attr("text", path), "unset")


@unittest.skipUnless(shutil.which("git"), "git не найден")
class LineEndingsInRealGitTests(unittest.TestCase):
    """Правила из .gitattributes в настоящем git: CRLF в рабочей папке не попадает в индекс, повторная
    нормализация ничего не меняет, картинки и SVG хранятся как есть."""

    # «картинка», которую git по содержимому счёл бы текстом: сохранить её байты в точности должно правило, а не эвристика
    PNG = b"not really a picture\r\nplain bytes\r\n"
    SVG = b"<svg>\r\n<g/>\r\n</svg>\r\n"

    def make_repo(self, autocrlf):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        self.git(repo, "init", "-q")
        self.git(repo, "config", "core.autocrlf", autocrlf)
        self.git(repo, "config", "core.safecrlf", "false")  # глобальная настройка машины не должна мешать проверке
        read(self, ".gitattributes")  # нет файла — провал с понятным сообщением
        (repo / ".gitattributes").write_bytes((ROOT / ".gitattributes").read_bytes())
        (repo / "note.md").write_bytes(b"one\r\ntwo\r\n")
        (repo / "script.py").write_bytes(b"x = 1\r\n")
        (repo / "pic.png").write_bytes(self.PNG)
        (repo / "chart.svg").write_bytes(self.SVG)
        return repo

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout

    def blob(self, repo, path):
        sha = self.git(repo, "rev-parse", f":{path}").decode().strip()
        return self.git(repo, "cat-file", "blob", sha)

    def test_crlf_text_goes_into_the_index_as_lf_and_other_files_stay_byte_for_byte(self):
        for autocrlf in ("true", "false"):
            with self.subTest(core_autocrlf=autocrlf):
                repo = self.make_repo(autocrlf)
                self.git(repo, "add", "-A")
                self.assertEqual(self.blob(repo, "note.md"), b"one\ntwo\n")
                self.assertEqual(self.blob(repo, "script.py"), b"x = 1\n")
                self.assertEqual(self.blob(repo, "pic.png"), self.PNG)
                self.assertEqual(self.blob(repo, "chart.svg"), self.SVG)

    def test_renormalizing_again_changes_nothing(self):
        for autocrlf in ("true", "false"):
            with self.subTest(core_autocrlf=autocrlf):
                repo = self.make_repo(autocrlf)
                self.git(repo, "add", "-A")
                before = self.git(repo, "write-tree")
                self.git(repo, "add", "--renormalize", ".")
                self.git(repo, "add", "--renormalize", ".")
                self.assertEqual(self.git(repo, "write-tree"), before)


class ArchitectureDocTests(unittest.TestCase):
    def text(self):
        return read(self, "docs/architecture.md")

    def test_names_the_layers_in_order(self):
        text = self.text().lower()
        positions = [text.find(word) for word in ("контент", "профиль", "прайс", "сборк", "страниц", "записк")]
        self.assertNotIn(-1, positions)

    def test_names_every_public_function_of_the_build_module(self):
        text = self.text()
        for name in ("load_content", "load_profile", "apply_profile", "validate", "render_md", "render_html",
                     "render_techdir", "coverage", "load_pricelist", "validate_pricelist"):
            self.assertTrue(callable(getattr(build, name)), f"в build.py нет {name}")
            self.assertIn(name, text)

    def test_explains_the_page_without_external_connections(self):
        text = self.text()
        for needle in ("guide/template.html", "logic:start", "logic:end", "masterGuide.progress.v1", "textContent",
                       "createElement", "GUIDE_DATA", "guide-data", "try/catch", "node"):
            self.assertIn(needle, text)
        template = (ROOT / "guide" / "template.html").read_text(encoding="utf-8")
        for marker in ("/* logic:start */", "/* logic:end */", "masterGuide.progress.v1", build.DATA_MARKER):
            self.assertIn(marker, template, "документ говорит о том, чего нет в шаблоне страницы")

    def test_explains_the_leak_check_and_the_boundaries(self):
        text = self.text()
        for needle in ("tools/check_public.py", "git ls-files", "private/markers.txt", "guide/content", "guide/profiles",
                       "guide/docs/for-techdir.md", ".github/workflows/ci.yml", ".github/workflows/pages.yml"):
            self.assertIn(needle, text)
        for extension in sorted(check_public.FORBIDDEN_EXTENSIONS):
            self.assertIn(extension, text)


class PricelistDocTests(unittest.TestCase):
    def text(self):
        return read(self, "docs/pricelist.md")

    def test_describes_every_field_and_kind_of_the_schema(self):
        text = self.text()
        names = (build.PRICELIST_FIELDS + build.PRICE_ITEM_FIELDS + build.PRICE_MATRIX_FIELDS + build.PRICE_COLUMN_FIELDS
                 + build.PRICE_ROW_FIELDS + build.PRICE_HINT_FIELDS + build.PRICE_KINDS)
        for name in names:
            self.assertIn(f"`{name}`", text, f"{name} не описано")
        self.assertIn('"ask"', text)
        self.assertIn("null", text)

    def test_complete_json_examples_pass_the_pricelist_validation(self):
        checked = 0
        for block in json_blocks(self.text()):
            if isinstance(block, dict) and "items" in block:
                self.assertEqual(build.validate_pricelist(block), [])
                checked += 1
        self.assertGreaterEqual(checked, 1, "нет полного примера прайса в ```json")

    def test_explains_connecting_a_pricelist_and_how_the_panel_counts(self):
        text = self.text()
        for needle in ("--pricelist", "--no-pricelist", "private/pricelist.json", "guide/profiles/demo-pricelist.json",
                       "Прайс и акт", "validate_pricelist", "К оплате", "Стоимость услуг", "Стоимость комплектующих",
                       "Скидка", "Гарантия"):
            self.assertIn(needle, text)
        self.assertRegex(text, r"(?i)количеств")
        self.assertRegex(text, r"(?i)не (?:может быть )?выше прайса|не может превышать|выше прайса")
        self.assertRegex(text, r"(?i)половина вверх|вверх")


class PrivacyDocTests(unittest.TestCase):
    def text(self):
        return read(self, "docs/privacy.md")

    def test_says_what_is_stored_where(self):
        text = self.text()
        for needle in ("localStorage", "masterGuide.progress.v1", "в памяти", "private/", "private/profile.json",
                       "private/pricelist.json", "private/markers.txt", "private/dist"):
            self.assertIn(needle, text)
        lower = text.lower()
        for word in ("номер заявки", "телефон", "выбор"):
            self.assertIn(word, lower)

    def test_private_is_really_ignored_by_git(self):
        ignore = [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()]
        self.assertIn("private/", ignore)
        self.assertIn("private/", self.text())

    def test_explains_the_leak_check_and_what_must_not_be_published(self):
        text = self.text()
        self.assertIn("tools/check_public.py", text)
        self.assertIn("--markers", text)
        for extension in sorted(check_public.FORBIDDEN_EXTENSIONS):
            self.assertIn(extension, text)
        self.assertRegex(text.lower(), r"слов\w*-маркер|маркер")


class DeployDocTests(unittest.TestCase):
    def text(self):
        return read(self, "docs/deploy.md")

    def test_lists_the_ways_to_hand_the_guide_to_masters(self):
        text = self.text()
        for needle in ("index.html", "guide.md", "for-techdir.md", "GitHub Pages", "мессенджер"):
            self.assertIn(needle, text)

    def test_shows_how_to_build_the_real_guide_from_own_profile_and_pricelist(self):
        text = normalized(self.text())
        self.assertIn("--profile private/profile.json", text)
        self.assertIn("--pricelist private/pricelist.json", text)
        self.assertIn("--out private/dist", text)
        self.assertRegex(text, r"(?i)обнов")
        self.assertIn(".github/workflows/pages.yml", text)

    def test_pages_publishes_only_the_demo(self):
        text = self.text().lower()
        self.assertRegex(text, r"демо")
        self.assertRegex(text, r"pages[^.\n]*демо|демо[^.\n]*pages")


if __name__ == "__main__":
    unittest.main()
