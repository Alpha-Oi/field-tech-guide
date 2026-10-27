"""Падежи имени техдира: вместо одной переменной techdir_name — четыре формы (кто, кого, кому, кем).

Ожидаемые слова взяты из задания и правил русского языка, а не из проверяемых файлов.
Настоящий профиль сюда не попадает: в публичный тест значения компании не идут.
"""

import contextlib
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from guide import build

ROOT = Path(__file__).resolve().parents[2]
CONTENT_DIR = ROOT / "guide" / "content"
DEMO_PROFILE = ROOT / "guide" / "profiles" / "demo.json"
VARS_DOC = ROOT / "guide" / "profiles" / "VARS.md"
TECHDIR_TEMPLATE = ROOT / "guide" / "docs" / "for-techdir.md"

OLD_NAME = "techdir_name"
# имя формы -> слово «техдир» в нужном падеже (демо-значения)
FORMS = {
    "techdir_nom": "техдир",
    "techdir_gen": "техдира",
    "techdir_dat": "техдиру",
    "techdir_ins": "техдиром",
}

# предлоги и слова, после которых именительный падеж невозможен
NOMINATIVE_AFTER = (
    r"(?:\bк|\bу|\bот|\bдля|\bбез|\bпо словам|\bРекомендация|\bОриентир|\bСовет|\bВывод|\bрюкзака)"
)


def run_build(*args):
    """build.main в этом же процессе; возвращает (код, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = build.main(list(args))
    return code, out.getvalue(), err.getvalue()


def read(path):
    return Path(path).read_text(encoding="utf-8")


def texts_with_old_name():
    """Файлы, где раньше стояла одна переменная: тексты, записка, демо-профиль, описание переменных."""
    files = sorted(CONTENT_DIR.glob("*.json")) + [TECHDIR_TEMPLATE, DEMO_PROFILE, VARS_DOC]
    return [path for path in files if path.is_file()]


class OldVariableTests(unittest.TestCase):
    def test_old_variable_is_gone_from_content_note_profile_and_vars_doc(self):
        left = [path.name for path in texts_with_old_name() if OLD_NAME in read(path)]
        self.assertEqual(left, [], "осталась переменная techdir_name")


class DemoProfileTests(unittest.TestCase):
    def demo_vars(self):
        return json.loads(read(DEMO_PROFILE))["vars"]

    def test_demo_profile_has_the_four_forms_with_right_cases(self):
        values = self.demo_vars()
        for name, word in FORMS.items():
            self.assertEqual(values.get(name), word, name)

    def test_demo_profile_has_no_old_variable(self):
        self.assertNotIn(OLD_NAME, self.demo_vars())

    def test_list_vars_prints_the_four_forms_and_not_the_old_one(self):
        code, out, err = run_build("--list-vars")
        self.assertEqual(code, 0, err)
        names = out.split()
        for name in FORMS:
            self.assertIn(name, names)
        self.assertNotIn(OLD_NAME, names)

    def test_vars_doc_describes_each_form_with_its_demo_example(self):
        rows = {}
        for line in read(VARS_DOC).splitlines():
            match = re.match(r"\| `([a-z0-9_]+)` \|", line)
            if match:
                rows[match.group(1)] = [cell.strip() for cell in line.strip().strip("|").split("|")]
        for name, word in FORMS.items():
            self.assertIn(name, rows, f"в VARS.md нет строки {name}")
            self.assertEqual(rows[name][2], word, f"пример для {name}")
            self.assertTrue(rows[name][1], f"нет смысла для {name}")


class RenderedCasesTests(unittest.TestCase):
    """Собранный гайд: предлоги не стоят перед именительным падежом, формы стоят по смыслу фразы."""

    @classmethod
    def build_into(cls, tmp, profile_path=None):
        args = ["--out", tmp]
        if profile_path is not None:
            args += ["--profile", str(profile_path)]
        code, _out, err = run_build(*args)
        if code != 0:
            raise AssertionError(f"сборка не прошла: {err}")
        return {name: read(Path(tmp) / name) for name in ("index.html", "guide.md", "for-techdir.md")}

    def test_demo_guide_has_no_preposition_before_nominative(self):
        with tempfile.TemporaryDirectory() as tmp:
            texts = self.build_into(tmp)
        for name, text in texts.items():
            for bad in ("к техдир ", "у техдир ", "к техдир,", "у техдир,", "к техдир.", "у техдир."):
                self.assertNotIn(bad, text, f"{name}: «{bad}»")

    def test_demo_guide_text_uses_dative_and_genitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            md = self.build_into(tmp)["guide.md"]
        self.assertIn("техдиру", md)
        self.assertIn("техдира", md)
        self.assertIn("обратись к техдиру", md)

    def test_each_place_gets_the_form_its_phrase_needs(self):
        # у каждой формы своё нелепое «слово»: по нему видно, какая форма встала в какую фразу
        marks = {
            "techdir_nom": "ИМЕНИТ",
            "techdir_gen": "РОДИТ",
            "techdir_dat": "ДАТЕЛЬ",
            "techdir_ins": "ТВОРИТ",
        }
        profile = json.loads(read(DEMO_PROFILE))
        profile["vars"].pop(OLD_NAME, None)
        profile["vars"].update(marks)
        with tempfile.TemporaryDirectory() as tmp:
            profile_path = Path(tmp) / "profile.json"
            profile_path.write_text(json.dumps(profile, ensure_ascii=False), encoding="utf-8")
            out_dir = Path(tmp) / "out"
            out_dir.mkdir()
            texts = self.build_into(str(out_dir), profile_path)
        md = texts["guide.md"]
        # примеры фраз из задания: «обратись к ...», «по словам ...», «Рекомендация ...»
        self.assertIn("обратись к ДАТЕЛЬ", md)
        self.assertIn("по словам РОДИТ", md)
        self.assertIn("Рекомендация РОДИТ", md)
        for name, text in texts.items():
            # после предлогов и «Ориентир/Совет/Вывод» именительного падежа нет
            self.assertIsNone(re.search(NOMINATIVE_AFTER + r"\s+ИМЕНИТ", text), name)
            # дательный и творительный не стоят там, где нужен родительный
            self.assertIsNone(re.search(r"(?:по словам|Рекомендация|Ориентир|Совет|Вывод)\s+(?:ДАТЕЛЬ|ТВОРИТ)", text), name)


if __name__ == "__main__":
    unittest.main()
