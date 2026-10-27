"""Тесты графика «Шагов по этапам»: tools/make_charts.py читает guide/content/*.json и пишет SVG.

Ожидаемые числа берутся из временного контента, который тест собирает сам (три этапа с известным
числом шагов), а не из самого графика. Против настоящего контента проверяются только свойства,
которые не зависят от текстов: столбцов столько же, сколько этапов, и файл в репозитории свежий.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from tools import make_charts

ROOT = Path(__file__).resolve().parents[2]
CONTENT_DIR = ROOT / "guide" / "content"
CHART_FILE = ROOT / "docs" / "img" / "steps-by-stage.svg"
CHART_SCRIPT = ROOT / "tools" / "make_charts.py"

SVG_NS = "{http://www.w3.org/2000/svg}"
HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")


def step(step_id, optional=False):
    data = {"id": step_id, "title": "Шаг " + step_id, "kind": "know", "lead": "Текст.", "reqs": ["R01"]}
    if optional:
        data["optional"] = True
    return data


def stage_file(stage_id, title, steps):
    return {"stage": {"id": stage_id, "title": title, "intro": "Вводная."}, "steps": steps}


# три этапа с заранее известным числом шагов: (обязательных, дополнительных)
FIXTURE = [
    ("10-one.json", stage_file("s1", "Этап 1. Первый & главный", [step("s1-01"), step("s1-02"), step("s1-03")])),
    ("20-two.json", stage_file("s2", "Этап 2. Второй «со скобками»", [step("s2-01"), step("s2-02", True), step("s2-03", True)])),
    ("45-extra.json", stage_file("s2b", "Этап 2а. Побочный", [step("s2b-01"), step("s2b-02"), step("s2b-03"), step("s2b-04")])),
    # файл без этапа (как памятка) в график не попадает
    ("90-note.json", {"cheatsheet": {"title": "Памятка", "chain": [], "reports": []}}),
]
EXPECTED = {"s1": (3, 0), "s2": (1, 2), "s2b": (4, 0)}
EXPECTED_TITLES = ["Этап 1. Первый & главный", "Этап 2. Второй «со скобками»", "Этап 2а. Побочный"]
TOTAL_STEPS = 10
OPTIONAL_STEPS = 2


def write_content(directory, files=FIXTURE):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in files:
        (directory / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return directory


def run_main(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = make_charts.main(list(args))
    return code, out.getvalue(), err.getvalue()


def parse(svg_text):
    return ET.fromstring(svg_text.encode("utf-8"))


def bars(root):
    return [g for g in root.iter(SVG_NS + "g") if g.get("class") == "bar"]


def segments(root):
    return [r for r in root.iter(SVG_NS + "rect") if (r.get("class") or "").startswith("seg")]


def channel(value):
    value = value / 255
    return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4


def luminance(color):
    red, green, blue = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def contrast(first, second):
    high, low = sorted((luminance(first), luminance(second)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class ChartContentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.content = write_content(self.tmp / "content")

    def svg(self):
        return make_charts.render_svg(make_charts.load_stages(self.content))

    def test_one_bar_per_stage_in_the_order_of_the_content_files(self):
        root = parse(self.svg())
        self.assertEqual([g.get("data-stage") for g in bars(root)], ["s1", "s2", "s2b"])

    def test_a_file_without_a_stage_is_not_a_bar(self):
        stages = make_charts.load_stages(self.content)
        self.assertEqual(len(stages), 3)

    def test_each_bar_is_split_into_required_and_optional_steps(self):
        root = parse(self.svg())
        found = {}
        for seg in segments(root):
            kind = "optional" if "opt" in seg.get("class").split() else "required"
            found.setdefault(seg.get("data-stage"), {})[kind] = int(seg.get("data-steps"))
        expected = {sid: {"required": req, **({"optional": opt} if opt else {})} for sid, (req, opt) in EXPECTED.items()}
        # у этапа без дополнительных шагов второго куска нет, а у этапа без обязательных (если бы он был) не было бы первого
        self.assertEqual(found, expected)

    def test_bar_lengths_are_proportional_and_add_up_to_the_number_of_steps(self):
        root = parse(self.svg())
        segs = segments(root)
        widths = [float(seg.get("width")) for seg in segs]
        steps = [int(seg.get("data-steps")) for seg in segs]
        self.assertEqual(sum(steps), TOTAL_STEPS)
        ratios = {round(width / count, 6) for width, count in zip(widths, steps)}
        self.assertEqual(len(ratios), 1, "длина куска не пропорциональна числу шагов")
        unit = ratios.pop()
        self.assertGreater(unit, 0)
        self.assertAlmostEqual(sum(widths) / unit, TOTAL_STEPS)

    def test_segments_of_one_bar_sit_one_after_another_without_a_gap(self):
        root = parse(self.svg())
        for group in bars(root):
            segs = [r for r in group.iter(SVG_NS + "rect")]
            for left, right in zip(segs, segs[1:]):
                self.assertAlmostEqual(float(left.get("x")) + float(left.get("width")), float(right.get("x")))

    def test_labels_are_the_stage_titles_with_special_characters_kept(self):
        root = parse(self.svg())
        labels = [t.text for t in root.iter(SVG_NS + "text") if t.get("class") == "label"]
        self.assertEqual(labels, EXPECTED_TITLES)

    def test_every_bar_shows_its_number_of_steps(self):
        root = parse(self.svg())
        counts = {g.get("data-stage"): next(t.text for t in g.iter(SVG_NS + "text") if t.get("class") == "count") for g in bars(root)}
        self.assertEqual(counts["s1"], "3")
        self.assertEqual(counts["s2b"], "4")
        self.assertTrue(counts["s2"].startswith("3"), counts["s2"])
        self.assertIn("2 доп.", counts["s2"])

    def test_heading_and_summary_use_correct_russian_plurals(self):
        text = " ".join(t.text for t in parse(self.svg()).iter(SVG_NS + "text") if t.text)
        self.assertIn("Шагов по этапам", text)
        self.assertIn("Всего 10 шагов", text)
        self.assertIn("2 дополнительных", text)

    def test_plural_forms(self):
        self.assertEqual(make_charts.plural(1, "шаг", "шага", "шагов"), "шаг")
        self.assertEqual(make_charts.plural(3, "шаг", "шага", "шагов"), "шага")
        self.assertEqual(make_charts.plural(11, "шаг", "шага", "шагов"), "шагов")
        self.assertEqual(make_charts.plural(21, "шаг", "шага", "шагов"), "шаг")
        self.assertEqual(make_charts.plural(84, "шаг", "шага", "шагов"), "шага")

    def test_optional_steps_have_their_own_colour_and_a_legend(self):
        root = parse(self.svg())
        fills = {}
        for seg in segments(root):
            fills.setdefault("opt" if "opt" in seg.get("class").split() else "req", set()).add(seg.get("fill"))
        self.assertEqual(set(fills), {"opt", "req"})
        self.assertEqual(len(fills["opt"]), 1)
        self.assertEqual(len(fills["req"]), 1)
        self.assertNotEqual(fills["opt"], fills["req"])
        legend = [t.text for t in root.iter(SVG_NS + "text") if t.get("class") == "legend"]
        self.assertEqual(len(legend), 2)
        self.assertTrue(any("Дополнительно" in item for item in legend), legend)

    def test_without_optional_steps_there_is_no_optional_legend_entry_and_no_empty_segment(self):
        only_required = [FIXTURE[0], FIXTURE[2]]
        root = parse(make_charts.render_svg(make_charts.load_stages(write_content(self.tmp / "plain", only_required))))
        self.assertTrue(all(float(seg.get("width")) > 0 for seg in segments(root)))
        self.assertFalse([seg for seg in segments(root) if "opt" in seg.get("class").split()])
        legend = [t.text for t in root.iter(SVG_NS + "text") if t.get("class") == "legend"]
        self.assertEqual(len(legend), 1)

    def test_text_and_bars_are_readable_on_the_opaque_background_in_any_theme(self):
        root = parse(self.svg())
        background = [r for r in root.iter(SVG_NS + "rect") if r.get("class") == "bg"]
        self.assertEqual(len(background), 1, "нужна одна сплошная подложка, иначе читаемость зависит от темы страницы")
        back = background[0].get("fill")
        self.assertRegex(back, HEX_RE.pattern)
        for text in root.iter(SVG_NS + "text"):
            color = text.get("fill")
            self.assertRegex(color or "", HEX_RE.pattern, f"у текста «{text.text}» нет явного цвета")
            self.assertGreaterEqual(contrast(color, back), 4.5, f"«{text.text}» {color} на {back}")
        for seg in segments(root):
            self.assertGreaterEqual(contrast(seg.get("fill"), back), 3.0, seg.get("class"))

    def test_the_picture_is_a_self_contained_accessible_svg(self):
        text = self.svg()
        root = parse(text)
        self.assertEqual(root.tag, SVG_NS + "svg")
        self.assertEqual(root.get("role"), "img")
        self.assertTrue(root.get("viewBox"))
        self.assertTrue(root.get("width") and root.get("height"))
        self.assertTrue(root.find(SVG_NS + "title").text)
        self.assertTrue(root.find(SVG_NS + "desc").text)
        for banned in ("<script", "http://", "https://", "xlink:href", "<image", "<style"):
            self.assertNotIn(banned, text.replace('xmlns="http://www.w3.org/2000/svg"', ""), banned)

    def test_the_output_is_deterministic_with_lf_line_endings(self):
        first, second = self.svg(), self.svg()
        self.assertEqual(first, second)
        self.assertTrue(first.endswith("\n"))
        self.assertNotIn("\r", first)

    def test_a_long_title_still_fits_the_label_area(self):
        long_title = "Этап 9. " + "очень длинное название этапа " * 3
        files = [("10-long.json", stage_file("s9", long_title, [step("s9-01")]))]
        root = parse(make_charts.render_svg(make_charts.load_stages(write_content(self.tmp / "long", files))))
        label = next(t for t in root.iter(SVG_NS + "text") if t.get("class") == "label")
        first_bar = next(iter(segments(root)))
        self.assertGreater(float(first_bar.get("x")), float(label.get("x")))
        self.assertGreaterEqual(float(label.get("x")), len(long_title) * 7)


class ChartCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.content = write_content(self.tmp / "content")
        self.out = self.tmp / "img" / "chart.svg"

    def write(self):
        return run_main("--content", str(self.content), "--out", str(self.out))

    def check(self):
        return run_main("--content", str(self.content), "--out", str(self.out), "--check")

    def test_without_flags_it_writes_the_file_and_creates_the_folder(self):
        code, out, err = self.write()
        self.assertEqual(code, 0, err)
        self.assertTrue(self.out.is_file())
        parse(self.out.read_text(encoding="utf-8"))

    def test_check_passes_on_a_fresh_file(self):
        self.write()
        code, out, err = self.check()
        self.assertEqual((code, err), (0, ""))

    def test_check_does_not_touch_the_file(self):
        self.write()
        before = self.out.read_bytes()
        self.check()
        self.assertEqual(self.out.read_bytes(), before)

    def test_check_catches_a_stale_chart_after_a_step_is_added(self):
        self.write()
        data = FIXTURE[0][1]
        changed = {**data, "steps": data["steps"] + [step("s1-04")]}
        (self.content / "10-one.json").write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        code, out, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("устарел", out + err)
        self.assertIn("make_charts.py", out + err)

    def test_check_catches_a_hand_edited_file(self):
        self.write()
        self.out.write_text(self.out.read_text(encoding="utf-8").replace("Шагов по этапам", "Шагов"), encoding="utf-8")
        code, out, err = self.check()
        self.assertEqual(code, 1)

    def test_check_fails_when_the_file_is_missing(self):
        code, out, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("нет файла", out + err)

    def test_check_ignores_crlf_made_by_a_checkout(self):
        self.write()
        self.out.write_bytes(self.out.read_bytes().replace(b"\n", b"\r\n"))
        code, out, err = self.check()
        self.assertEqual(code, 0, out + err)

    def test_an_empty_content_folder_is_a_clear_error_not_a_traceback(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        code, out, err = run_main("--content", str(empty), "--out", str(self.out))
        self.assertEqual(code, 2)
        self.assertIn("этап", out + err)
        self.assertFalse(self.out.exists())

    def test_a_broken_json_file_is_reported_with_its_name(self):
        (self.content / "50-broken.json").write_text("{ не json", encoding="utf-8")
        code, out, err = self.write()
        self.assertEqual(code, 2)
        self.assertIn("50-broken.json", out + err)
        self.assertFalse(self.out.exists())


class ChartConsoleEncodingTests(unittest.TestCase):
    """Скрипт запущен как программа: вывод и сообщения об ошибках идут в UTF-8, какой бы ни была кодировка консоли.

    На русской Windows консоль и пайпы работают в cp1251 или cp866; без явной кодировки кириллица
    в выводе превращается в нечитаемые байты (так же сделаны build.py и check_public.py).
    """

    CONSOLES = ("cp1251", "cp866")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def decode(self, raw, console):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            self.fail(f"при кодировке консоли {console} вывод не в UTF-8: {raw[:60]!r}")

    def run_script(self, console, *args):
        env = {**os.environ, "PYTHONIOENCODING": console}
        env.pop("PYTHONUTF8", None)
        done = subprocess.run([sys.executable, str(CHART_SCRIPT), *args], capture_output=True, env=env, timeout=60, check=False)
        return done.returncode, self.decode(done.stdout, console), self.decode(done.stderr, console)

    def test_the_check_message_is_utf8_whatever_the_console_encoding_is(self):
        for console in self.CONSOLES:
            with self.subTest(console):
                code, out, err = self.run_script(console, "--check")
                self.assertEqual(code, 0, out + err)
                self.assertIn("График актуален", out)

    def test_the_message_about_a_written_file_is_utf8_too(self):
        content = write_content(self.tmp / "content")
        for console in self.CONSOLES:
            with self.subTest(console):
                code, out, err = self.run_script(console, "--content", str(content), "--out", str(self.tmp / f"{console}.svg"))
                self.assertEqual(code, 0, err)
                self.assertIn("Записан", out)

    def test_an_error_goes_to_stderr_in_utf8(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        for console in self.CONSOLES:
            with self.subTest(console):
                code, out, err = self.run_script(console, "--content", str(empty), "--out", str(self.tmp / "none.svg"))
                self.assertEqual(code, 2)
                self.assertIn("Ошибка", err)
                self.assertIn("нет ни одного файла с этапом", err)

    def test_the_stale_chart_message_is_utf8_too(self):
        content = write_content(self.tmp / "content")
        out_file = self.tmp / "chart.svg"
        out_file.write_text("<svg/>", encoding="utf-8")
        for console in self.CONSOLES:
            with self.subTest(console):
                code, out, err = self.run_script(console, "--content", str(content), "--out", str(out_file), "--check")
                self.assertEqual(code, 1)
                self.assertIn("Проверка не пройдена", out)
                self.assertIn("устарел", out)


class RepositoryChartTests(unittest.TestCase):
    def count_stage_files(self):
        found = 0
        for path in sorted(CONTENT_DIR.glob("*.json")):
            if "stage" in json.loads(path.read_text(encoding="utf-8")):
                found += 1
        return found

    def test_the_committed_chart_is_up_to_date(self):
        code, out, err = run_main("--check")
        self.assertEqual(code, 0, out + err)

    def test_the_committed_chart_has_a_bar_for_every_stage_and_counts_every_step(self):
        self.assertTrue(CHART_FILE.is_file(), "нет docs/img/steps-by-stage.svg: запусти python tools/make_charts.py")
        root = parse(CHART_FILE.read_text(encoding="utf-8"))
        self.assertEqual(len(bars(root)), self.count_stage_files())
        total = 0
        for path in sorted(CONTENT_DIR.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            total += len(data.get("steps", []))
        self.assertEqual(sum(int(seg.get("data-steps")) for seg in segments(root)), total)


if __name__ == "__main__":
    unittest.main()
