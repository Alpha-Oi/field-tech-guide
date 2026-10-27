"""Тесты упаковки репозитория: демо-профиль, описание переменных, записка техдиру,
защита от утечки на временном репозитории, служебные файлы (README, LICENSE, CI).

Ожидаемые значения берутся из interfaces.md и спецификации, а не из проверяемых файлов.
"""

import contextlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import NamedTuple

from guide import build
from tools import check_public

ROOT = Path(__file__).resolve().parents[2]
CONTENT_DIR = ROOT / "guide" / "content"
DEMO_PROFILE = ROOT / "guide" / "profiles" / "demo.json"
VARS_DOC = ROOT / "guide" / "profiles" / "VARS.md"
TECHDIR_TEMPLATE = ROOT / "guide" / "docs" / "for-techdir.md"
PRIVATE_PROFILE = ROOT / "private" / "profile.json"

# Одна команда проверки из interfaces.md — CI обязан запускать именно её.
# Последний шаг — актуальность графика docs/img/steps-by-stage.svg.
CHECK_COMMAND = (
    "python -m unittest discover -s guide/tests -t . "
    "&& python guide/build.py --check --lenient-vars "
    "&& python tools/check_public.py "
    "&& python tools/make_charts.py --check"
)

# Та же команда в документах для людей: строгий вариант, без --lenient-vars.
DOCUMENT_CHECK_COMMAND = (
    "python -m unittest discover -s guide/tests -t . "
    "&& python guide/build.py --check "
    "&& python tools/check_public.py "
    "&& python tools/make_charts.py --check"
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Числа и слова из внутренних документов компании в публичный файл не попадают ни в каком виде
# (ни открытым текстом, ни хешем: короткие числа по хешу подбираются за доли секунды). Запрещённый
# список лежит в локальном файле private/forbidden.txt, которого в git нет. Формат файла:
#   # комментарий; пустые строки пропускаются
#   value: <точное значение>     значение переменной профиля, которое демо-профиль не должен повторять
#   text: <регулярное выражение> шаблон, которого нет в публичных файлах; регистр не важен
# Без файла проверки чисел компании пропускаются с пометкой, проверки на временных данных идут как обычно.
FORBIDDEN_PATH = ROOT / "private" / "forbidden.txt"
FORBIDDEN_SKIP_NOTE = "нет private/forbidden.txt: проверка чисел компании пропущена"
# цифры, разделённые пробелом разрядов («12 345», в том числе с неразрывным пробелом), — одно число
THOUSANDS_RE = re.compile(r"(?<=\d)[ \u00a0\u202f\u2009](?=\d{3}(?!\d))")

PUBLIC_TEXT_SUFFIXES = {".json", ".md", ".py", ".html", ".yml", ".txt"}

STAGE_ID_RE = re.compile(r"s(\d+)(b)?")
STAGE_TITLE_RE = re.compile(r"Этап (\d+а?)\. ")
# «этап 3», «этапе 4а», «этапов 1–3», «этапами 2 и 3»
STAGE_REF_RE = re.compile(r"[Ээ]тап\w*\s+(\d+а?(?:\s*(?:[–—-]|и|,)\s*\d+а?)*)")


def read_repo_file(case, rel):
    """Текст файла репозитория; нет файла — понятный провал теста, а не трассировка."""
    path = ROOT / rel
    if not path.is_file():
        case.fail(f"нет файла {rel}")
    return path.read_text(encoding="utf-8")


def run_build(*args):
    """build.main в этом же процессе; возвращает (код, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = build.main(list(args))
    return code, out.getvalue(), err.getvalue()


def normalize_value(value):
    """Значение профиля без пробелов и с одним видом тире: «7 000 – 8 000» и «7000-8000» — одно и то же."""
    return re.sub(r"[\s\u00a0]", "", value).replace("-", "–").replace("—", "–").lower()


class ForbiddenListError(ValueError):
    """Строка файла запрещённого списка записана неверно."""


class Forbidden(NamedTuple):
    values: frozenset  # нормализованные (normalize_value) значения переменных профиля
    patterns: tuple  # скомпилированные шаблоны текста, регистр не важен


def load_forbidden(path):
    """Запрещённый список из файла: строки `value: …` и `text: …`, `#` и пустые строки пропускаются.

    Текст неверной строки в сообщение об ошибке не попадает: в нём могут быть слова компании.
    """
    values, patterns = set(), []
    text = Path(path).read_text(encoding="utf-8-sig")
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind, colon, body = line.partition(":")
        kind, body = kind.strip().lower(), body.strip()
        if not colon or kind not in ("value", "text"):
            raise ForbiddenListError(f"строка {number}: нужна запись вида «value: <значение>» или «text: <шаблон>»")
        if not body:
            raise ForbiddenListError(f"строка {number}: после «{kind}:» пусто")
        if kind == "value":
            values.add(normalize_value(body))
            continue
        try:
            patterns.append(re.compile(body, re.IGNORECASE))
        except re.error as exc:
            raise ForbiddenListError(f"строка {number}: неверное регулярное выражение ({exc.msg})") from None
    return Forbidden(frozenset(values), tuple(patterns))


def real_forbidden(case, path=FORBIDDEN_PATH):
    """Список компании из private/forbidden.txt; нет файла — тест пропускается с пометкой."""
    if not Path(path).is_file():
        case.skipTest(FORBIDDEN_SKIP_NOTE)
    return load_forbidden(path)


def forbidden_vars(profile_vars, forbidden):
    """Имена переменных профиля, значения которых есть в списке запрещённого."""
    return sorted(name for name, value in profile_vars.items() if normalize_value(value) in forbidden.values)


def forbidden_numbers(numbers, forbidden):
    """Подписи («число № N») чисел, которые есть в списке запрещённых значений; сами числа не возвращаются."""
    return [f"число № {position}" for position, number in enumerate(numbers, start=1) if str(number) in forbidden.values]


def forbidden_in_files(paths, forbidden, collapse_thousands=False):
    """«файл: шаблон № N» для каждого шаблона, сработавшего на тексте файла (сам шаблон не возвращается).

    collapse_thousands — число с пробелом разрядов («12 345») проверяется ещё и как «12345».
    """
    found = []
    for path in paths:
        path = Path(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        variants = [text, THOUSANDS_RE.sub("", text)] if collapse_thousands else [text]
        shown = path.relative_to(ROOT).as_posix() if ROOT in path.parents else path.name
        for number, pattern in enumerate(forbidden.patterns, start=1):
            if any(pattern.search(variant) for variant in variants):
                found.append(f"{shown}: шаблон № {number}")
    return found


def iter_strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from iter_strings(item)
    elif isinstance(node, dict):
        for item in node.values():
            yield from iter_strings(item)


def iter_numbers(node):
    """Все целые числа внутри JSON, кроме булевых значений."""
    if isinstance(node, bool):
        return
    if isinstance(node, int):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from iter_numbers(item)
    elif isinstance(node, dict):
        for item in node.values():
            yield from iter_numbers(item)


def load_stages():
    return build.load_content(str(CONTENT_DIR))["stages"]


def find_step(case, step_id):
    for stage in load_stages():
        for step in stage["steps"]:
            if step["id"] == step_id:
                return step
    case.fail(f"нет шага {step_id}")


def step_text(case, step_id):
    return "\n".join(iter_strings(find_step(case, step_id)))


def router_scare_problems(step):
    """Нарушения в шаге «Настройка роутера»: чистка реестра в услугах и журнал событий без оговорки «безвредн»."""
    problems = []
    text = "\n".join(iter_strings(step)).lower()
    if "реестр" in text:
        problems.append("в шаге роутера есть слово «реестр»: чистку реестра клиенту не продают")
    journal = [point.lower() for point in step.get("points", []) if "журнал" in point.lower()]
    if len(journal) != 1:
        problems.append(f"фраз про журнал событий должно быть одна, а их {len(journal)}")
    else:
        for needed in ("безвредн", "настоящ"):
            if needed not in journal[0]:
                problems.append(f"во фразе про журнал событий нет оговорки «{needed}…»")
    return problems


def public_text_files():
    """Текстовые файлы репозитория, которые уходят в публичный git (без private/, dist/, .autopilot/)."""
    files = sorted(ROOT.glob("*.md")) + [ROOT / "LICENSE"]
    for base in ("guide", "tools", ".github", "docs"):
        for path in sorted((ROOT / base).rglob("*")):
            if path.is_file() and path.suffix in PUBLIC_TEXT_SUFFIXES and "__pycache__" not in path.parts:
                files.append(path)
    return [path for path in files if path.is_file()]


def content_vars():
    return set(build.collect_vars(build.load_content(str(CONTENT_DIR))))


def techdir_vars():
    if not TECHDIR_TEMPLATE.is_file():
        return set()
    return set(build._text_vars(TECHDIR_TEMPLATE.read_text(encoding="utf-8")))


class DemoProfileTests(unittest.TestCase):
    def profile(self):
        if not DEMO_PROFILE.is_file():
            self.fail("нет файла guide/profiles/demo.json")
        return build.load_profile(str(DEMO_PROFILE))

    def test_covers_every_variable_found_in_content_and_techdir_note(self):
        missing = (content_vars() | techdir_vars()) - set(self.profile()["vars"])
        self.assertEqual(sorted(missing), [])

    def test_has_no_variables_that_nothing_uses(self):
        unused = set(self.profile()["vars"]) - content_vars() - techdir_vars()
        self.assertEqual(sorted(unused), [])

    def test_links_are_neutral_example_addresses(self):
        links = {k: v for k, v in self.profile()["vars"].items() if k.startswith("link_")}
        self.assertTrue(links, "в демо-профиле нет ссылок")
        for name, url in links.items():
            self.assertTrue(url.startswith("https://example.com/"), f"{name}: {url}")

    def test_values_are_not_taken_from_company_sources(self):
        forbidden = real_forbidden(self)
        self.assertEqual(forbidden_vars(self.profile()["vars"], forbidden), [])

    def test_demo_pricelist_has_no_numbers_from_company_sources(self):
        forbidden = real_forbidden(self)
        pricelist = json.loads((ROOT / "guide" / "profiles" / "demo-pricelist.json").read_text(encoding="utf-8"))
        numbers = list(iter_numbers(pricelist))
        self.assertTrue(numbers)
        self.assertEqual(forbidden_numbers(numbers, forbidden), [])

    def test_cleaning_price_matches_the_cleaning_item_of_the_demo_pricelist(self):
        pricelist = json.loads((ROOT / "guide" / "profiles" / "demo-pricelist.json").read_text(encoding="utf-8"))
        cleaning = [item for item in pricelist["items"] if item["name"] == "Чистка от пыли"]
        self.assertEqual(len(cleaning), 1)
        self.assertEqual(self.profile()["vars"]["price_cleaning"], str(cleaning[0]["price"]))

    def test_part_price_guidelines_are_round_made_up_numbers_above_the_cleaning_price(self):
        values = self.profile()["vars"]
        for name in ("price_psu", "price_board", "price_cpu"):
            self.assertRegex(values.get(name, ""), r"^[1-9]\d*00$", f"{name}: нужно круглое число")
        # довод держится, пока чистка дешевле любой из деталей
        cleaning = int(values["price_cleaning"])
        for name in ("price_psu", "price_board", "price_cpu"):
            self.assertLess(cleaning, int(values[name]), name)

    def test_check_passes_strictly_without_lenient_vars(self):
        code, out, err = run_build("--check")
        self.assertEqual(code, 0, err)
        self.assertNotIn("Неизвестные переменные", out)
        self.assertNotIn("Предупреждение", out)

    def test_default_build_writes_the_three_files_with_everything_substituted(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, _, err = run_build("--out", tmp)
            self.assertEqual(code, 0, err)
            texts = {}
            for name in ("index.html", "guide.md", "for-techdir.md"):
                path = Path(tmp) / name
                self.assertTrue(path.is_file(), name)
                texts[name] = path.read_text(encoding="utf-8")
            # рантайм-токены страницы («номер заявки», «телефон») в index.html остаются, переменные профиля — нет
            for name, text in texts.items():
                for var in content_vars() | techdir_vars():
                    self.assertNotIn("{{" + var + "}}", text, f"{name}: {var}")
            self.assertNotIn("{{", texts["for-techdir.md"])


class VarsDocTests(unittest.TestCase):
    def test_describes_every_profile_variable(self):
        text = read_repo_file(self, "guide/profiles/VARS.md")
        documented = set(re.findall(r"`([a-z][a-z0-9_]*)`", text))
        missing = (content_vars() | techdir_vars()) - documented
        self.assertEqual(sorted(missing), [])


    def test_examples_column_has_no_values_from_company_sources_and_no_real_values(self):
        forbidden = real_forbidden(self)
        text = read_repo_file(self, "guide/profiles/VARS.md")
        rows = [line for line in text.splitlines() if line.startswith("| `")]
        self.assertTrue(rows)
        for row in rows:
            cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
            example = cells[2]
            self.assertEqual(forbidden_vars({cells[0]: example}, forbidden), [], "значение из внутренних документов в колонке примеров")
            # пометка вида «у настоящего гайда — …» выдаёт реальное значение
            self.assertNotRegex(row, r"(?i)настоящ\w+ гайд\w*\s*[—-]\s*\d", cells[0])


class PublicTextsTests(unittest.TestCase):
    """В публичных файлах нет цифр и слов из внутренних документов компании."""

    def test_no_numbers_from_the_internal_documents(self):
        forbidden = real_forbidden(self)
        self.assertEqual(forbidden_in_files(public_text_files(), forbidden, collapse_thousands=True), [])

    def test_no_words_from_the_internal_documents(self):
        forbidden = real_forbidden(self)
        self.assertEqual(forbidden_in_files(public_text_files(), forbidden), [])

    def test_reader_texts_do_not_cite_the_internal_regulation(self):
        paths = sorted(CONTENT_DIR.glob("*.json")) + [TECHDIR_TEMPLATE, ROOT / "README.md"]
        for path in paths:
            self.assertNotRegex(path.read_text(encoding="utf-8").lower(), r"регламент", path.name)


class ForbiddenListLoaderTests(unittest.TestCase):
    """Загрузчик private/forbidden.txt — на временных файлах, без настоящего списка."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def write(self, text, name="forbidden.txt"):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_values_and_patterns_are_read_and_comments_and_blank_lines_are_skipped(self):
        path = self.write("# запрещённые значения: пример\n\nvalue: 7777\n   \n  # ещё комментарий\ntext: Образец\nvalue: 8888\n")
        forbidden = load_forbidden(path)
        self.assertEqual(forbidden.values, frozenset({"7777", "8888"}))
        self.assertEqual(len(forbidden.patterns), 1)
        self.assertTrue(forbidden.patterns[0].search("Это образец текста"))

    def test_values_are_normalised_like_profile_values(self):
        forbidden = load_forbidden(self.write("value: 7 777\nvalue: 11-22\nvalue: 33—44\nvalue: 5%\n"))
        self.assertEqual(forbidden.values, frozenset({"7777", "11–22", "33–44", "5%"}))

    def test_prefix_and_pattern_case_do_not_matter(self):
        forbidden = load_forbidden(self.write("TEXT: образец\nValue: ABC\n"))
        self.assertEqual(forbidden.values, frozenset({"abc"}))
        self.assertTrue(forbidden.patterns[0].search("ОБРАЗЕЦ"))

    def test_a_hash_inside_a_pattern_is_not_a_comment(self):
        forbidden = load_forbidden(self.write("text: C#\n"))
        self.assertEqual(len(forbidden.patterns), 1)
        self.assertTrue(forbidden.patterns[0].search("язык c# и другие"))

    def test_byte_order_mark_is_ignored(self):
        path = self.tmp / "bom.txt"
        path.write_bytes("\ufeffvalue: 7777\n".encode("utf-8"))
        self.assertEqual(load_forbidden(path).values, frozenset({"7777"}))

    def test_empty_file_gives_an_empty_list(self):
        forbidden = load_forbidden(self.write("# только комментарий\n"))
        self.assertEqual((forbidden.values, forbidden.patterns), (frozenset(), ()))

    def test_a_line_without_a_known_prefix_is_a_clear_error_with_its_number(self):
        path = self.write("value: 7777\n\nпросто слово\n")
        with self.assertRaisesRegex(ForbiddenListError, r"строка 3: .*value:.*text:"):
            load_forbidden(path)
        with self.assertRaisesRegex(ForbiddenListError, r"строка 1: "):
            load_forbidden(self.write("number: 7777\n", name="other.txt"))

    def test_an_empty_value_or_pattern_is_an_error(self):
        for text in ("value:\n", "text:   \n"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(ForbiddenListError, r"строка 1: .*пусто"):
                    load_forbidden(self.write(text))

    def test_a_broken_regular_expression_is_an_error_with_the_line_number(self):
        with self.assertRaisesRegex(ForbiddenListError, r"строка 2: .*регулярн"):
            load_forbidden(self.write("value: 7777\ntext: (незакрытая\n"))

    def test_an_error_never_repeats_the_text_of_the_line(self):
        with self.assertRaises(ForbiddenListError) as ctx:
            load_forbidden(self.write("секретнаяфраза без префикса\n"))
        self.assertNotIn("секретнаяфраза", str(ctx.exception))

    def test_without_the_file_the_company_checks_are_skipped_with_a_note(self):
        with self.assertRaises(unittest.SkipTest) as ctx:
            real_forbidden(self, self.tmp / "no-such-file.txt")
        self.assertEqual(str(ctx.exception), FORBIDDEN_SKIP_NOTE)
        self.assertEqual(FORBIDDEN_SKIP_NOTE, "нет private/forbidden.txt: проверка чисел компании пропущена")

    def test_with_the_file_the_company_list_is_loaded(self):
        forbidden = real_forbidden(self, self.write("value: 7777\ntext: Образец\n"))
        self.assertEqual(forbidden.values, frozenset({"7777"}))
        self.assertEqual(len(forbidden.patterns), 1)


class ForbiddenChecksOnTemporaryDataTests(unittest.TestCase):
    """Подложенное «запрещённое» значение роняет проверку; список и файлы здесь временные, настоящий список не нужен."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        listing = self.tmp / "forbidden.txt"
        listing.write_text("value: 7777\nvalue: 11-22\ntext: Образец\ntext: (?<![\\d.,])8888(?![\\d])\n", encoding="utf-8")
        self.forbidden = load_forbidden(listing)

    def public_file(self, text, name="README.md"):
        path = self.tmp / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_planted_value_in_a_temporary_profile_is_caught_by_name(self):
        profile = self.tmp / "profile.json"
        profile.write_text(json.dumps({"name": "t", "vars": {"price_x": "7 777", "range_y": "11 - 22", "fine": "100"}}), encoding="utf-8")
        loaded = build.load_profile(str(profile))
        self.assertEqual(forbidden_vars(loaded["vars"], self.forbidden), ["price_x", "range_y"])

    def test_a_profile_without_forbidden_values_passes(self):
        self.assertEqual(forbidden_vars({"a": "100", "b": "7777 руб", "c": ""}, self.forbidden), [])

    def test_a_planted_number_in_a_temporary_pricelist_is_caught_without_returning_it(self):
        found = forbidden_numbers([100, 7777, 250], self.forbidden)
        self.assertEqual(len(found), 1)
        self.assertNotIn("7777", " ".join(found))

    def test_a_planted_word_in_a_temporary_public_file_is_caught_in_any_case(self):
        for text in ("Это Образец текста", "ОБРАЗЕЦ", "про-образец-1"):
            with self.subTest(text=text):
                found = forbidden_in_files([self.public_file(text + "\n")], self.forbidden)
                self.assertEqual(len(found), 1, found)
                self.assertIn("README.md", found[0])

    def test_the_report_names_the_pattern_number_and_never_repeats_the_pattern(self):
        found = forbidden_in_files([self.public_file("Образец и 8888\n")], self.forbidden)
        self.assertEqual(len(found), 2)
        report = " ".join(found)
        self.assertIn("шаблон № 1", report)
        self.assertIn("шаблон № 2", report)
        self.assertNotIn("образец", report.lower())
        self.assertNotIn("8888", report)

    def test_a_number_with_a_thousands_separator_is_found_only_when_asked(self):
        for text in ("Цена 8 888 руб", "Цена 8\u00a0888 руб", "Цена 8\u202f888 руб"):
            with self.subTest(text=text):
                path = self.public_file(text + "\n")
                self.assertEqual(forbidden_in_files([path], self.forbidden), [])
                self.assertEqual(len(forbidden_in_files([path], self.forbidden, collapse_thousands=True)), 1)

    def test_neighbouring_digits_and_decimals_are_not_a_hit(self):
        path = self.public_file("Номера 18888, 88889, 1.8888 и 1,8888\n")
        self.assertEqual(forbidden_in_files([path], self.forbidden, collapse_thousands=True), [])

    def test_clean_files_pass_and_each_file_is_reported_by_its_own_name(self):
        clean = self.public_file("Ничего лишнего.\n", name="a.md")
        dirty = self.public_file("образец\n", name="b.md")
        found = forbidden_in_files([clean, dirty], self.forbidden)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].startswith("b.md"), found)


class StageNumberingTests(unittest.TestCase):
    """«Этап N» в тексте ведёт на реально пронумерованный заголовок этапа."""

    @staticmethod
    def expected_label(stage_id):
        match = STAGE_ID_RE.fullmatch(stage_id)
        # этап с буквой b в идентификаторе в тексте называется «4а» (кириллическая «а»)
        return match.group(1) + ("а" if match.group(2) else "")

    def test_every_stage_title_starts_with_its_number(self):
        for stage in load_stages():
            label = self.expected_label(stage["id"])
            self.assertTrue(stage["title"].startswith(f"Этап {label}. "), f"{stage['id']}: {stage['title']}")

    def test_documents_stage_is_numbered_4a_and_keeps_its_name(self):
        titles = {stage["id"]: stage["title"] for stage in load_stages()}
        self.assertEqual(titles["s4b"], "Этап 4а. Документы: БСО и «Мод»")

    def references(self):
        """(источник, номер) для каждого «этап N» в текстах гайда и записки."""
        found = []
        for stage in load_stages():
            for text in iter_strings(stage):
                found += [(stage["id"], text)]
        template = TECHDIR_TEMPLATE.read_text(encoding="utf-8")
        found += [("for-techdir.md", line) for line in template.splitlines()]
        refs = []
        for source, text in found:
            for match in STAGE_REF_RE.finditer(text):
                for number in re.findall(r"\d+а?", match.group(1)):
                    refs.append((source, number, text))
        return refs

    def test_every_stage_reference_points_to_a_numbered_heading(self):
        headings = set()
        for stage in load_stages():
            found = STAGE_TITLE_RE.match(stage["title"])
            if found:
                headings.add(found.group(1))
        refs = self.references()
        self.assertTrue(refs)
        for source, number, text in refs:
            self.assertIn(number, headings, f"{source}: «этап {number}» — нет такого заголовка: {text[:60]}")

    def test_references_to_documents_and_mod_rules_use_stage_4a(self):
        near_documents = re.compile(r"(?:Мод|БСО|документ)[^.\n]{0,60}?[Ээ]тап\w*\s+(\d+а?)")
        checked = 0
        for stage in load_stages():
            for text in iter_strings(stage):
                for match in near_documents.finditer(text):
                    checked += 1
                    self.assertEqual(match.group(1), "4а", f"{stage['id']}: {text[:80]}")
        for line in TECHDIR_TEMPLATE.read_text(encoding="utf-8").splitlines():
            for match in near_documents.finditer(line):
                checked += 1
                self.assertEqual(match.group(1), "4а", line[:80])
        self.assertGreaterEqual(checked, 2)


class ContentHonestyTests(unittest.TestCase):
    """Тексты гайда не пугают, не грубят и не обещают того, чего нет (spec: «Тон и правила текста»)."""

    def test_dust_step_gives_the_honest_reasons_and_no_scare_arguments(self):
        text = step_text(self, "s2-05").lower()
        self.assertNotRegex(text, r"возгора|сожж|х\s?20|x20|скуп")
        self.assertNotRegex(text, r"\d{4,5}\s*₽", "цены комплектующих компании в тексте не нужны")
        for needle in ("перегрев", "влаг", "статическ", "редк", "{{price_cleaning}}",
                       "не преувеличивай", "до и после"):
            self.assertIn(needle, text)

    def test_dust_step_compares_cleaning_with_the_price_of_parts_as_guidelines(self):
        # сравнение из брифа: чистка против блока питания и материнской платы с процессором; цены — переменные профиля
        text = step_text(self, "s2-05")
        wording = ("чистка раз-два в год стоит около {{price_cleaning}} ₽; замена блока питания — около {{price_psu}} ₽, "
                   "материнской платы — около {{price_board}} ₽ плюс процессор — около {{price_cpu}} ₽")
        self.assertIn(wording, text)
        self.assertIn("ориентир", text.lower(), "цены даны как ориентиры, а не как обещание")

    def test_router_step_does_not_pair_a_scare_with_a_sale(self):
        self.assertEqual(router_scare_problems(find_step(self, "s7-08")), [])

    def test_router_scare_check_is_not_vacuous(self):
        step = find_step(self, "s7-08")
        with_registry = {**step, "points": step["points"] + ["Дальше возможна чистка реестра, если клиент согласен"]}
        self.assertEqual(len(router_scare_problems(with_registry)), 1)
        for needed in ("безвредн", "настоящ"):
            softened = {**step, "points": [point.replace(needed, "x") for point in step["points"]]}
            self.assertEqual(len(router_scare_problems(softened)), 1, needed)
        without_journal = {**step, "points": [point for point in step["points"] if "журнал" not in point.lower()]}
        self.assertEqual(len(router_scare_problems(without_journal)), 1)

    def test_no_rude_judgements_about_clients(self):
        banned = re.compile(r"глуп|сердобол|тупо|дурац|идиот|жадн|скуп", re.IGNORECASE)
        for stage in load_stages():
            for text in iter_strings(stage):
                self.assertIsNone(banned.search(text), f"{stage['id']}: {text[:80]}")
        self.assertIsNone(banned.search(TECHDIR_TEMPLATE.read_text(encoding="utf-8")))

    def test_registering_and_filming_a_client_need_a_warning_and_consent(self):
        for step_id in ("s6-03", "s5-04"):
            text = step_text(self, step_id).lower()
            self.assertIn("соглас", text, step_id)
            self.assertIn("предупреди", text, step_id)

    def test_signed_document_is_not_promised_to_win_disputes(self):
        text = step_text(self, "s4b-05").lower()
        self.assertNotRegex(text, r"правот|\bсуд|судеб")
        self.assertIn("помогает в спорах", text)


class DocumentStepsTests(unittest.TestCase):
    """Этап 4а: вместо заглушек «выдаёт техдир» — то, что у мастера есть на самом деле (панель «Прайс и акт», единый бланк)."""

    PRICE_PHRASE = "прайс уже в приложении — кнопка «Прайс и акт» внизу экрана; цену выше прайса ставить нельзя"
    BLANK_PHRASE = ("один унифицированный бланк договора и актов (на месте и с забором) выдаёт {{techdir_nom}}; "
                    "заполняй его по правилам ниже")

    def test_the_two_old_placeholders_are_gone(self):
        text = (CONTENT_DIR / "45-documents.json").read_text(encoding="utf-8")
        self.assertNotIn("[прайс выдаёт техдир]", text)
        self.assertNotIn("[бланк БСО и акт забора — у техдира]", text)

    def test_services_table_sends_the_master_to_the_price_panel_of_the_page(self):
        self.assertIn(self.PRICE_PHRASE, step_text(self, "s4b-02"))
        template = (ROOT / "guide" / "template.html").read_text(encoding="utf-8")
        self.assertIn("'Прайс и акт'", template, "кнопки «Прайс и акт» на странице нет: подсказка в шаге врёт")

    def test_the_blank_is_one_unified_form_given_out_by_the_techdir_in_both_places_it_is_mentioned(self):
        for step_id in ("s4b-01", "s4b-06"):
            with self.subTest(step_id):
                self.assertIn(self.BLANK_PHRASE, step_text(self, step_id))


class TechdirNoteTests(unittest.TestCase):
    SECTION_RE = re.compile(r"^## (\d+)\. ", re.MULTILINE)

    def sections(self):
        text = read_repo_file(self, "guide/docs/for-techdir.md")
        parts = self.SECTION_RE.split(text)
        # parts: [преамбула, номер, тело, номер, тело, ...]
        return {int(parts[i]): parts[i + 1] for i in range(1, len(parts) - 1, 2)}

    def test_has_the_seven_sections_from_the_spec_in_order(self):
        text = read_repo_file(self, "guide/docs/for-techdir.md")
        numbers = [int(n) for n in self.SECTION_RE.findall(text)]
        self.assertEqual(numbers, [1, 2, 3, 4, 5, 6, 7])

    def test_every_section_marks_its_advice_as_a_recommendation(self):
        for number, body in self.sections().items():
            self.assertRegex(body.lower(), r"рекомендаци", f"раздел {number}")

    def test_third_section_has_a_ready_first_message_in_a_quote_block(self):
        body = self.sections().get(3, "")
        quoted = [line for line in body.splitlines() if line.startswith("> ")]
        self.assertGreaterEqual(len(quoted), 3, "готовый текст первого сообщения мастеру")

    def test_renders_with_demo_profile_and_keeps_no_template_marks(self):
        template = read_repo_file(self, "guide/docs/for-techdir.md")
        rendered = build.render_techdir(template, build.load_profile(str(DEMO_PROFILE)))
        self.assertNotIn("{{", rendered)
        self.assertIn("@dispatcher", rendered)

    def test_public_texts_do_not_advise_pressure_overpricing_or_faking_work(self):
        banned = re.compile(r"давить|давлени|завыш|имитир|накрут|обмануть", re.IGNORECASE)
        for rel in ("guide/docs/for-techdir.md", "README.md"):
            text = read_repo_file(self, rel)
            self.assertIsNone(banned.search(text), rel)


@unittest.skipUnless(shutil.which("git"), "git не найден")
class MarkerGuardTests(unittest.TestCase):
    """Слово из локального списка маркеров, попавшее в отслеживаемый файл, роняет проверку."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.git("init", "-q")
        self.make(".gitignore", "private/\n")
        self.make("private/markers.txt", "# впиши сюда по слову на строку\nzzcompanyzz\n")
        self.make("README.md", "Нейтральный текст без названий.\n")
        self.git("add", ".gitignore", "README.md")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def make(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def check(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = check_public.main(["--root", str(self.root)])
        return code, out.getvalue()

    def test_clean_tracked_files_pass_with_the_markers_file_present(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def test_marker_word_planted_in_a_tracked_file_fails_without_echoing_it(self):
        self.make("README.md", "Приложение ZZCompanyZZ ставь первым.\n")
        code, out = self.check()
        self.assertEqual(code, 1)
        self.assertIn("README.md:1", out)
        self.assertNotIn("zzcompanyzz", out.lower())

    def test_without_the_markers_file_the_same_text_passes_as_in_clean_ci(self):
        self.make("README.md", "Приложение ZZCompanyZZ ставь первым.\n")
        (self.root / "private" / "markers.txt").unlink()
        code, out = self.check()
        self.assertEqual(code, 0, out)


@unittest.skipUnless(PRIVATE_PROFILE.is_file(), "private/profile.json есть только у владельца репозитория")
class PrivateProfileTests(unittest.TestCase):
    def test_covers_every_variable_and_keeps_links_secure(self):
        profile = build.load_profile(str(PRIVATE_PROFILE))
        missing = (content_vars() | techdir_vars()) - set(profile["vars"])
        self.assertEqual(sorted(missing), [])
        for name, value in profile["vars"].items():
            if name.startswith("link_"):
                self.assertTrue(value.startswith("https://"), name)

    def test_strict_check_passes(self):
        code, _, err = run_build("--profile", str(PRIVATE_PROFILE), "--check")
        self.assertEqual(code, 0, err)

    def test_part_price_guidelines_are_filled_in_with_numbers_not_placeholders(self):
        values = build.load_profile(str(PRIVATE_PROFILE))["vars"]
        for name in ("price_psu", "price_board", "price_cpu"):
            self.assertRegex(values.get(name, ""), r"^\d+$", f"{name}: нужно число (значение в сообщение не попадает)")


class RepoFilesTests(unittest.TestCase):
    def test_license_is_mit_for_the_author_in_2026(self):
        text = read_repo_file(self, "LICENSE")
        self.assertTrue(text.startswith("MIT License"))
        self.assertIn("Copyright (c) 2026 Alpha-Oi", text)

    def test_changelog_follows_keep_a_changelog_and_has_the_first_release(self):
        text = read_repo_file(self, "CHANGELOG.md")
        self.assertIn("keepachangelog.com", text)
        self.assertRegex(text, r"(?m)^## \[0\.1\.0\] - \d{4}-\d{2}-\d{2}$")

    def test_changelog_lists_what_the_acceptance_review_changed_under_the_first_release(self):
        text = read_repo_file(self, "CHANGELOG.md")
        found = re.search(r"(?ms)^## \[0\.1\.0\].*?(?=^## \[|\Z)", text)
        self.assertIsNotNone(found, "нет раздела [0.1.0]")
        section = found.group(0)
        self.assertRegex(section, r"(?m)^### Исправлено", "в [0.1.0] нет подраздела «Исправлено» с итогами приёмки")
        fixes = section.split("### Исправлено", 1)[1]
        self.assertIn("приёмк", fixes)
        for needle in ("пыл", "Прайс и акт", "make_charts", "ADR 0001", "pull request"):
            self.assertIn(needle, fixes, f"в итогах приёмки не упомянуто: {needle}")
        self.assertGreaterEqual(len(re.findall(r"(?m)^- ", fixes)), 5)
        self.assertNotRegex(fixes, r"CI|Pages", "запись об исправлениях не обещает того, чего ещё нет (CI и Pages не запускались)")

    def test_adr_0001_describes_the_pricelist_the_way_the_build_treats_it(self):
        # build.py проверяет прайс и целиком кладёт его в данные страницы под ключом pricelist; в тексты он не подставляется
        text = " ".join(read_repo_file(self, "docs/adr/0001-контент-профиль-прайс.md").split())
        self.assertRegex(text, r"вшива\w+ в данные страницы под ключом `pricelist`")
        self.assertIn("не подставляется в тексты", text)
        self.assertRegex(text, r"`guide\.md`[^.]*раздел «Прайс»")

    def test_ci_runs_the_agreed_check_command_on_push_and_pull_request(self):
        text = read_repo_file(self, ".github/workflows/ci.yml")
        self.assertRegex(text, r"(?m)^\s+push:")
        self.assertRegex(text, r"(?m)^\s+pull_request:")
        self.assertRegex(text, r"python-version:\s*[\"']3\.12[\"']")
        self.assertIn(CHECK_COMMAND, " ".join(text.split()))
        self.assertNotIn("private/", text)

    def test_pages_workflow_deploys_dist_from_main_with_pinned_actions(self):
        text = read_repo_file(self, ".github/workflows/pages.yml")
        self.assertRegex(text, r"branches:\s*\[\s*main\s*\]")
        self.assertIn("python guide/build.py", text)
        self.assertRegex(text, r"actions/upload-pages-artifact@v\d+\.\d+\.\d+")
        self.assertRegex(text, r"actions/deploy-pages@v\d+\.\d+\.\d+")

    def test_every_action_in_both_workflows_has_a_pinned_version(self):
        for rel in (".github/workflows/ci.yml", ".github/workflows/pages.yml"):
            text = read_repo_file(self, rel)
            uses = re.findall(r"uses:\s*(\S+)", text)
            self.assertTrue(uses, rel)
            for ref in uses:
                self.assertRegex(ref, r"@v\d+\.\d+\.\d+$", f"{rel}: {ref}")

    def test_workflows_are_valid_yaml_when_a_yaml_parser_is_available(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML не установлен: проверка синтаксиса YAML пропущена")
        for rel in (".github/workflows/ci.yml", ".github/workflows/pages.yml"):
            document = yaml.safe_load(read_repo_file(self, rel))
            self.assertIn("jobs", document, rel)
            # у PyYAML ключ «on» читается как True
            self.assertTrue("on" in document or True in document, rel)


class ChartCheckInDocumentsTests(unittest.TestCase):
    """Проверка актуальности графика стоит в команде проверки и описана в документах."""

    def test_documents_carry_the_full_check_command_with_the_chart_step(self):
        for rel in ("README.md", "CONTRIBUTING.md"):
            with self.subTest(rel):
                text = " ".join(read_repo_file(self, rel).split())
                self.assertIn(DOCUMENT_CHECK_COMMAND, text)

    def test_contributing_powershell_block_runs_the_chart_check_too(self):
        text = read_repo_file(self, "CONTRIBUTING.md")
        powershell = "\n".join(re.findall(r"```powershell\r?\n(.*?)```", text, re.DOTALL))
        self.assertIn("python tools/make_charts.py --check", powershell)

    def test_contributing_says_how_to_rebuild_the_chart_after_a_content_change(self):
        text = read_repo_file(self, "CONTRIBUTING.md")
        self.assertRegex(text, r"`python tools/make_charts\.py`")
        self.assertRegex(text, r"`python tools/make_charts\.py --check`")
        self.assertIn("docs/img/steps-by-stage.svg", text)

    def test_pull_request_template_checklist_carries_the_full_check_command(self):
        text = " ".join(read_repo_file(self, ".github/PULL_REQUEST_TEMPLATE.md").split())
        self.assertIn(DOCUMENT_CHECK_COMMAND, text)

    def test_architecture_and_privacy_describe_the_chart_script_and_the_images_folder(self):
        for rel in ("docs/architecture.md", "docs/privacy.md"):
            with self.subTest(rel):
                text = read_repo_file(self, rel)
                self.assertIn("tools/make_charts.py", text)
                self.assertIn("docs/img/", text)
                self.assertIn("steps-by-stage.svg", text)


# файлы документов проекта пишет отдельная задача; существование этих файлов проверяют её тесты,
# здесь проверяется только то, что README на них ссылается
DOCUMENT_FILES = frozenset({
    "CONTRIBUTING.md", "SECURITY.md", "CODE_OF_CONDUCT.md",
    "docs/architecture.md", "docs/pricelist.md", "docs/privacy.md", "docs/deploy.md",
})
# картинки витрины: имена из задачи; каждая лежит в docs/img/ и показана в README
SHOWCASE_IMAGES = (
    "hero-phone-360.png", "stages-360.png", "cheatsheet-360.png", "report-360.png", "price-360.png",
    "dark-360.png", "desktop-1024.png", "social-preview.png", "steps-by-stage.svg",
)
# ширина снимка в пикселях: страница снята при ширине окна 360 или 1024 CSS-пикселей с плотностью 1 или 2
PICTURE_WIDTHS = {"360": (360, 720), "1024": (1024, 2048)}
BADGE_HOSTS = ("github.com", "img.shields.io")

FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
MERMAID_RE = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)


def strip_code(text):
    """Текст без блоков и фрагментов кода: в них скобки и квадратные заглушки — не ссылки."""
    return INLINE_CODE_RE.sub("", FENCE_RE.sub("", text))


def readme_images(text):
    """(описание, адрес) каждой картинки вне блоков кода в порядке появления: ![описание](адрес) и <img src alt>."""
    found = []
    for match in re.finditer(r"!\[([^\]]*)\]\(([^)\s]+)\)|<img\b[^>]*>", strip_code(text)):
        if match.group(2) is not None:
            found.append((match.group(1), match.group(2)))
            continue
        src = re.search(r'\bsrc="([^"]*)"', match.group(0))
        alt = re.search(r'\balt="([^"]*)"', match.group(0))
        found.append((alt.group(1) if alt else "", src.group(1) if src else ""))
    return found


def readme_targets(text):
    """Адреса всех ссылок и картинок вне блоков кода: ](адрес) и href="адрес"."""
    text = strip_code(text)
    return re.findall(r"\]\(([^)\s]+)\)", text) + re.findall(r'\bhref="([^"]*)"', text)


def local_path(target):
    """Путь к файлу репозитория без #якоря; None для внешних адресов, mailto и якорей внутри страницы."""
    if target.startswith(("http://", "https://", "mailto:", "#")):
        return None
    return target.split("#", 1)[0] or None


def heading_slugs(text):
    """Якоря заголовков так, как их строит GitHub: строчные буквы, без знаков препинания, пробел → дефис."""
    slugs = set()
    for title in re.findall(r"(?m)^#{1,6}\s+(.+?)\s*$", FENCE_RE.sub("", text)):
        slugs.add(re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-"))
    return slugs


def mermaid_problems(block):
    """Простая проверка того, что ломает Mermaid чаще всего: кавычки, скобки, «;» и «#», незнакомые участники.

    Настоящий разбор делает сам Mermaid на GitHub; эта проверка ловит опечатки до публикации.
    """
    problems = []
    lines = [line for line in block.splitlines() if line.strip()]
    kind = lines[0].split()[0] if lines else ""
    declared = set(re.findall(r"(?m)^\s*(?:actor|participant)\s+(\w+)", block))
    for number, line in enumerate(lines, start=1):
        if line.count('"') % 2:
            problems.append(f"строка {number}: нечётное число кавычек")
        outside = re.sub(r'"[^"]*"', "", line)
        for opening, closing in ("[]", "()", "{}"):
            if outside.count(opening) != outside.count(closing):
                problems.append(f"строка {number}: не закрыта скобка {opening}")
        if ";" in line or "#" in line:
            problems.append(f"строка {number}: «;» или «#» ломают разбор")
        if re.search(r'\[(?!["\[(/\\])[^\]]*[^\x00-\x7f]', line):
            problems.append(f"строка {number}: русский текст в скобках надо заключить в кавычки")
        if kind == "sequenceDiagram":
            arrow = re.match(r"\s*(\w+)\s*(?:-->>|->>|--\)|-\)|-->|->)\s*(\w+)\s*:\s*(.*)$", line)
            if arrow:
                for who in arrow.group(1, 2):
                    if who not in declared:
                        problems.append(f"строка {number}: участник {who} не объявлен")
                if not arrow.group(3).strip():
                    problems.append(f"строка {number}: у сообщения нет текста")
    return problems


class ReadmeHelperTests(unittest.TestCase):
    """Разборщики README проверяются на коротких примерах: иначе тесты README могли бы ничего не находить."""

    def test_images_are_found_in_markdown_and_html_but_not_in_code(self):
        text = '![один](a.png)\n<img src="b.png" width="3" alt="два">\n`![нет](c.png)`\n```\n![нет](d.png)\n```\n'
        self.assertEqual(readme_images(text), [("один", "a.png"), ("два", "b.png")])

    def test_targets_include_badge_links_and_html_links_but_not_code(self):
        text = '[![CI](https://x.test/b.svg)](LICENSE) [док](docs/a.md#раздел) <a href="B.md">б</a> `[нет](c.md)`'
        self.assertEqual(readme_targets(text), ["https://x.test/b.svg", "LICENSE", "docs/a.md#раздел", "B.md"])

    def test_local_path_drops_anchors_and_skips_outside_addresses(self):
        self.assertEqual(local_path("docs/a.md#раздел"), "docs/a.md")
        for outside in ("https://x.test/a", "http://x.test", "mailto:a@b.test", "#раздел"):
            self.assertIsNone(local_path(outside))

    def test_heading_slugs_follow_github_rules(self):
        text = "# Гайд для мастеров\n## Быстрый старт\n### Что нужно? (кратко)\n```\n# не заголовок\n```\n"
        self.assertEqual(heading_slugs(text), {"гайд-для-мастеров", "быстрый-старт", "что-нужно-кратко"})

    def test_mermaid_lint_accepts_a_good_flowchart_and_sequence(self):
        flow = 'flowchart LR\n    a["Текст<br/>файл.json"] --> b(["Сборка"])\n'
        sequence = "sequenceDiagram\n    actor M as Мастер\n    participant A as Приложение\n    M->>A: Принять\n"
        self.assertEqual(mermaid_problems(flow), [])
        self.assertEqual(mermaid_problems(sequence), [])

    def test_mermaid_lint_catches_the_usual_breakages(self):
        cases = {
            "нечётное число кавычек": 'flowchart LR\n    a["Текст] --> b\n',
            "не закрыта скобка": 'flowchart LR\n    a["Текст" --> b\n',
            "«;» или «#»": 'flowchart LR\n    a["Один; два"] --> b\n',
            "в кавычки": "flowchart LR\n    a[Русский текст] --> b\n",
            "не объявлен": "sequenceDiagram\n    actor M as Мастер\n    M->>X: Привет\n",
            "нет текста": "sequenceDiagram\n    actor M as Мастер\n    participant A as Приложение\n    M->>A:\n",
        }
        for fragment, block in cases.items():
            with self.subTest(fragment):
                self.assertTrue(any(fragment in problem for problem in mermaid_problems(block)), mermaid_problems(block))


class ReadmeTests(unittest.TestCase):
    def readme(self):
        return read_repo_file(self, "README.md")

    def test_names_the_commands_and_paths_a_user_needs(self):
        text = self.readme()
        for needle in (
            "python guide/build.py",
            "--profile private/profile.json",
            "--pricelist",
            "private/pricelist.json",
            "demo-pricelist.json",
            "tools/check_public.py",
            "tools/make_charts.py",
            "private/markers.txt",
            "guide/content",
            "MIT",
            "docs/img/hero-phone-360.png",
        ):
            self.assertIn(needle, text)

    def test_has_a_short_english_part_and_says_the_demo_content_is_an_example(self):
        text = self.readme()
        self.assertRegex(text, r"(?m)^## English")
        self.assertRegex(text, r"(?i)example")
        self.assertRegex(text, r"(?i)демо.{0,80}пример|пример.{0,80}демо")

    def test_english_summary_is_the_last_part_and_is_8_to_10_lines_long(self):
        english = self.readme().split("\n## English\n", 1)[1]
        lines = [line for line in english.splitlines() if line.strip()]
        self.assertTrue(8 <= len(lines) <= 10, f"в английской части {len(lines)} строк, нужно 8–10")
        self.assertNotRegex(english, r"(?m)^#{1,6} ", "после английской части не должно быть новых разделов")
        self.assertIsNone(re.search(r"[а-яё]{4,}", english.replace("Качество", "")), "английская часть не должна быть по-русски")

    def test_profile_instructions_work_on_a_fresh_clone_in_bash_and_powershell(self):
        text = self.readme()
        self.assertNotIn("(в Windows — `copy`)", text)
        bash_mkdir, bash_copy = "mkdir -p private", "cp guide/profiles/demo.json private/profile.json"
        ps_mkdir, ps_copy = "New-Item -ItemType Directory -Force private", "Copy-Item guide/profiles/demo.json private/profile.json"
        for command in (bash_mkdir, bash_copy, ps_mkdir, ps_copy):
            self.assertIn(command, text)
        # папку нужно создать до копирования в ней файла
        self.assertLess(text.index(bash_mkdir), text.index(bash_copy))
        self.assertLess(text.index(ps_mkdir), text.index(ps_copy))

    def test_quick_start_is_three_commands_that_are_the_same_in_bash_and_powershell(self):
        text = self.readme()
        start = text.index("## Быстрый старт")
        block = FENCE_RE.search(text, start).group(0).strip("`\n ")
        commands = [line for line in block.splitlines() if line.strip()]
        self.assertEqual(len(commands), 3, commands)
        self.assertEqual(commands[-1], "python guide/build.py")
        for command in commands:
            self.assertNotIn("&&", command, "в PowerShell 5 «&&» не работает")
            self.assertNotIn("$", command)

    def test_every_picture_in_docs_img_is_used_by_the_readme(self):
        text = self.readme()
        pictures = sorted(p for p in (ROOT / "docs" / "img").iterdir() if p.suffix in (".png", ".svg"))
        self.assertTrue(pictures)
        for path in pictures:
            self.assertIn(f"docs/img/{path.name}", text, f"{path.name} не нужен README — удали его")

    def test_there_are_no_pictures_in_docs_outside_docs_img(self):
        stray = [p.relative_to(ROOT).as_posix() for p in (ROOT / "docs").rglob("*")
                 if p.suffix.lower() in (".png", ".svg", ".jpg", ".jpeg", ".gif") and (ROOT / "docs" / "img") not in p.parents]
        self.assertEqual(stray, [], "картинки живут только в docs/img/")

    def test_all_showcase_pictures_exist_and_have_the_agreed_sizes(self):
        for name in SHOWCASE_IMAGES:
            self.assertTrue((ROOT / "docs" / "img" / name).is_file(), f"нет docs/img/{name}")
        for path in sorted((ROOT / "docs" / "img").glob("*.png")):
            header = path.read_bytes()[:24]
            self.assertEqual(header[:8], PNG_SIGNATURE, path.name)
            width, height = struct.unpack(">II", header[16:24])
            if path.name == "social-preview.png":
                self.assertEqual((width, height), (1280, 640), "карточка репозитория — 1280×640")
                self.assertLess(path.stat().st_size, 1_000_000, "GitHub принимает карточку до 1 МБ")
                continue
            for label, widths in PICTURE_WIDTHS.items():
                if f"-{label}." in path.name:
                    self.assertIn(width, widths, path.name)
            self.assertGreater(height, width // 2, path.name)

    def test_every_local_picture_it_embeds_exists_has_a_description_and_is_a_real_file(self):
        images = readme_images(self.readme())
        local = [(alt, src) for alt, src in images if not src.startswith(("http://", "https://"))]
        self.assertIn("docs/img/hero-phone-360.png", [src for _, src in local])
        for alt, src in local:
            self.assertTrue(alt.strip(), f"у картинки {src} нет описания")
            path = ROOT / src
            self.assertTrue(path.is_file(), src)
            if path.suffix == ".png":
                header = path.read_bytes()[:24]
                self.assertEqual(header[:8], PNG_SIGNATURE, src)
                width, height = struct.unpack(">II", header[16:24])
                self.assertGreater(width, 200, src)
                self.assertGreater(height, 200, src)
            elif path.suffix == ".svg":
                ET.fromstring(path.read_bytes())
            else:
                self.fail(f"неожиданный вид картинки: {src}")

    def test_the_main_picture_comes_first_and_the_chart_and_social_card_are_mentioned(self):
        text = self.readme()
        local = [src for _, src in readme_images(text) if not src.startswith("http")]
        self.assertEqual(local[0], "docs/img/hero-phone-360.png")
        self.assertIn("docs/img/steps-by-stage.svg", local)
        self.assertIn("docs/img/social-preview.png", text)

    def test_remote_images_are_only_the_badges_and_published_links_are_marked_as_not_working_yet(self):
        text = self.readme()
        remote = [src for _, src in readme_images(text) if src.startswith("http")]
        self.assertTrue(remote)
        for src in remote:
            self.assertTrue(any(f"//{host}/" in src for host in BADGE_HOSTS), src)
        for address in ("https://github.com/Alpha-Oi/field-tech-guide", "https://alpha-oi.github.io/field-tech-guide/"):
            self.assertIn(address, text)
        self.assertIn("заработают после публикации", text)

    def test_every_local_link_points_to_an_existing_file_or_a_project_document(self):
        missing = []
        for target in readme_targets(self.readme()):
            path = local_path(target)
            if path and path not in DOCUMENT_FILES and not (ROOT / path).exists():
                missing.append(target)
        self.assertEqual(missing, [])

    def test_links_to_every_project_document_are_present(self):
        linked = {local_path(target) for target in readme_targets(self.readme())}
        self.assertEqual(sorted(DOCUMENT_FILES - linked), [])

    def test_links_to_a_section_of_the_page_point_to_an_existing_heading(self):
        text = self.readme()
        slugs = heading_slugs(text)
        anchors = [target[1:] for target in readme_targets(text) if target.startswith("#")]
        self.assertTrue(anchors)
        for anchor in anchors:
            self.assertIn(anchor, slugs)

    def test_has_three_mermaid_diagrams_that_pass_the_lint(self):
        blocks = MERMAID_RE.findall(self.readme())
        self.assertEqual(len(blocks), 3)
        kinds = [block.split()[0] for block in blocks]
        self.assertIn(kinds[0], ("flowchart", "graph"))
        self.assertIn(kinds[1], ("flowchart", "graph"))
        self.assertEqual(kinds[2], "sequenceDiagram")
        for number, block in enumerate(blocks, start=1):
            self.assertEqual(mermaid_problems(block), [], f"диаграмма {number}")

    def test_data_flow_diagram_shows_inputs_build_and_the_three_outputs(self):
        block = MERMAID_RE.findall(self.readme())[0]
        for needle in ("guide/content/*.json", "Профиль", "Прайс", "guide/build.py", "index.html", "guide.md", "for-techdir.md"):
            self.assertIn(needle, block)
        # стрелки идут от входов к сборке и от неё к результатам, а не наоборот
        self.assertRegex(block, r"content\S*\s*-->\s*build")
        self.assertRegex(block, r"build\s*-->\s*page")

    def test_stage_map_has_exactly_the_stage_titles_of_the_content_in_order(self):
        block = MERMAID_RE.findall(self.readme())[1]
        self.assertEqual(re.findall(r'"(Этап [^"]+)"', block), [stage["title"] for stage in load_stages()])

    def test_order_lifecycle_has_the_four_participants_and_the_real_button_names(self):
        block = MERMAID_RE.findall(self.readme())[2]
        for participant in ("Мастер", "Приложение службы", "Диспетчер", "Чат мастера"):
            self.assertRegex(block, rf"(?:actor|participant)\s+\w+\s+as\s+{participant}\b")
        chain_text = "\n".join(iter_strings(build.load_content(str(CONTENT_DIR))["cheatsheet"]["chain"]))
        for name in ("Принять", "Я на месте", "В Работе", "Сдать Заявку", "В Пути"):
            self.assertIn(name, chain_text, f"в памятке нет «{name}»: диаграмма выдумывает кнопку")
            self.assertIn(name, block, f"на диаграмме нет «{name}»")
        self.assertIn("Отчёт", block)

    def test_workflows_are_described_as_configured_not_as_running(self):
        for rel in ("README.md", "CHANGELOG.md"):
            text = read_repo_file(self, rel)
            self.assertIn("настроены", text, rel)
            self.assertIn("после публикации", text, rel)
            self.assertNotIn("v0.1.0", text, rel)

    def test_states_the_real_number_of_tests(self):
        stated = re.search(r"\*\*Тесты: (\d+)\.\*\*", self.readme())
        self.assertIsNotNone(stated, "в разделе «Качество» нет строки «**Тесты: N.**»")
        loaded = unittest.defaultTestLoader.discover(str(ROOT / "guide" / "tests"), top_level_dir=str(ROOT))
        self.assertEqual(
            int(stated.group(1)), loaded.countTestCases(),
            "число тестов в README разошлось с настоящим: запусти проверку целиком («Ran N tests») и впиши N",
        )

    def test_has_no_company_words_or_numbers(self):
        forbidden = real_forbidden(self)
        self.assertEqual(forbidden_in_files([ROOT / "README.md"], forbidden, collapse_thousands=True), [])

    def test_has_no_local_paths_and_does_not_use_the_full_job_title(self):
        text = self.readme()
        self.assertNotIn("D" + ":\\", text)
        self.assertNotIn("C" + ":\\Us" + "ers", text)
        self.assertIsNone(re.search(r"(?i)технический\s+директор", text), "в README пишут «техдир»")


class DecisionRecordsLinksTests(unittest.TestCase):
    """README, CONTRIBUTING.md и docs/architecture.md ведут к записям решений (docs/adr/) и к словарю проекта (CONTEXT.md)."""

    DOCUMENTS = ("README.md", "CONTRIBUTING.md", "docs/architecture.md")
    REQUIRED = ("docs/adr/README.md", "CONTEXT.md")

    def link_targets(self, rel):
        """Локальные ссылки файла как пути от корня репозитория (относительные ссылки считаются от папки файла)."""
        base = (ROOT / rel).parent
        found = set()
        for target in readme_targets(read_repo_file(self, rel)):
            path = local_path(target)
            if path:
                found.add(Path(os.path.normpath(base / path)).relative_to(ROOT).as_posix())
        return found

    def test_each_document_links_to_the_adr_index_and_the_project_glossary(self):
        for rel in self.DOCUMENTS:
            with self.subTest(rel):
                linked = self.link_targets(rel)
                self.assertEqual(sorted(set(self.REQUIRED) - linked), [], f"{rel}: нет ссылок на эти файлы")
                for name in self.REQUIRED:
                    self.assertTrue((ROOT / name).is_file(), f"{rel}: ссылка ведёт в никуда, нет файла {name}")


if __name__ == "__main__":
    unittest.main()
