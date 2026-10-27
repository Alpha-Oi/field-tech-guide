"""Тесты прайса в движке сборки: загрузка, проверка схемы (interfaces.md, U7), раздел в guide.md,
ключ `pricelist` в данных страницы, флаги командной строки.

Ожидаемые значения разобраны вручную по фикстуре `fixtures/pricelist.json` и схеме U7,
а не получены из кода сборки.
"""

import contextlib
import copy
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guide import build

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONTENT = FIXTURES / "content"
PROFILE = FIXTURES / "profile.json"
TEMPLATE = FIXTURES / "template.html"
PRICELIST = FIXTURES / "pricelist.json"
ROOT = Path(__file__).resolve().parents[2]
DEMO_PRICELIST = ROOT / "guide" / "profiles" / "demo-pricelist.json"
REAL_TEMPLATE = ROOT / "guide" / "template.html"

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def fixture_pricelist() -> dict:
    return build.load_pricelist(str(PRICELIST))


def guide_data() -> dict:
    content = build.load_content(str(CONTENT))
    profile = build.load_profile(str(PROFILE))
    return build.apply_profile(content, profile)


def run_cli(args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = build.main(list(args))
    return code, out.getvalue(), err.getvalue()


def page_data(html: str) -> dict:
    blocks = re.findall(r'<script type="application/json" id="guide-data">(.*?)</script>', html, flags=re.S)
    assert len(blocks) == 1, "в странице должен быть ровно один блок #guide-data"
    return json.loads(blocks[0])


def cli_args(out_dir, *extra):
    return ["--content", str(CONTENT), "--profile", str(PROFILE), "--template", str(TEMPLATE), "--out", str(out_dir),
            *extra]


class LoadPricelistTests(unittest.TestCase):
    def test_fixture_is_read_as_written(self):
        pricelist = fixture_pricelist()
        self.assertEqual(pricelist["title"], "Тестовый прайс")
        self.assertEqual(pricelist["currency"], "₽")
        self.assertEqual([item["id"] for item in pricelist["items"]],
                         ["p001", "p002", "p003", "p004", "p005", "p006", "p007"])
        self.assertEqual(pricelist["matrices"][0]["rows"][0]["prices"],
                         {"c1": 9900, "c2": "ask", "c3": None, "c4": {"hint": "от 2900 ₽", "from": 2900}})

    def test_missing_file_is_content_error_in_russian(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(build.ContentError) as ctx:
            build.load_pricelist(str(Path(tmp) / "nope.json"))
        self.assertIn("Прайс не найден", str(ctx.exception))

    def test_broken_json_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken-price.json"
            path.write_text("{ не json", encoding="utf-8")
            with self.assertRaises(build.ContentError) as ctx:
                build.load_pricelist(str(path))
        self.assertIn("broken-price.json", str(ctx.exception))

    def test_root_must_be_an_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.json"
            path.write_text("[1, 2]", encoding="utf-8")
            with self.assertRaises(build.ContentError) as ctx:
                build.load_pricelist(str(path))
        self.assertIn("объект", str(ctx.exception))


class ValidatePricelistTests(unittest.TestCase):
    def setUp(self):
        self.pricelist = fixture_pricelist()

    def errors_after(self, mutate):
        pricelist = copy.deepcopy(self.pricelist)
        mutate(pricelist)
        return build.validate_pricelist(pricelist)

    def item(self, pricelist, item_id):
        return next(item for item in pricelist["items"] if item["id"] == item_id)

    def test_fixture_is_valid(self):
        self.assertEqual(build.validate_pricelist(self.pricelist), [])

    def test_fixture_has_all_four_kinds_and_three_kinds_of_cells(self):
        self.assertEqual({item["kind"] for item in self.pricelist["items"]}, {"fixed", "from", "ask", "na"})
        cells = [row["prices"].get(column["id"]) for matrix in self.pricelist["matrices"]
                 for row in matrix["rows"] for column in matrix["columns"]]
        self.assertTrue(any(isinstance(cell, int) for cell in cells))
        self.assertIn("ask", cells)
        self.assertIn(None, cells)
        hints = [cell for cell in cells if isinstance(cell, dict)]
        self.assertEqual(sorted(cell["from"] is None for cell in hints), [False, True])

    def test_all_errors_are_russian_strings(self):
        broken = copy.deepcopy(self.pricelist)
        broken["items"][0]["kind"] = "free"
        broken["items"][1]["price"] = None
        broken["items"][2]["name"] = ""
        broken["matrices"][0]["rows"][0]["prices"]["zz"] = 5
        errors = build.validate_pricelist(broken)
        self.assertGreaterEqual(len(errors), 4)
        for message in errors:
            with self.subTest(message=message):
                self.assertIsInstance(message, str)
                self.assertRegex(message, CYRILLIC)

    def test_duplicate_item_id_is_named(self):
        errors = self.errors_after(lambda pl: pl["items"][1].update(id="p001"))
        self.assertEqual(len(errors), 1)
        self.assertIn("p001", errors[0])
        self.assertIn("повторяется", errors[0])

    def test_unknown_kind_is_named(self):
        errors = self.errors_after(lambda pl: self.item(pl, "p002").update(kind="free"))
        self.assertEqual(len(errors), 1)
        self.assertIn("p002", errors[0])
        self.assertIn("free", errors[0])

    def test_fixed_and_from_need_a_whole_price_from_zero(self):
        for kind, item_id in (("fixed", "p002"), ("from", "p005")):
            for bad in (None, -1, 10.5, 1500.0, "1500", True):
                with self.subTest(kind=kind, price=bad):
                    errors = self.errors_after(lambda pl: self.item(pl, item_id).update(price=bad))
                    self.assertEqual(len(errors), 1, errors)
                    self.assertIn(item_id, errors[0])
                    self.assertIn("price", errors[0])

    def test_missing_price_of_fixed_is_an_error(self):
        def drop(pricelist):
            del self.item(pricelist, "p002")["price"]
        errors = self.errors_after(drop)
        self.assertEqual(len(errors), 1)
        self.assertIn("p002", errors[0])

    def test_zero_price_is_allowed_for_fixed_and_from(self):
        self.assertEqual(self.errors_after(lambda pl: self.item(pl, "p002").update(price=0)), [])
        self.assertEqual(self.errors_after(lambda pl: self.item(pl, "p005").update(price=0)), [])

    def test_ask_and_na_must_not_have_a_numeric_price(self):
        for item_id in ("p006", "p007"):
            for bad in (0, 100):
                with self.subTest(item=item_id, price=bad):
                    errors = self.errors_after(lambda pl: self.item(pl, item_id).update(price=bad))
                    self.assertEqual(len(errors), 1, errors)
                    self.assertIn(item_id, errors[0])
                    self.assertIn("null", errors[0])

    def test_empty_name_is_an_error(self):
        for bad in ("", "   ", None):
            with self.subTest(name=bad):
                errors = self.errors_after(lambda pl: self.item(pl, "p003").update(name=bad))
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("p003", errors[0])
                self.assertIn("name", errors[0])

    def test_number_must_be_a_string(self):
        errors = self.errors_after(lambda pl: self.item(pl, "p003").update(no=14))
        self.assertEqual(len(errors), 1)
        self.assertIn("p003", errors[0])
        self.assertIn("no", errors[0])

    def test_group_and_description_types(self):
        self.assertEqual(len(self.errors_after(lambda pl: self.item(pl, "p003").update(group=""))), 1)
        self.assertEqual(len(self.errors_after(lambda pl: self.item(pl, "p003").update(description=5))), 1)
        self.assertEqual(self.errors_after(lambda pl: self.item(pl, "p003").update(description="")), [])

    def test_unknown_fields_are_errors(self):
        errors = self.errors_after(lambda pl: self.item(pl, "p001").update(discount=5))
        self.assertEqual(len(errors), 1)
        self.assertIn("p001", errors[0])
        self.assertIn("discount", errors[0])
        top = self.errors_after(lambda pl: pl.update(owner="кто-то"))
        self.assertEqual(len(top), 1)
        self.assertIn("owner", top[0])
        matrix = self.errors_after(lambda pl: pl["matrices"][0].update(extra=1))
        self.assertEqual(len(matrix), 1)
        self.assertIn("m1", matrix[0])
        self.assertIn("extra", matrix[0])

    def test_price_key_that_is_not_a_column_is_named(self):
        errors = self.errors_after(lambda pl: pl["matrices"][0]["rows"][0]["prices"].update(c9=1000))
        self.assertEqual(len(errors), 1)
        self.assertIn("m1", errors[0])
        self.assertIn("r1", errors[0])
        self.assertIn("c9", errors[0])

    def test_cell_values(self):
        for good in (0, 1, 14500, "ask", None):
            with self.subTest(cell=good):
                errors = self.errors_after(lambda pl: pl["matrices"][0]["rows"][0]["prices"].update(c1=good))
                self.assertEqual(errors, [])
        for bad in (-5, 1.5, "free", "", True, [], {}):
            with self.subTest(cell=bad):
                errors = self.errors_after(lambda pl: pl["matrices"][0]["rows"][0]["prices"].update(c1=bad))
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("r1", errors[0])
                self.assertIn("c1", errors[0])

    def test_hint_cells(self):
        for good in ({"hint": "от 2900 ₽", "from": 2900}, {"hint": "700 ₽ + 100 ₽ за клавишу", "from": None},
                     {"hint": "по осмотру", "from": 0}, {"hint": "по осмотру"}):
            with self.subTest(cell=good):
                errors = self.errors_after(lambda pl: pl["matrices"][0]["rows"][0]["prices"].update(c1=good))
                self.assertEqual(errors, [])
        bad_cells = (
            {}, {"from": 100}, {"hint": ""}, {"hint": "   "}, {"hint": 5}, {"hint": None},
            {"hint": "x", "from": -1}, {"hint": "x", "from": 1.5}, {"hint": "x", "from": "100"},
            {"hint": "x", "from": True}, {"hint": "x", "extra": 1}, {"hint": "цена {{app_name}}", "from": None},
        )
        for bad in bad_cells:
            with self.subTest(cell=bad):
                errors = self.errors_after(lambda pl: pl["matrices"][0]["rows"][0]["prices"].update(c1=bad))
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("r1", errors[0])
                self.assertIn("c1", errors[0])
                self.assertRegex(errors[0], CYRILLIC)

    def test_matrix_without_prices_means_nothing_is_available(self):
        def drop(pricelist):
            del pricelist["matrices"][0]["rows"][1]["prices"]
        self.assertEqual(self.errors_after(drop), [])

    def test_duplicate_matrix_column_and_row_ids(self):
        matrix_dup = self.errors_after(lambda pl: pl["matrices"].append(copy.deepcopy(pl["matrices"][0])))
        self.assertTrue(any("m1" in message and "повторяется" in message for message in matrix_dup), matrix_dup)
        column_dup = self.errors_after(lambda pl: pl["matrices"][0]["columns"][1].update(id="c1"))
        self.assertTrue(any("c1" in message and "повторяется" in message for message in column_dup), column_dup)
        row_dup = self.errors_after(lambda pl: pl["matrices"][0]["rows"][1].update(id="r1"))
        self.assertTrue(any("r1" in message and "повторяется" in message for message in row_dup), row_dup)

    def test_matrix_names_and_labels_must_not_be_empty(self):
        self.assertEqual(len(self.errors_after(lambda pl: pl["matrices"][0]["columns"][0].update(name=""))), 1)
        self.assertEqual(len(self.errors_after(lambda pl: pl["matrices"][0]["rows"][0].update(label=" "))), 1)
        self.assertEqual(len(self.errors_after(lambda pl: pl["matrices"][0].update(title=""))), 1)

    def test_items_must_be_a_list_and_something_must_be_there(self):
        self.assertTrue(self.errors_after(lambda pl: pl.update(items="нет")))
        empty = self.errors_after(lambda pl: pl.update(items=[], matrices=[]))
        self.assertTrue(any("ни позиций" in message for message in empty), empty)

    def test_matrices_alone_are_enough(self):
        self.assertEqual(self.errors_after(lambda pl: pl.update(items=[])), [])

    def test_profile_tokens_are_not_allowed(self):
        errors = self.errors_after(lambda pl: self.item(pl, "p003").update(name="Диагностика {{app_name}}"))
        self.assertEqual(len(errors), 1)
        self.assertIn("p003", errors[0])
        self.assertIn("{{", errors[0])

    def test_not_an_object(self):
        errors = build.validate_pricelist([1])
        self.assertEqual(len(errors), 1)
        self.assertRegex(errors[0], CYRILLIC)

    def test_title_and_currency_are_required(self):
        self.assertEqual(len(self.errors_after(lambda pl: pl.update(title=""))), 1)
        self.assertEqual(len(self.errors_after(lambda pl: pl.pop("currency"))), 1)
        self.assertEqual(self.errors_after(lambda pl: pl.pop("note")), [])

    def test_validate_content_also_checks_the_pricelist(self):
        data = guide_data()
        data["pricelist"] = fixture_pricelist()
        self.assertEqual(build.validate(data), [])
        data["pricelist"]["items"][0]["kind"] = "free"
        errors = build.validate(data)
        self.assertEqual(len(errors), 1)
        self.assertIn("p001", errors[0])


class PricelistInContentPipelineTests(unittest.TestCase):
    def test_apply_profile_leaves_the_pricelist_untouched(self):
        data = {"stages": [], "cheatsheet": None, "pricelist": fixture_pricelist()}
        data["pricelist"]["items"][0]["name"] = "Выезд {{unknown_var}}"
        result = build.apply_profile(data, {"name": "x", "vars": {}})
        self.assertEqual(result["pricelist"]["items"][0]["name"], "Выезд {{unknown_var}}")
        self.assertEqual(build.collect_vars(data), [])


class RenderMdPricelistTests(unittest.TestCase):
    def setUp(self):
        self.data = guide_data()
        self.without = build.render_md(self.data)
        self.with_price = build.render_md({**self.data, "pricelist": fixture_pricelist()})

    def test_without_pricelist_nothing_changes(self):
        self.assertNotIn("Прайс", self.without)
        self.assertEqual(build.render_md({**self.data, "pricelist": None}), self.without)
        self.assertTrue(self.with_price.startswith(self.without.rstrip("\n")))

    def test_section_header_and_note(self):
        self.assertIn("\n## Прайс\n", self.with_price)
        self.assertIn("Тестовый прайс", self.with_price)
        self.assertIn("Цены вымышленные, только для тестов. Комплектующие не входят.", self.with_price)

    def test_items_are_grouped_and_priced(self):
        text = self.with_price
        self.assertIn("### Выезд и консультации", text)
        self.assertIn("### Диагностика", text)
        self.assertIn("### Настройка", text)
        self.assertIn("| 1 | Выезд мастера | 0 ₽ | Выезд по городу. |", text)
        self.assertIn("| 14.1 | Диагностика компьютера | 1000 ₽ | Проверка узлов и температур; результат — на словах. |", text)
        self.assertIn("| 5 | Настройка сети | от 1500 ₽ | Цена зависит от оборудования. |", text)
        self.assertIn("| 6 | Восстановление данных | уточнять |  |", text)
        self.assertIn("| 7 | Услуга без выезда | недоступно | Недоступно в этом прайсе. |", text)
        self.assertLess(text.index("### Выезд и консультации"), text.index("### Диагностика"))
        self.assertLess(text.index("### Диагностика"), text.index("### Настройка"))

    def test_matrix_table(self):
        text = self.with_price
        self.assertIn("### Ремонт ноутбуков (тест)", text)
        self.assertIn("Цены без деталей и расходников.", text)
        self.assertIn("| Модель | Замена батареи | Замена экрана | Замена клавиатуры | Ремонт платы |", text)
        self.assertIn("| 13\" · 2018 · A100 | 9900 ₽ | уточнять | — | от 2900 ₽ |", text)
        self.assertIn("| 15\" · 2020 · B200 | 0 ₽ | 14500 ₽ | — | 700 ₽ + 100 ₽ за клавишу |", text)

    def test_pipe_in_text_does_not_break_the_table(self):
        pricelist = fixture_pricelist()
        pricelist["items"][0]["name"] = "Выезд | срочный"
        pricelist["items"][0]["description"] = "Первая строка\nвторая строка"
        text = build.render_md({**self.data, "pricelist": pricelist})
        self.assertIn("| 1 | Выезд \\| срочный | 0 ₽ | Первая строка вторая строка |", text)


class RenderHtmlPricelistTests(unittest.TestCase):
    def test_pricelist_is_a_separate_key_of_the_page_data(self):
        template = TEMPLATE.read_text(encoding="utf-8")
        html = build.render_html({**guide_data(), "pricelist": fixture_pricelist()}, template)
        embedded = page_data(html)
        self.assertEqual(embedded["pricelist"], fixture_pricelist())
        self.assertIn("stages", embedded)

    def test_without_pricelist_the_key_is_absent(self):
        template = TEMPLATE.read_text(encoding="utf-8")
        embedded = page_data(build.render_html(guide_data(), template))
        self.assertNotIn("pricelist", embedded)

    def test_closing_script_tag_in_price_text_is_escaped(self):
        pricelist = fixture_pricelist()
        pricelist["items"][0]["name"] = "Выезд </script><b>x</b>"
        html = build.render_html({**guide_data(), "pricelist": pricelist}, TEMPLATE.read_text(encoding="utf-8"))
        self.assertEqual(html.count("</script>"), 1)
        self.assertEqual(page_data(html)["pricelist"]["items"][0]["name"], "Выезд </script><b>x</b>")


class PricelistCliTests(unittest.TestCase):
    def test_explicit_pricelist_goes_into_page_and_guide_md(self):
        with tempfile.TemporaryDirectory() as out:
            code, stdout, stderr = run_cli(cli_args(out, "--pricelist", str(PRICELIST)))
            self.assertEqual(code, 0, stderr)
            embedded = page_data((Path(out) / "index.html").read_text(encoding="utf-8"))
            markdown = (Path(out) / "guide.md").read_text(encoding="utf-8")
        self.assertEqual(embedded["pricelist"], fixture_pricelist())
        self.assertIn("## Прайс", markdown)
        self.assertIn("позиций 7", stdout)
        self.assertIn("матриц 1", stdout)

    def test_missing_pricelist_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as tmp:
            code, _, stderr = run_cli(cli_args(out, "--pricelist", str(Path(tmp) / "nope.json")))
            self.assertEqual(code, 1)
            self.assertIn("Прайс не найден", stderr)
            self.assertFalse((Path(out) / "index.html").exists())

    def test_invalid_pricelist_is_reported_and_nothing_is_written(self):
        broken = fixture_pricelist()
        broken["items"][1]["kind"] = "free"
        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            code, _, stderr = run_cli(cli_args(out, "--pricelist", str(path)))
            self.assertEqual(code, 1)
            self.assertIn("p002", stderr)
            self.assertFalse((Path(out) / "index.html").exists())

    def test_check_mode_validates_the_pricelist_too(self):
        broken = fixture_pricelist()
        broken["items"][0]["price"] = -3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")
            code, _, stderr = run_cli(["--content", str(CONTENT), "--profile", str(PROFILE), "--check",
                                       "--pricelist", str(path)])
        self.assertEqual(code, 1)
        self.assertIn("p001", stderr)
        code, stdout, _ = run_cli(["--content", str(CONTENT), "--profile", str(PROFILE), "--check",
                                   "--pricelist", str(PRICELIST)])
        self.assertEqual(code, 0)

    def test_default_path_is_the_demo_pricelist_in_the_repository(self):
        self.assertEqual(Path(build.DEFAULT_PRICELIST), ROOT / "guide" / "profiles" / "demo-pricelist.json")

    def test_default_pricelist_is_used_when_the_file_exists(self):
        with tempfile.TemporaryDirectory() as out:
            with mock.patch.object(build, "DEFAULT_PRICELIST", PRICELIST):
                code, _, stderr = run_cli(cli_args(out))
            self.assertEqual(code, 0, stderr)
            embedded = page_data((Path(out) / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(embedded["pricelist"]["title"], "Тестовый прайс")

    def test_without_the_default_file_there_is_no_pricelist_and_no_failure(self):
        with tempfile.TemporaryDirectory() as out, tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(build, "DEFAULT_PRICELIST", Path(tmp) / "none.json"):
                code, _, stderr = run_cli(cli_args(out))
            self.assertEqual(code, 0, stderr)
            embedded = page_data((Path(out) / "index.html").read_text(encoding="utf-8"))
            markdown = (Path(out) / "guide.md").read_text(encoding="utf-8")
        self.assertNotIn("pricelist", embedded)
        self.assertNotIn("## Прайс", markdown)

    def test_no_pricelist_flag_skips_even_an_existing_default(self):
        with tempfile.TemporaryDirectory() as out:
            with mock.patch.object(build, "DEFAULT_PRICELIST", PRICELIST):
                code, _, stderr = run_cli(cli_args(out, "--no-pricelist"))
            self.assertEqual(code, 0, stderr)
            embedded = page_data((Path(out) / "index.html").read_text(encoding="utf-8"))
        self.assertNotIn("pricelist", embedded)

    def test_pricelist_and_no_pricelist_together_is_an_error(self):
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(cli_args(out, "--pricelist", str(PRICELIST), "--no-pricelist"))
        self.assertEqual(code, 1)
        self.assertIn("--no-pricelist", stderr)


class DemoPricelistTests(unittest.TestCase):
    """Демо-прайс из репозитория: нейтральные вымышленные данные."""

    @classmethod
    def setUpClass(cls):
        cls.pricelist = build.load_pricelist(str(DEMO_PRICELIST))

    def test_is_valid(self):
        self.assertEqual(build.validate_pricelist(self.pricelist), [])

    def test_size_and_shape(self):
        items = self.pricelist["items"]
        self.assertTrue(10 <= len(items) <= 15, len(items))
        groups = {item["group"] for item in items}
        self.assertTrue(3 <= len(groups) <= 4, groups)
        self.assertEqual({item["kind"] for item in items}, {"fixed", "from", "ask", "na"})
        for item in items:
            if isinstance(item["price"], int):
                self.assertEqual(item["price"] % 50, 0, item)

    def test_one_small_matrix_with_ask_and_unavailable_cells(self):
        self.assertEqual(len(self.pricelist["matrices"]), 1)
        matrix = self.pricelist["matrices"][0]
        self.assertEqual((len(matrix["rows"]), len(matrix["columns"])), (3, 3))
        cells = [row["prices"].get(column["id"]) for row in matrix["rows"] for column in matrix["columns"]]
        self.assertIn("ask", cells)
        self.assertIn(None, cells)
        self.assertTrue(any(isinstance(cell, int) for cell in cells))

    def test_demo_builds_into_the_real_page(self):
        with tempfile.TemporaryDirectory() as out:
            code, _, stderr = run_cli(["--content", str(CONTENT), "--profile", str(PROFILE),
                                       "--template", str(REAL_TEMPLATE), "--out", out,
                                       "--pricelist", str(DEMO_PRICELIST)])
            self.assertEqual(code, 0, stderr)
            embedded = page_data((Path(out) / "index.html").read_text(encoding="utf-8"))
        self.assertEqual(embedded["pricelist"], self.pricelist)


if __name__ == "__main__":
    unittest.main()
