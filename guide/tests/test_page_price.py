"""Тесты панели «Прайс и акт» в `guide/template.html`.

Чистая логика (поиск, строки выбора, проверка цен, итог, текст для бланка) лежит в блоке
`/* logic:start */ … /* logic:end */` и гоняется через `node`; если `node` нет, проверки логики
пропускаются с пометкой. Ожидаемые числа разобраны вручную (в комментариях — арифметика).
Прайс для проверок — фикстура `fixtures/pricelist.json`, она только читается.
"""

import json
import unittest
from pathlib import Path

from guide.tests.test_page_logic import LOGIC_END, LOGIC_START, NEED_NODE, call, logic_source, template_text

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pricelist.json"
PRICELIST = json.loads(FIXTURE.read_text(encoding="utf-8"))
ITEMS = PRICELIST["items"]
MATRIX = PRICELIST["matrices"][0]

LAW_HINT = "по закону цена не может превышать прайс"


def ids(items):
    return [item["id"] for item in items]


def item(item_id):
    return next(entry for entry in ITEMS if entry["id"] == item_id)


def line(kind="fixed", base=1000, price=None, qty="1", warranty="", name="Услуга", key="k1"):
    """Строка выбора так, как её держит страница: цена и количество — строки из полей ввода."""
    return {"key": key, "name": name, "no": "1", "kind": kind, "base": base,
            "price": str(base) if price is None and base is not None else (price or ""),
            "qty": qty, "warranty": warranty}


@NEED_NODE
class SearchItemsTest(unittest.TestCase):
    def search(self, query, group=""):
        return ids(call("searchItems", ITEMS, query, group))

    def test_empty_query_returns_everything_in_price_order(self):
        self.assertEqual(self.search(""), ["p001", "p002", "p003", "p004", "p005", "p006", "p007"])
        self.assertEqual(self.search("   "), self.search(""))

    def test_name_is_found_regardless_of_case(self):
        # «выезд» есть в «Выезд мастера» и в «Услуга без выезда»
        self.assertEqual(self.search("выезд"), ["p001", "p007"])
        self.assertEqual(self.search("ВЫЕЗД"), ["p001", "p007"])
        self.assertEqual(self.search("Выезд Мастера"), ["p001"])

    def test_yo_and_ye_are_the_same_letter(self):
        for query in ("ёмкость", "емкость", "ЕМКОСТЬ"):
            with self.subTest(query=query):
                self.assertEqual(self.search(query), ["p004"])

    def test_number_matches_from_the_start_of_the_number(self):
        self.assertEqual(self.search("14.1"), ["p003"])
        self.assertEqual(self.search("14"), ["p003"])
        # «5» — это номер p005, а «15» (p004) начинается с единицы
        self.assertEqual(self.search("5"), ["p005"])
        self.assertEqual(self.search("1"), ["p001", "p003", "p004"])

    def test_all_words_must_match_in_any_order(self):
        self.assertEqual(self.search("диагностика компьютера"), ["p003"])
        self.assertEqual(self.search("компьютера диагностика"), ["p003"])
        self.assertEqual(self.search("диагностика сети"), [])

    def test_group_filter_and_query_work_together(self):
        self.assertEqual(self.search("", "Настройка"), ["p005", "p006", "p007"])
        self.assertEqual(self.search("сети", "Настройка"), ["p005"])
        self.assertEqual(self.search("сети", "Диагностика"), [])
        self.assertEqual(self.search("", "Нет такой группы"), [])

    def test_nothing_found(self):
        self.assertEqual(self.search("zzz"), [])

    def test_groups_keep_the_order_of_first_appearance(self):
        self.assertEqual(call("groupsOf", ITEMS), ["Выезд и консультации", "Диагностика", "Настройка"])
        self.assertEqual(call("groupsOf", []), [])


@NEED_NODE
class PriceLabelTest(unittest.TestCase):
    def test_labels_by_kind(self):
        self.assertEqual(call("priceLabel", "fixed", 1500, "₽"), "1500 ₽")
        self.assertEqual(call("priceLabel", "fixed", 0, "₽"), "0 ₽")
        self.assertEqual(call("priceLabel", "from", 1500, "₽"), "от 1500 ₽")
        self.assertEqual(call("priceLabel", "ask", None, "₽"), "уточнять")
        self.assertEqual(call("priceLabel", "na", None, "₽"), "недоступно")

    def test_currency_comes_from_the_pricelist(self):
        self.assertEqual(call("priceLabel", "fixed", 10, "руб."), "10 руб.")


@NEED_NODE
class MakeLineTest(unittest.TestCase):
    def test_fixed_starts_with_the_price_from_the_list(self):
        result = call("makeLine", item("p003"))
        self.assertEqual(result["key"], "i:p003")
        self.assertEqual(result["name"], "Диагностика компьютера")
        self.assertEqual(result["no"], "14.1")
        self.assertEqual((result["kind"], result["base"], result["price"]), ("fixed", 1000, "1000"))
        self.assertEqual((result["qty"], result["warranty"]), ("1", ""))

    def test_free_fixed_item_starts_with_zero(self):
        result = call("makeLine", item("p001"))
        self.assertEqual((result["kind"], result["base"], result["price"]), ("fixed", 0, "0"))

    def test_from_starts_with_the_minimum_price(self):
        result = call("makeLine", item("p005"))
        self.assertEqual((result["kind"], result["base"], result["price"]), ("from", 1500, "1500"))

    def test_ask_starts_with_an_empty_price(self):
        result = call("makeLine", item("p006"))
        self.assertEqual((result["kind"], result["base"], result["price"]), ("ask", None, ""))

    def test_unavailable_item_cannot_be_added(self):
        self.assertIsNone(call("makeLine", item("p007")))


@NEED_NODE
class MatrixLinesTest(unittest.TestCase):
    def test_cell_kinds(self):
        self.assertEqual(call("cellKind", MATRIX, "r1", "c1"), {"kind": "fixed", "price": 9900})
        self.assertEqual(call("cellKind", MATRIX, "r1", "c2"), {"kind": "ask", "price": None})
        self.assertEqual(call("cellKind", MATRIX, "r1", "c3"), {"kind": "na", "price": None})   # null
        self.assertEqual(call("cellKind", MATRIX, "r2", "c3"), {"kind": "na", "price": None})   # ключа нет
        self.assertEqual(call("cellKind", MATRIX, "r2", "c1"), {"kind": "fixed", "price": 0})
        self.assertEqual(call("cellKind", MATRIX, "r9", "c1"), {"kind": "na", "price": None})   # нет такой модели

    def test_hint_cells_are_asked_with_a_hint_and_a_floor(self):
        self.assertEqual(call("cellKind", MATRIX, "r1", "c4"),
                         {"kind": "ask", "price": None, "hint": "от 2900 ₽", "from": 2900})
        self.assertEqual(call("cellKind", MATRIX, "r2", "c4"),
                         {"kind": "ask", "price": None, "hint": "700 ₽ + 100 ₽ за клавишу", "from": None})

    def test_hint_cell_makes_a_line_with_hint_and_floor(self):
        first, second = call("matrixLines", MATRIX, "r1", ["c1", "c4"])
        self.assertEqual((first["hint"], first["floor"]), ("", None))
        self.assertEqual((second["kind"], second["base"], second["price"]), ("ask", None, ""))
        self.assertEqual((second["name"], second["hint"], second["floor"]),
                         ("Ремонт платы — 13\" · 2018 · A100", "от 2900 ₽", 2900))
        (formula,) = call("matrixLines", MATRIX, "r2", ["c4"])
        self.assertEqual((formula["hint"], formula["floor"]), ("700 ₽ + 100 ₽ за клавишу", None))

    def test_plain_lines_have_no_hint(self):
        result = call("makeLine", item("p006"))
        self.assertEqual((result["hint"], result["floor"]), ("", None))

    def test_lines_are_named_service_dash_model(self):
        result = call("matrixLines", MATRIX, "r1", ["c1", "c2"])
        self.assertEqual([entry["name"] for entry in result],
                         ["Замена батареи — 13\" · 2018 · A100", "Замена экрана — 13\" · 2018 · A100"])
        self.assertEqual([entry["key"] for entry in result], ["m:m1:r1:c1", "m:m1:r1:c2"])

    def test_number_cell_is_fixed_and_ask_cell_needs_a_price(self):
        first, second = call("matrixLines", MATRIX, "r1", ["c1", "c2"])
        self.assertEqual((first["kind"], first["base"], first["price"]), ("fixed", 9900, "9900"))
        self.assertEqual((second["kind"], second["base"], second["price"]), ("ask", None, ""))
        self.assertEqual((first["qty"], first["warranty"]), ("1", ""))

    def test_unavailable_cells_are_skipped(self):
        result = call("matrixLines", MATRIX, "r1", ["c1", "c2", "c3"])
        self.assertEqual([entry["key"] for entry in result], ["m:m1:r1:c1", "m:m1:r1:c2"])
        self.assertEqual(call("matrixLines", MATRIX, "r2", ["c3"]), [])

    def test_order_follows_the_columns_of_the_matrix(self):
        result = call("matrixLines", MATRIX, "r2", ["c2", "c1"])
        self.assertEqual([entry["key"] for entry in result], ["m:m1:r2:c1", "m:m1:r2:c2"])

    def test_zero_price_cell_is_available(self):
        (only,) = call("matrixLines", MATRIX, "r2", ["c1"])
        self.assertEqual((only["kind"], only["base"], only["price"]), ("fixed", 0, "0"))

    def test_unknown_model_or_column_gives_nothing(self):
        self.assertEqual(call("matrixLines", MATRIX, "r9", ["c1"]), [])
        self.assertEqual(call("matrixLines", MATRIX, "r1", ["c9"]), [])
        self.assertEqual(call("matrixLines", MATRIX, "r1", []), [])


@NEED_NODE
class MergeLinesTest(unittest.TestCase):
    def test_new_lines_are_appended_and_repeats_are_skipped(self):
        first = line(key="i:p001", name="А", qty="3")
        again = line(key="i:p001", name="А", qty="1")
        other = line(key="i:p002", name="Б")
        result = call("mergeLines", [first], [again, other])
        self.assertEqual([entry["key"] for entry in result["lines"]], ["i:p001", "i:p002"])
        self.assertEqual((result["added"], result["skipped"]), (1, 1))
        # уже выбранная строка сохраняет то, что в ней успели набрать
        self.assertEqual(result["lines"][0]["qty"], "3")

    def test_repeats_inside_the_new_batch_are_skipped_too(self):
        one = line(key="x")
        result = call("mergeLines", [], [one, one])
        self.assertEqual((len(result["lines"]), result["added"], result["skipped"]), (1, 1, 1))


@NEED_NODE
class StepQtyTest(unittest.TestCase):
    def test_plus_and_minus_by_one(self):
        self.assertEqual(call("stepQty", "1", 1), "2")
        self.assertEqual(call("stepQty", "5", -1), "4")
        self.assertEqual(call("stepQty", " 7 ", 1), "8")

    def test_never_below_one_or_above_the_limit(self):
        self.assertEqual(call("stepQty", "1", -1), "1")
        self.assertEqual(call("stepQty", "0", -1), "1")
        self.assertEqual(call("stepQty", "999", 1), "999")

    def test_garbage_in_the_field_counts_as_zero(self):
        for raw in ("", "abc", "1.5", "-3"):
            with self.subTest(raw=raw):
                self.assertEqual(call("stepQty", raw, 1), "1")
                self.assertEqual(call("stepQty", raw, -1), "1")


@NEED_NODE
class LineErrorTest(unittest.TestCase):
    def error(self, **kwargs):
        return call("lineError", line(**kwargs))

    def fields(self, **kwargs):
        return call("lineFieldErrors", line(**kwargs))

    def test_valid_lines_have_no_error(self):
        self.assertIsNone(self.error(kind="fixed", base=1500))
        self.assertIsNone(self.error(kind="from", base=1500))
        self.assertIsNone(self.error(kind="ask", base=None, price="2500"))
        self.assertEqual(self.fields(kind="fixed", base=1500), {"qty": None, "price": None})

    def test_fixed_price_above_the_list_breaks_the_law(self):
        message = self.error(kind="fixed", base=1500, price="1501")
        self.assertIn(LAW_HINT, message.lower())
        self.assertIn("1500", message)
        self.assertIn(LAW_HINT, self.fields(kind="fixed", base=1500, price="1501")["price"].lower())

    def test_fixed_price_equal_or_below_the_list_is_fine(self):
        for price in ("1500", "1499", "1", "0"):
            with self.subTest(price=price):
                self.assertIsNone(self.error(kind="fixed", base=1500, price=price))

    def test_free_visit_cannot_be_charged(self):
        self.assertIn(LAW_HINT, self.error(kind="fixed", base=0, price="100").lower())
        self.assertIsNone(self.error(kind="fixed", base=0, price="0"))

    def test_from_price_may_be_below_or_above_the_minimum(self):
        for price in ("500", "1500", "5000", "0"):
            with self.subTest(price=price):
                self.assertIsNone(self.error(kind="from", base=1500, price=price))

    def test_ask_price_is_required_and_positive(self):
        for price in ("", "   ", "0"):
            with self.subTest(price=price):
                self.assertIsNotNone(self.error(kind="ask", base=None, price=price))
        self.assertIsNone(self.error(kind="ask", base=None, price="1"))

    def test_price_must_be_a_whole_non_negative_number(self):
        for price in ("", "abc", "-5", "99.5", "99,5", "1e3", "100000000"):
            with self.subTest(price=price):
                self.assertIsNotNone(self.fields(kind="from", base=1500, price=price)["price"])

    def test_spaces_inside_the_price_are_ignored(self):
        self.assertIsNone(self.error(kind="from", base=1500, price=" 1 000 "))

    def test_quantity_must_be_a_whole_number_from_one(self):
        for qty in ("0", "1.5", "0.5", "", "  ", "-1", "abc", "1000"):
            with self.subTest(qty=qty):
                self.assertIsNotNone(self.fields(qty=qty)["qty"])
                self.assertIsNone(self.fields(qty=qty)["price"])
        for qty in ("1", "2", "10", "999", " 3 "):
            with self.subTest(qty=qty):
                self.assertIsNone(self.fields(qty=qty)["qty"])

    def test_both_fields_can_be_wrong_at_once(self):
        result = self.fields(kind="fixed", base=1000, price="2000", qty="0")
        self.assertIsNotNone(result["qty"])
        self.assertIsNotNone(result["price"])


@NEED_NODE
class LineWarningTest(unittest.TestCase):
    """Ниже ориентира прайса (from в ячейке-объекте) страница предупреждает, но не блокирует."""

    def ask_line(self, price, floor=2900):
        entry = line(kind="ask", base=None, price=price, name="Ремонт платы")
        entry.update(hint="от 2900 ₽", floor=floor)
        return entry

    def test_price_below_the_floor_warns(self):
        message = call("lineWarning", self.ask_line("2500"))
        self.assertIn("ниже ориентира прайса", message.lower())
        self.assertIn("2900", message)

    def test_price_at_or_above_the_floor_does_not_warn(self):
        for price in ("2900", "3000", "50000"):
            with self.subTest(price=price):
                self.assertIsNone(call("lineWarning", self.ask_line(price)))

    def test_floor_zero_never_warns(self):
        self.assertIsNone(call("lineWarning", self.ask_line("1", floor=0)))

    def test_no_floor_no_warning(self):
        self.assertIsNone(call("lineWarning", self.ask_line("1", floor=None)))
        self.assertIsNone(call("lineWarning", line(kind="from", base=1500, price="500")))

    def test_warning_is_not_an_error_and_does_not_stop_the_total(self):
        entry = self.ask_line("2500")
        self.assertIsNone(call("lineError", entry))
        self.assertEqual(call("lineSum", entry), 2500)
        act = call("computeAct", [entry], "", "")
        self.assertTrue(act["ok"])
        self.assertEqual(act["total"], 2500)

    def test_invalid_price_gives_an_error_not_a_warning(self):
        for price in ("", "0", "abc", "-5"):
            with self.subTest(price=price):
                self.assertIsNone(call("lineWarning", self.ask_line(price)))
        self.assertIsNotNone(call("lineError", self.ask_line("0")))


@NEED_NODE
class LineSumTest(unittest.TestCase):
    def test_sum_is_price_times_quantity(self):
        self.assertEqual(call("lineSum", line(base=1500, qty="2")), 3000)
        self.assertEqual(call("lineSum", line(kind="from", base=1500, price="2000", qty="3")), 6000)
        self.assertEqual(call("lineSum", line(base=0, qty="5")), 0)

    def test_spaces_are_ignored(self):
        self.assertEqual(call("lineSum", line(kind="from", base=1500, price=" 1 500 ", qty=" 2 ")), 3000)

    def test_invalid_line_has_no_sum(self):
        self.assertIsNone(call("lineSum", line(base=1500, price="1600")))     # выше прайса
        self.assertIsNone(call("lineSum", line(base=1500, qty="0")))
        self.assertIsNone(call("lineSum", line(base=1500, qty="1.5")))
        self.assertIsNone(call("lineSum", line(kind="ask", base=None, price="")))


@NEED_NODE
class ComputeActTest(unittest.TestCase):
    def act(self, lines, parts="", discount=""):
        return call("computeAct", lines, parts, discount)

    def one(self, price, discount="", parts=""):
        """Одна строка: сумма услуг равна price."""
        return self.act([line(kind="from", base=0, price=str(price))], parts, discount)

    def test_empty_list_means_nothing_is_chosen(self):
        result = self.act([])
        self.assertTrue(result["empty"])
        self.assertFalse(result["ok"])
        self.assertIsNone(result["total"])

    def test_services_parts_discount_and_total(self):
        # 1000 × 2 = 2000; скидка 10% от услуг = 200; комплектующие 500; 2000 − 200 + 500 = 2300
        result = self.act([line(base=1000, qty="2")], "500", "10")
        self.assertTrue(result["ok"])
        self.assertFalse(result["empty"])
        self.assertEqual(
            (result["services"], result["parts"], result["discountPct"], result["discount"], result["total"]),
            (2000, 500, 10, 200, 2300),
        )

    def test_several_lines_and_sums_per_line(self):
        # 1500 × 1 + 300 × 3 = 1500 + 900 = 2400
        lines = [line(base=1500, key="a"), line(base=300, qty="3", key="b")]
        result = self.act(lines)
        self.assertEqual((result["services"], result["discount"], result["total"]), (2400, 0, 2400))
        self.assertEqual(result["lineSums"], {"a": 1500, "b": 900})

    def test_blank_parts_and_discount_mean_zero(self):
        result = self.one(1000)
        self.assertTrue(result["ok"])
        self.assertEqual((result["parts"], result["discountPct"], result["discount"], result["total"]), (0, 0, 0, 1000))
        spaced = self.one(1000, discount="  ", parts="  ")
        self.assertEqual(spaced["total"], 1000)

    def test_discount_zero_changes_nothing(self):
        result = self.one(2000, discount="0", parts="300")
        self.assertEqual((result["discount"], result["total"]), (0, 2300))

    def test_discount_hundred_percent_leaves_only_parts(self):
        result = self.one(2000, discount="100", parts="300")
        self.assertEqual((result["discount"], result["total"]), (2000, 300))

    def test_discount_is_rounded_half_up_to_the_ruble(self):
        cases = [
            # (услуги, %, сумма скидки): точное значение → после округления
            (5, "10", 1),      # 0.5 → 1
            (15, "10", 2),     # 1.5 → 2
            (14, "10", 1),     # 1.4 → 1
            (4, "10", 0),      # 0.4 → 0
            (1005, "10", 101),  # 100.5 → 101
            (10, "33", 3),     # 3.3 → 3
            (15, "33", 5),     # 4.95 → 5
            (1, "1", 0),       # 0.01 → 0
            (50, "1", 1),      # 0.5 → 1
        ]
        for services, percent, expected in cases:
            with self.subTest(services=services, percent=percent):
                result = self.one(services, discount=percent)
                self.assertEqual(result["discount"], expected)
                self.assertEqual(result["total"], services - expected)

    def test_bad_discount_stops_the_total(self):
        for bad in ("101", "-1", "10.5", "abc", "1e2"):
            with self.subTest(discount=bad):
                result = self.one(1000, discount=bad)
                self.assertFalse(result["ok"])
                self.assertIsNotNone(result["errors"]["discount"])
                self.assertIsNone(result["errors"]["parts"])
                self.assertIsNone(result["total"])
                self.assertIsNone(result["discount"])

    def test_bad_parts_stop_the_total(self):
        for bad in ("-5", "12.5", "abc", "1e3"):
            with self.subTest(parts=bad):
                result = self.one(1000, parts=bad)
                self.assertFalse(result["ok"])
                self.assertIsNotNone(result["errors"]["parts"])
                self.assertIsNone(result["total"])

    def test_error_in_a_line_stops_the_total_and_names_the_line(self):
        lines = [line(base=1000, price="1200", key="bad"), line(base=500, key="good")]
        result = self.act(lines)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["total"])
        self.assertIsNone(result["services"])
        self.assertIn(LAW_HINT, result["errors"]["lines"]["bad"].lower())
        self.assertNotIn("good", result["errors"]["lines"])

    def test_ask_line_without_price_blocks_the_total(self):
        result = self.act([line(kind="ask", base=None, price="")])
        self.assertFalse(result["ok"])
        self.assertIsNone(result["total"])

    def test_empty_list_still_reports_a_bad_field(self):
        result = self.act([], "abc", "")
        self.assertTrue(result["empty"])
        self.assertIsNotNone(result["errors"]["parts"])


@NEED_NODE
class ActTextTest(unittest.TestCase):
    def build(self, lines, parts="", discount="", currency="₽"):
        act = call("computeAct", lines, parts, discount)
        return call("actText", lines, act, currency)

    def test_text_for_the_form(self):
        lines = [
            line(base=1000, qty="2", name="Диагностика компьютера", key="a"),
            line(base=9900, qty="1", name="Замена батареи — 13\" · 2018 · A100", warranty="1 месяц", key="b"),
        ]
        # 2000 + 9900 = 11900; скидка 10% = 1190; 11900 − 1190 + 500 = 11210
        self.assertEqual(
            self.build(lines, parts="500", discount="10"),
            "1. Диагностика компьютера — 1000 × 2 = 2000\n"
            "2. Замена батареи — 13\" · 2018 · A100 — 9900 × 1 = 9900 — 1 месяц\n"
            "\n"
            "Стоимость услуг: 11900 ₽\n"
            "Стоимость комплектующих: 500 ₽\n"
            "Скидка (10%): 1190 ₽\n"
            "К оплате: 11210 ₽",
        )

    def test_zero_parts_and_discount_are_still_written(self):
        text = self.build([line(base=300, name="Консультация")])
        self.assertEqual(
            text,
            "1. Консультация — 300 × 1 = 300\n"
            "\n"
            "Стоимость услуг: 300 ₽\n"
            "Стоимость комплектующих: 0 ₽\n"
            "Скидка (0%): 0 ₽\n"
            "К оплате: 300 ₽",
        )

    def test_empty_warranty_is_omitted_and_blank_one_too(self):
        text = self.build([line(base=300, name="Консультация", warranty="   ")])
        self.assertTrue(text.startswith("1. Консультация — 300 × 1 = 300\n"))

    def test_warranty_stays_on_one_line(self):
        text = self.build([line(base=300, name="Консультация", warranty="  3 мес.\nбез выезда  ")])
        self.assertTrue(text.startswith("1. Консультация — 300 × 1 = 300 — 3 мес. без выезда\n"))

    def test_nothing_to_copy_while_there_are_errors_or_no_lines(self):
        self.assertIsNone(self.build([]))
        self.assertIsNone(self.build([line(base=1000, price="2000")]))
        self.assertIsNone(self.build([line(base=1000)], discount="150"))

    def test_free_visit_line(self):
        text = self.build([line(base=0, name="Выезд мастера")])
        self.assertIn("1. Выезд мастера — 0 × 1 = 0\n", text)
        self.assertTrue(text.endswith("К оплате: 0 ₽"))


class PriceTemplateTest(unittest.TestCase):
    """Проверки самого файла шаблона (node не нужен)."""

    @classmethod
    def setUpClass(cls):
        cls.text = template_text()
        cls.logic = logic_source()

    def test_logic_functions_live_in_the_logic_block(self):
        for name in ("searchItems", "groupsOf", "priceLabel", "makeLine", "cellKind", "matrixLines", "mergeLines",
                     "lineFieldErrors", "lineError", "lineSum", "computeAct", "actText"):
            with self.subTest(function=name):
                self.assertIn(f"function {name}(", self.logic)
        self.assertEqual(self.text.count(LOGIC_START), 1)
        self.assertEqual(self.text.count(LOGIC_END), 1)

    def test_ui_strings_required_by_the_spec_are_present(self):
        for phrase in ("Прайс и акт", "Ничего не выбрано", "Выбрано", "Стоимость услуг", "Стоимость комплектующих",
                       "Скидка, %", "К оплате", "Гарантия", "Скопировать список для бланка", "Очистить",
                       "недоступно", "уточнять", "Поиск по названию или номеру", "Группа", "Модель",
                       "По закону цена не может превышать прайс",
                       "В бланк пиши от руки красным; в пустых полях прочерки; только оказанные услуги",
                       "только если услуга оказана столько раз"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)

    def test_panel_is_built_only_when_the_page_has_a_pricelist(self):
        self.assertIn("data.pricelist", self.text)
        self.assertNotIn('id="price-open"', self.text, "кнопка прайса создаётся скриптом, только если есть прайс")
        self.assertNotIn('id="price"', self.text)

    def test_price_code_never_touches_storage(self):
        start = self.text.index("// price:start")
        end = self.text.index("// price:end")
        self.assertLess(start, end)
        region = self.text[start:end]
        for forbidden in ("localStorage", "sessionStorage", "indexedDB", "document.cookie", "readStored(",
                          "writeStored(", "STORAGE_KEY"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, region)
        # весь блок логики тоже ничего не сохраняет, кроме прогресса шагов
        self.assertNotIn("writeStored(", self.logic)

    def test_price_panel_is_hidden_when_printing(self):
        css = self.text.split("@media print", 1)[1].split("</style>", 1)[0]
        self.assertIn(".sheet", css)


if __name__ == "__main__":
    unittest.main()
