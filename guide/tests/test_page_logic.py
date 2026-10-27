"""Тесты страницы-гайда `guide/template.html`.

Чистая логика страницы лежит в шаблоне между строками `/* logic:start */` и
`/* logic:end */`. Тест вырезает этот блок и гоняет его через `node`; если `node`
нет, проверки логики пропускаются с пометкой; при REQUIRE_NODE=1 (так в CI) — падают (см. `_node.py`).

Ожидаемые значения взяты из брифа, спецификации (R04, R64, A02, A03) и разобраны
вручную: «Чистыми» = «Всего» − «Запчасти» (13000 − 1000 = 12000; 10000 − 1000 = 9000),
русские формы «шаг / шага / шагов», формат сохранения `{ "<id шага>": true }`.
Фикстуры движка (`guide/tests/fixtures/`) только читаются.
"""

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from guide import build
from guide.tests import _node

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "guide" / "template.html"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
BUILD_SCRIPT = ROOT / "guide" / "build.py"

# node и поведение без него (пропуск; при REQUIRE_NODE — провал) — в одном месте, см. _node.py
NODE, NEED_NODE = _node.NODE, _node.NEED_NODE

LOGIC_START = "/* logic:start */"
LOGIC_END = "/* logic:end */"


def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def logic_source() -> str:
    text = template_text()
    start = text.find(LOGIC_START)
    end = text.find(LOGIC_END)
    if start == -1 or end == -1 or end < start:
        raise AssertionError("в template.html нет блока между /* logic:start */ и /* logic:end */")
    return text[start + len(LOGIC_START):end]


def call(name: str, *args):
    """Вызывает функцию из блока логики и возвращает её результат (через JSON)."""
    arguments = ", ".join(json.dumps(arg, ensure_ascii=False) for arg in args)
    script = (
        logic_source()
        + f"\n;const __result = {name}({arguments});\n"
        + "process.stdout.write(JSON.stringify(__result === undefined ? null : __result));\n"
    )
    done = subprocess.run([NODE, "-"], input=script.encode("utf-8"), capture_output=True, timeout=60)
    if done.returncode != 0:
        raise AssertionError(f"node упал при вызове {name}: " + done.stderr.decode("utf-8", errors="replace")[-600:])
    return json.loads(done.stdout.decode("utf-8"))


def fixture_data() -> dict:
    content = build.load_content(str(FIXTURES / "content"))
    profile = build.load_profile(str(FIXTURES / "profile.json"))
    return build.apply_profile(content, profile)


def onsite_report() -> dict:
    reports = fixture_data()["cheatsheet"]["reports"]
    return next(report for report in reports if report["id"] == "onsite")


ONSITE_TEXT = (
    "{order}\nСтатус - Готов\n\nВсего  - {total}\nЗапчасти - {parts}\nЧистыми {net}\n{works}\n"
    "Прошу Проверить и подтвердить отчет статусом Готов"
)


def segments(items):
    return [(item["text"], item["bold"]) for item in items]


@NEED_NODE
class ParseBoldTest(unittest.TestCase):
    def test_plain_text_is_one_segment(self):
        self.assertEqual(segments(call("parseBold", "Открой магазин приложений")), [("Открой магазин приложений", False)])

    def test_double_asterisks_make_a_bold_segment(self):
        result = call("parseBold", "Найди **Приложение службы** и нажми «Установить»")
        self.assertEqual(
            segments(result),
            [("Найди ", False), ("Приложение службы", True), (" и нажми «Установить»", False)],
        )

    def test_several_bold_parts(self):
        result = call("parseBold", "**раз** и **два**")
        self.assertEqual(segments(result), [("раз", True), (" и ", False), ("два", True)])

    def test_html_in_data_stays_plain_text(self):
        result = call("parseBold", "<b>**x**</b><img src=x>")
        self.assertEqual(segments(result), [("<b>", False), ("x", True), ("</b><img src=x>", False)])

    def test_lonely_or_spaced_markers_stay_literal(self):
        for text in ("цена ** 5", "раз **два", "****", "2 ** 3 ** 4"):
            with self.subTest(text=text):
                joined = "".join(item["text"] for item in call("parseBold", text))
                self.assertEqual(joined, text)
                self.assertFalse(any(item["bold"] for item in call("parseBold", text)))


@NEED_NODE
class SubstituteTokensTest(unittest.TestCase):
    def test_order_and_phone_are_substituted(self):
        values = {"order": "12345", "phone": "89001234567"}
        self.assertEqual(call("substituteTokens", "{{order}} Я на месте. Мой телефон {{phone}}", values),
                         "12345 Я на месте. Мой телефон 89001234567")

    def test_empty_order_shows_visible_placeholder(self):
        self.assertEqual(call("substituteTokens", "{{order}} Принял", {"order": "", "phone": ""}), "[НОМЕР ЗАЯВКИ] Принял")

    def test_missing_or_blank_phone_shows_visible_placeholder(self):
        self.assertEqual(call("substituteTokens", "Телефон {{phone}}", {}), "Телефон [ТВОЙ ТЕЛЕФОН С 8]")
        self.assertEqual(call("substituteTokens", "Телефон {{phone}}", {"phone": "   "}), "Телефон [ТВОЙ ТЕЛЕФОН С 8]")

    def test_spaces_inside_braces_are_allowed(self):
        self.assertEqual(call("substituteTokens", "{{ order }} В пути", {"order": "77"}), "77 В пути")

    def test_unknown_token_is_left_as_is(self):
        self.assertEqual(call("substituteTokens", "{{unknown}} и {{constructor}}", {"order": "1"}),
                         "{{unknown}} и {{constructor}}")

    def test_value_is_not_substituted_again(self):
        values = {"order": "{{phone}}", "phone": "89001234567"}
        self.assertEqual(call("substituteTokens", "{{order}}", values), "{{phone}}")

    def test_dollar_signs_in_value_are_kept(self):
        self.assertEqual(call("substituteTokens", "{{order}}", {"order": "$& $1"}), "$& $1")

    def test_segments_mark_placeholders_as_not_filled(self):
        result = call("tokenSegments", "{{order}} Принял", {"order": ""})
        self.assertEqual(
            [(item["text"], item["token"], item["filled"]) for item in result],
            [("[НОМЕР ЗАЯВКИ]", "order", False), (" Принял", None, True)],
        )
        filled = call("tokenSegments", "{{order}} Принял", {"order": "55"})
        self.assertEqual(
            [(item["text"], item["token"], item["filled"]) for item in filled],
            [("55", "order", True), (" Принял", None, True)],
        )


@NEED_NODE
class ComputeReportTest(unittest.TestCase):
    def setUp(self):
        self.report = onsite_report()

    def compute(self, total, parts, works="Замена SSD", order="12345", phone=""):
        return call("computeReport", self.report, {"total": total, "parts": parts, "works": works},
                    {"order": order, "phone": phone})

    def test_example_from_the_brief(self):
        result = self.compute("13000", "1000")
        self.assertTrue(result["ok"])
        self.assertEqual(result["values"]["net"], "12000")
        self.assertEqual(
            result["text"],
            "12345\nСтатус - Готов\n\nВсего  - 13000\nЗапчасти - 1000\nЧистыми 12000\nЗамена SSD\n"
            "Прошу Проверить и подтвердить отчет статусом Готов",
        )

    def test_net_and_summa_note(self):
        result = self.compute("10000", "1000")
        self.assertTrue(result["ok"])
        self.assertEqual(result["values"]["net"], "9000")
        self.assertEqual(result["note"], "В поле «Сумма» в приложении впиши: 9000")
        self.assertIn("Чистыми 9000", result["text"])

    def test_no_parts_means_total_goes_to_summa(self):
        result = self.compute("10000", "0")
        self.assertTrue(result["ok"])
        self.assertEqual(result["note"], "В поле «Сумма» в приложении впиши: 10000")

    def test_spaces_and_comma_are_accepted(self):
        result = self.compute("10 000", "1 000")
        self.assertTrue(result["ok"])
        self.assertEqual(result["values"]["net"], "9000")
        decimals = self.compute("1000,5", "0,5")
        self.assertTrue(decimals["ok"])
        self.assertEqual(decimals["values"]["net"], "1000")

    def test_float_noise_is_rounded(self):
        result = self.compute("0.3", "0.1")
        self.assertEqual(result["values"]["net"], "0.2")

    def test_empty_order_stays_visible_in_text(self):
        result = self.compute("5000", "0", order="")
        self.assertTrue(result["text"].startswith("[НОМЕР ЗАЯВКИ]\nСтатус - Готов"))

    def test_line_breaks_of_description_are_kept(self):
        result = self.compute("5000", "0", works="Чистка от пыли\nЗамена пасты")
        self.assertIn("Чистыми 5000\nЧистка от пыли\nЗамена пасты\nПрошу Проверить", result["text"])

    def test_blocking_problems_give_russian_hint_and_no_report(self):
        cases = {
            "empty total": (("", "1000"), "total", "empty"),
            "empty parts": (("10000", ""), "parts", "empty"),
            "not a number": (("много", "1000"), "total", "invalid"),
            "negative total": (("-500", "0"), "total", "invalid"),
            "negative parts": (("10000", "-1"), "parts", "invalid"),
            "parts above total": (("1000", "2000"), "parts", "invalid"),
            "absurdly large": (("9" * 40, "0"), "total", "invalid"),
        }
        for title, ((total, parts), key, kind) in cases.items():
            with self.subTest(title):
                result = self.compute(total, parts)
                self.assertFalse(result["ok"])
                self.assertIsNone(result["text"])
                self.assertIsNone(result["note"])
                self.assertIn(key, result["errors"])
                self.assertEqual(result["errors"][key]["kind"], kind)
                self.assertRegex(result["errors"][key]["message"], r"[А-Яа-яЁё]")

    def test_empty_description_blocks_the_report(self):
        result = self.compute("10000", "1000", works="   ")
        self.assertFalse(result["ok"])
        self.assertEqual(result["errors"]["works"]["kind"], "empty")
        self.assertIsNone(result["text"])

    def test_parts_above_total_names_both_fields(self):
        message = self.compute("1000", "2000")["errors"]["parts"]["message"]
        self.assertIn("Запчасти", message)
        self.assertIn("Всего", message)

    def test_form_comes_from_data_not_from_code(self):
        # другая форма (в духе «закрытия Мода»): свои ключи, подписи и шаблон
        report = {
            "id": "closing",
            "title": "Закрытие",
            "fields": [
                {"key": "taken", "label": "Взято с клиента", "type": "number"},
                {"key": "spare", "label": "Сумма запчастей", "type": "number"},
                {"key": "warranty", "label": "Срок гарантии", "type": "text"},
            ],
            "computed": [{"key": "clean", "op": "sub", "a": "taken", "b": "spare"}],
            "template": "Заявка {{order}}\nВзято {{taken}}, запчасти {{spare}}, чистыми {{clean}}\nГарантия {{warranty}}",
            "note": "Чистыми: {{clean}}",
        }
        result = call("computeReport", report, {"taken": "11400", "spare": "3200", "warranty": "90 дней"}, {"order": "A1"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], "Заявка A1\nВзято 11400, запчасти 3200, чистыми 8200\nГарантия 90 дней")
        self.assertEqual(result["note"], "Чистыми: 8200")


STAGES_FOR_READINESS = [
    {"id": "s1", "steps": [{"id": "s1-01"}, {"id": "s1-02", "optional": False}, {"id": "s1-03", "optional": True}]},
    {"id": "s2", "steps": [{"id": "s2-01"}, {"id": "s2-02"}]},
    {"id": "s2b", "steps": [{"id": "s2b-01", "optional": True}]},
    {"id": "s3", "steps": [{"id": "s3-01"}]},
    {"id": "s4", "steps": [{"id": "s4-01"}, {"id": "s4-02"}]},
    {"id": "s4b", "steps": [{"id": "s4b-01"}]},
]
# обязательных шагов в этапах s1–s3: s1-01, s1-02, s2-01, s2-02, s3-01 — пять


@NEED_NODE
class ReadinessTest(unittest.TestCase):
    def ready(self, *done):
        return call("readiness", STAGES_FOR_READINESS, {step_id: True for step_id in done})

    def test_nothing_done(self):
        result = self.ready()
        self.assertEqual((result["left"], result["total"], result["ready"]), (5, 5, False))
        self.assertEqual(result["text"], "До заявок осталось 5 шагов")

    def test_done_steps_reduce_the_number(self):
        result = self.ready("s1-01", "s2-01", "s3-01")
        self.assertEqual(result["left"], 2)
        self.assertEqual(result["text"], "До заявок осталось 2 шага")

    def test_last_step_uses_singular(self):
        result = self.ready("s1-01", "s1-02", "s2-01", "s3-01")
        self.assertEqual(result["text"], "До заявок осталось 1 шаг")

    def test_optional_and_later_stages_do_not_count(self):
        result = self.ready("s1-03", "s2b-01", "s4-01", "s4-02", "s4b-01")
        self.assertEqual(result["left"], 5)

    def test_all_required_done_means_ready(self):
        result = self.ready("s1-01", "s1-02", "s2-01", "s2-02", "s3-01")
        self.assertTrue(result["ready"])
        self.assertEqual(result["left"], 0)
        self.assertEqual(result["text"], "Можно писать «Доброе утро» диспетчеру")

    def test_unknown_ids_are_ignored(self):
        result = self.ready("s9-99", "nothing")
        self.assertEqual(result["left"], 5)

    def test_fixture_content(self):
        # в фикстуре обязательных шагов в s1–s2 пять: s1-01, s1-02, s2-01, s2-02, s2-03 (s1-03 — «Дополнительно»)
        stages = fixture_data()["stages"]
        none = call("readiness", stages, {})
        self.assertEqual((none["left"], none["ready"]), (5, False))
        marked = call("readiness", stages, {"s1-01": True, "s1-02": True})
        self.assertEqual(marked["left"], 3)
        self.assertEqual(marked["text"], "До заявок осталось 3 шага")


@NEED_NODE
class NextStepTest(unittest.TestCase):
    def test_first_unfinished_required_step_is_offered(self):
        self.assertEqual(call("nextStepId", STAGES_FOR_READINESS, {}), "s1-01")
        self.assertEqual(call("nextStepId", STAGES_FOR_READINESS, {"s1-01": True, "s1-02": True}), "s2-01")

    def test_optional_and_later_stages_are_never_offered(self):
        done = {"s1-01": True, "s1-02": True, "s2-01": True, "s2-02": True}
        self.assertEqual(call("nextStepId", STAGES_FOR_READINESS, done), "s3-01")
        done["s3-01"] = True
        self.assertIsNone(call("nextStepId", STAGES_FOR_READINESS, done))


@NEED_NODE
class UnfilledTokensTest(unittest.TestCase):
    def test_lists_placeholders_that_stayed_in_the_text(self):
        text = "{{order}} Я на месте. Мой телефон {{phone}}, ещё раз {{order}}"
        self.assertEqual(call("unfilledTokens", text, {}), ["order", "phone"])
        self.assertEqual(call("unfilledTokens", text, {"order": "5"}), ["phone"])
        self.assertEqual(call("unfilledTokens", text, {"order": "5", "phone": "8900"}), [])


@NEED_NODE
class ProgressNumbersTest(unittest.TestCase):
    def test_stage_and_overall_progress_count_every_step(self):
        stages = fixture_data()["stages"]
        progress = {"s1-01": True, "s1-03": True, "s2-02": True}
        self.assertEqual(call("stageProgress", stages[0], progress), {"done": 2, "total": 3})
        self.assertEqual(call("stageProgress", stages[1], progress), {"done": 1, "total": 3})
        self.assertEqual(call("overallProgress", stages, progress), {"done": 3, "total": 6})

    def test_unknown_ids_do_not_count(self):
        stages = fixture_data()["stages"]
        self.assertEqual(call("overallProgress", stages, {"s7-01": True}), {"done": 0, "total": 6})


@NEED_NODE
class StepsWordTest(unittest.TestCase):
    def test_russian_plural_forms(self):
        expected = {0: "шагов", 1: "шаг", 2: "шага", 4: "шага", 5: "шагов", 11: "шагов", 12: "шагов",
                    14: "шагов", 21: "шаг", 22: "шага", 25: "шагов", 101: "шаг", 111: "шагов"}
        for number, word in expected.items():
            with self.subTest(number=number):
                self.assertEqual(call("stepsWord", number), word)


@NEED_NODE
class SavedProgressTest(unittest.TestCase):
    KNOWN = ["s1-01", "s1-02", "s2-01"]

    def test_valid_state_is_restored(self):
        self.assertEqual(call("parseProgress", '{"s1-01":true,"s2-01":true}', self.KNOWN), {"s1-01": True, "s2-01": True})

    def test_unknown_ids_are_ignored(self):
        self.assertEqual(call("parseProgress", '{"s1-01":true,"s9-99":true}', self.KNOWN), {"s1-01": True})

    def test_only_true_counts(self):
        raw = '{"s1-01":false,"s1-02":"yes","s2-01":1}'
        self.assertEqual(call("parseProgress", raw, self.KNOWN), {})

    def test_garbage_gives_empty_state(self):
        for raw in (None, "", "не json", "[1,2]", "null", "42", '"s1-01"', "{"):
            with self.subTest(raw=raw):
                self.assertEqual(call("parseProgress", raw, self.KNOWN), {})

    def test_saved_form_is_object_of_step_ids(self):
        saved = call("serializeProgress", {"s1-01": True, "s9-99": True, "s1-02": False}, self.KNOWN)
        self.assertEqual(json.loads(saved), {"s1-01": True})

    def test_storage_key(self):
        self.assertEqual(call("storageKey"), "masterGuide.progress.v1")


@NEED_NODE
class SmallHelpersTest(unittest.TestCase):
    def test_done_button_label_by_kind(self):
        self.assertEqual(call("doneLabel", "do"), "Сделано")
        self.assertEqual(call("doneLabel", "buy"), "Куплено")
        self.assertEqual(call("doneLabel", "know"), "Прочитал")

    def test_only_https_links_are_clickable(self):
        self.assertEqual(call("safeUrl", "https://example.com/google-play"), "https://example.com/google-play")
        for bad in ("javascript:alert(1)", "http://example.com", "data:text/html,x", "//example.com", "", None):
            with self.subTest(url=bad):
                self.assertEqual(call("safeUrl", bad), "")


def hex_to_rgb(value: str):
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def luminance(value: str) -> float:
    parts = []
    for channel in hex_to_rgb(value):
        c = channel / 255
        parts.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def contrast(fg: str, bg: str) -> float:
    a, b = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (a + 0.05) / (b + 0.05)


def unguarded_accesses(source: str, names=("localStorage", "navigator.clipboard")) -> list:
    """Номера строк, где есть обращение к хранилищу или буферу обмена без `try {` на этой или 4 строках выше."""
    lines = source.splitlines()
    bad = []
    for index, line in enumerate(lines):
        if not any(name in line for name in names):
            continue
        window = lines[max(0, index - 4):index + 1]
        if not any(re.search(r'\btry\s*\{', item) for item in window):
            bad.append(index + 1)
    return bad


def theme_variables(css_block: str) -> dict:
    return dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,6})\b", css_block))


class PageTemplateTest(unittest.TestCase):
    """Проверки самого файла шаблона и сборки на фикстурах (node не нужен)."""

    @classmethod
    def setUpClass(cls):
        cls.text = template_text()

    def test_single_data_marker(self):
        self.assertEqual(self.text.count("<!--GUIDE_DATA-->"), 1)

    def test_logic_block_is_present_once_and_in_order(self):
        self.assertEqual(self.text.count(LOGIC_START), 1)
        self.assertEqual(self.text.count(LOGIC_END), 1)
        self.assertLess(self.text.index(LOGIC_START), self.text.index(LOGIC_END))

    def test_no_external_addresses_or_network_calls(self):
        self.assertIsNone(re.search(r"https?://", self.text), "в шаблоне не должно быть внешних адресов")
        self.assertIsNone(re.search(r"(?:src|href|action|poster|srcset)\s*=\s*[\"']?\s*//", self.text))
        for forbidden in ("@import", "<link", "<iframe", "<img", "<script src", "<script type=\"module\"", "fetch(",
                          "XMLHttpRequest", "sendBeacon", "WebSocket", "importScripts", "url(", "@font-face"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.text)

    def test_data_is_never_inserted_as_html(self):
        for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                          "srcdoc", "DOMParser"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.text)

    def test_storage_and_clipboard_access_is_guarded_by_try(self):
        # сам детектор видит голое обращение и не придирается к защищённому
        bare = "const a = 1;\nwindow.localStorage.getItem('x');"
        guarded = "try {\n  window.localStorage.getItem('x');\n} catch (e) {}"
        self.assertEqual(unguarded_accesses(bare), [2])
        self.assertEqual(unguarded_accesses(guarded), [])
        self.assertEqual(unguarded_accesses(self.text), [], "обращения к localStorage и clipboard должны быть в try")

    def test_print_view_hides_panels_and_buttons(self):
        css = self.text.split("@media print", 1)[1].split("</style>", 1)[0]
        for selector in (".dock", ".sheet", "button", ".page-head", ".howto"):
            with self.subTest(selector=selector):
                self.assertIn(selector, css)
        self.assertIn("display: none", css)

    def test_phone_page_basics(self):
        self.assertIn('<html lang="ru"', self.text)
        self.assertRegex(self.text, r'<meta name="viewport" content="[^"]*width=device-width')
        self.assertIn("@media print", self.text)
        self.assertIn("prefers-color-scheme: dark", self.text)
        self.assertIn(":focus-visible", self.text)

    def test_ui_strings_required_by_the_spec_are_present(self):
        for phrase in ("Сделано", "Куплено", "Прочитал", "Скопировать", "Памятка на заявку", "Номер заявки",
                       "Мой телефон с 8", "Прогресс не сохраняется", "Скопируй вручную", "Начать заново",
                       "masterGuide.progress.v1", "Можно писать «Доброе утро» диспетчеру", "До заявок осталось"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)

    def test_text_is_never_smaller_than_16px(self):
        sizes = re.findall(r"font-size\s*:\s*([^;}]+)", self.text)
        self.assertTrue(sizes, "в шаблоне нет ни одного font-size")
        for size in sizes:
            size = size.strip().lower()
            with self.subTest(size=size):
                if size in ("inherit", "100%", "1em", "1rem"):
                    continue
                match = re.fullmatch(r"(\d+(?:\.\d+)?)(px|rem|em|%)", size)
                self.assertIsNotNone(match, f"непонятный размер шрифта: {size}")
                number, unit = float(match.group(1)), match.group(2)
                minimum = {"px": 16, "rem": 1, "em": 1, "%": 100}[unit]
                self.assertGreaterEqual(number, minimum)

    def test_colors_have_aa_contrast_in_light_and_dark(self):
        root_blocks = re.findall(r":root\s*\{([^}]*)\}", self.text)
        self.assertGreaterEqual(len(root_blocks), 2, "нужны переменные цветов для светлой и тёмной темы")
        light = theme_variables(root_blocks[0])
        dark = {**light, **theme_variables(root_blocks[1])}
        text_pairs = [
            ("--text", "--bg"), ("--text", "--surface"), ("--muted", "--bg"), ("--muted", "--surface"),
            ("--on-accent", "--accent"), ("--accent", "--surface"), ("--accent", "--bg"),
            ("--ok-text", "--ok-bg"), ("--err-text", "--err-bg"), ("--notice-text", "--notice-bg"),
        ]
        ui_pairs = [("--border-strong", "--surface"), ("--border-strong", "--bg"), ("--focus", "--bg"),
                    ("--focus", "--surface")]
        for theme_name, variables in (("светлая", light), ("тёмная", dark)):
            for names, minimum in ((text_pairs, 4.5), (ui_pairs, 3.0)):
                for fg, bg in names:
                    with self.subTest(theme=theme_name, fg=fg, bg=bg):
                        self.assertIn(fg, variables)
                        self.assertIn(bg, variables)
                        self.assertGreaterEqual(contrast(variables[fg], variables[bg]), minimum)

    def test_fixture_content_builds_into_a_single_file_page(self):
        with tempfile.TemporaryDirectory() as out:
            done = subprocess.run(
                [sys.executable, str(BUILD_SCRIPT),
                 "--content", str(FIXTURES / "content"), "--profile", str(FIXTURES / "profile.json"),
                 "--template", str(TEMPLATE), "--out", out],
                capture_output=True, timeout=120, cwd=str(ROOT),
            )
            self.assertEqual(done.returncode, 0, done.stdout.decode("utf-8", errors="replace")[-500:]
                             + done.stderr.decode("utf-8", errors="replace")[-500:])
            page = (Path(out) / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("<!--GUIDE_DATA-->", page)
        blocks = re.findall(r'<script type="application/json" id="guide-data">(.*?)</script>', page, flags=re.S)
        self.assertEqual(len(blocks), 1)
        data = json.loads(blocks[0])
        self.assertEqual([stage["id"] for stage in data["stages"]], ["s1", "s2"])
        self.assertIn("reports", data["cheatsheet"])
        # вне блока данных страница не содержит внешних адресов
        outside = page.replace(blocks[0], "")
        self.assertIsNone(re.search(r"https?://", outside))


if __name__ == "__main__":
    unittest.main()
