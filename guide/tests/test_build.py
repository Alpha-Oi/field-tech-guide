"""Тесты движка сборки guide/build.py — на фикстурах из guide/tests/fixtures/.

Ожидаемые значения выведены вручную из фикстур и из interfaces.md (U1–U3),
а не из кода сборки.
"""

import contextlib
import copy
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from guide import build
from guide.tests.test_page_logic import NEED_NODE, call

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONTENT = FIXTURES / "content"
PROFILE = FIXTURES / "profile.json"
TEMPLATE = FIXTURES / "template.html"
MANIFEST = FIXTURES / "manifest.md"
TECHDIR_TEMPLATE = FIXTURES / "for-techdir.md"
BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "build.py"


def raw_data():
    """Контент фикстуры до подстановки профиля."""
    return build.load_content(str(CONTENT))


def fixture_profile():
    return build.load_profile(str(PROFILE))


def substituted():
    """Контент фикстуры после подстановки профиля — то, что проверяет validate."""
    return build.apply_profile(raw_data(), fixture_profile())


def find_step(data, step_id):
    for stage in data["stages"]:
        for step in stage["steps"]:
            if step["id"] == step_id:
                return step
    raise KeyError(step_id)


def find_report(data, report_id):
    for report in data["cheatsheet"]["reports"]:
        if report["id"] == report_id:
            return report
    raise KeyError(report_id)


def write_json(path, obj):
    Path(path).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def run_cli(args):
    """Запуск build.main в этом же процессе; возвращает (код, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = build.main(list(args))
    return code, out.getvalue(), err.getvalue()


class LoadContentTests(unittest.TestCase):
    def test_stages_follow_file_name_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            # имена: «10-…» раньше «20-…», хотя id этапов идут наоборот
            write_json(Path(tmp) / "20-b.json", {"stage": {"id": "s1", "title": "Б"}, "steps": []})
            write_json(Path(tmp) / "10-a.json", {"stage": {"id": "s9", "title": "А"}, "steps": []})
            data = build.load_content(tmp)
        self.assertEqual([s["id"] for s in data["stages"]], ["s9", "s1"])

    def test_fixture_stages_and_steps(self):
        data = raw_data()
        self.assertEqual([s["id"] for s in data["stages"]], ["s1", "s2"])
        first = data["stages"][0]
        self.assertEqual(first["title"], "Старт: приложение и чат")
        self.assertEqual(first["intro"], "Ставим приложение и знакомимся с диспетчером.")
        self.assertEqual([s["id"] for s in first["steps"]], ["s1-01", "s1-02", "s1-03"])
        self.assertEqual(first["steps"][0]["links"][0]["url"], "{{link_play}}")
        self.assertIs(first["steps"][2]["optional"], True)

    def test_fixture_cheatsheet(self):
        cheatsheet = raw_data()["cheatsheet"]
        self.assertEqual(cheatsheet["title"], "Памятка на заявку")
        self.assertEqual(len(cheatsheet["chain"]), 3)
        self.assertEqual(cheatsheet["reports"][0]["id"], "onsite")

    def test_empty_folder_says_no_content_files(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(build.ContentError) as ctx:
            build.load_content(tmp)
        self.assertIn("нет файлов контента", str(ctx.exception))

    def test_missing_folder_is_content_error(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(build.ContentError) as ctx:
            build.load_content(str(Path(tmp) / "nope"))
        self.assertIn("нет файлов контента", str(ctx.exception))

    def test_broken_json_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "10-bad.json").write_text("{ не json", encoding="utf-8")
            with self.assertRaises(build.ContentError) as ctx:
                build.load_content(tmp)
        self.assertIn("10-bad.json", str(ctx.exception))

    def test_file_with_neither_stage_nor_cheatsheet_names_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp) / "10-odd.json", {"hello": 1})
            with self.assertRaises(build.ContentError) as ctx:
                build.load_content(tmp)
        self.assertIn("10-odd.json", str(ctx.exception))

    def test_ignores_non_json_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp) / "10-a.json", {"stage": {"id": "s1", "title": "А"}, "steps": []})
            (Path(tmp) / "notes.txt").write_text("не контент", encoding="utf-8")
            data = build.load_content(tmp)
        self.assertEqual(len(data["stages"]), 1)


class LoadProfileTests(unittest.TestCase):
    def test_fixture_profile(self):
        profile = fixture_profile()
        self.assertEqual(profile["name"], "fixture")
        self.assertEqual(profile["vars"]["dispatcher_handle"], "@dispatcher")

    def test_missing_file_is_content_error(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(build.ContentError):
            build.load_profile(str(Path(tmp) / "nope.json"))

    def test_non_string_value_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            write_json(path, {"name": "x", "vars": {"app_name": 5}})
            with self.assertRaises(build.ContentError) as ctx:
                build.load_profile(str(path))
        self.assertIn("app_name", str(ctx.exception))

    def test_runtime_names_are_reserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            write_json(path, {"name": "x", "vars": {"order": "1"}})
            with self.assertRaises(build.ContentError) as ctx:
                build.load_profile(str(path))
        self.assertIn("order", str(ctx.exception))

    def test_variable_name_with_a_trailing_newline_or_non_latin_letters_is_rejected(self):
        for key in ("app_name\n", "прил", "app١"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "p.json"
                write_json(path, {"name": "x", "vars": {key: "1"}})
                with self.assertRaises(build.ContentError):
                    build.load_profile(str(path))


class ApplyProfileTests(unittest.TestCase):
    def test_substitutes_in_titles_nested_lists_and_objects(self):
        data = substituted()
        step = find_step(data, "s1-01")
        self.assertEqual(step["title"], "Скачай Приложение службы")
        self.assertEqual(step["points"][1], "Найди **Приложение службы** и нажми «Установить»")
        self.assertEqual(step["links"][0]["url"], "https://example.com/google-play")
        self.assertEqual(step["links"][1]["url"], "https://example.com/app-store")
        step2 = find_step(data, "s1-02")
        self.assertTrue(step2["send"][0]["text"].startswith("@dispatcher Здравствуйте!"))
        self.assertEqual(step2["tip"], "Если техдир ещё не добавил тебя в чат, подожди его сообщения.")

    def test_no_profile_tokens_left_and_input_not_modified(self):
        data = raw_data()
        before = copy.deepcopy(data)
        result = build.apply_profile(data, fixture_profile())
        self.assertEqual(data, before)
        self.assertIsNot(result, data)
        for name in ("app_name", "dispatcher_handle", "techdir_name", "link_play", "link_apple"):
            self.assertNotIn("{{" + name + "}}", json.dumps(result, ensure_ascii=False))

    def test_runtime_tokens_stay_untouched(self):
        data = substituted()
        self.assertEqual(find_step(data, "s2-03")["send"][0]["text"], "{{order}} Я на месте. Мой телефон {{phone}}")
        self.assertEqual(data["cheatsheet"]["chain"][0]["chat"], "{{order}} Принял")

    def test_report_keys_stay_untouched(self):
        report = find_report(substituted(), "onsite")
        self.assertIn("Всего  - {{total}}", report["template"])
        self.assertIn("Чистыми {{net}}", report["template"])
        self.assertEqual(report["note"], "В поле «Сумма» в приложении впиши: {{net}}")

    def test_profile_variable_inside_report_note_is_substituted(self):
        data = raw_data()
        find_report(data, "onsite")["note"] = "В поле «Сумма» в {{app_name}} впиши: {{net}}"
        note = find_report(build.apply_profile(data, fixture_profile()), "onsite")["note"]
        self.assertEqual(note, "В поле «Сумма» в Приложение службы впиши: {{net}}")

    def test_unknown_variable_raises_value_error_with_its_name(self):
        profile = fixture_profile()
        del profile["vars"]["link_play"]
        with self.assertRaises(ValueError) as ctx:
            build.apply_profile(raw_data(), profile)
        self.assertIn("link_play", str(ctx.exception))
        self.assertNotIn("link_apple", str(ctx.exception))

    def test_all_unknown_variables_are_named_at_once(self):
        profile = {"name": "empty", "vars": {}}
        with self.assertRaises(ValueError) as ctx:
            build.apply_profile(raw_data(), profile)
        for name in ("app_name", "dispatcher_handle", "link_apple", "link_play", "techdir_name"):
            self.assertIn(name, str(ctx.exception))

    def test_non_strict_leaves_unknown_tokens_in_place(self):
        profile = fixture_profile()
        del profile["vars"]["link_play"]
        result = build.apply_profile(raw_data(), profile, strict=False)
        self.assertEqual(find_step(result, "s1-01")["links"][0]["url"], "{{link_play}}")
        self.assertEqual(find_step(result, "s1-01")["links"][1]["url"], "https://example.com/app-store")

    def test_substituted_value_is_not_expanded_again(self):
        profile = {"name": "x", "vars": dict(fixture_profile()["vars"], app_name="{{dispatcher_handle}}")}
        result = build.apply_profile(raw_data(), profile)
        self.assertEqual(find_step(result, "s1-01")["title"], "Скачай {{dispatcher_handle}}")


class CollectVarsTests(unittest.TestCase):
    def test_fixture_variables_sorted_without_runtime_or_report_keys(self):
        self.assertEqual(
            build.collect_vars(raw_data()),
            ["app_name", "dispatcher_handle", "link_apple", "link_play", "techdir_name"],
        )

    def test_variable_in_report_note_counts_but_report_key_does_not(self):
        data = raw_data()
        find_report(data, "onsite")["note"] = "Впиши в {{extra_name}}: {{net}}"
        self.assertIn("extra_name", build.collect_vars(data))
        self.assertNotIn("net", build.collect_vars(data))
        self.assertNotIn("total", build.collect_vars(data))


class ValidateTests(unittest.TestCase):
    """Каждый случай из критериев приёмки даёт ровно одну русскую ошибку с id шага."""

    def errors_after(self, mutate):
        data = substituted()
        mutate(data)
        return build.validate(data)

    def assert_single_error(self, errors, *fragments):
        self.assertEqual(len(errors), 1, errors)
        for fragment in fragments:
            self.assertIn(fragment, errors[0])

    def test_fixture_content_is_valid(self):
        self.assertEqual(build.validate(substituted()), [])

    def test_duplicate_step_id(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-03").update(id="s1-02"))
        self.assert_single_error(errors, "s1-02", "повтор")

    def test_kind_outside_do_buy_know(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-02").update(kind="watch"))
        self.assert_single_error(errors, "s1-02", "kind", "watch")

    def test_do_step_without_proof(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-01").pop("proof"))
        self.assert_single_error(errors, "s1-01", "proof")

    def test_buy_step_without_proof(self):
        errors = self.errors_after(lambda d: find_step(d, "s2-01").pop("proof"))
        self.assert_single_error(errors, "s2-01", "proof")

    def test_know_step_needs_no_proof(self):
        step = find_step(substituted(), "s1-03")
        self.assertNotIn("proof", step)

    def test_proof_must_start_with_gotovo_comma(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-01").update(proof="Должно быть установлено."))
        self.assert_single_error(errors, "s1-01", "Готово,")

    def test_proof_without_comma_is_not_enough(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-01").update(proof="Готово когда всё сделано."))
        self.assert_single_error(errors, "s1-01", "Готово,")

    def test_link_not_https(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-01")["links"][0].update(url="http://example.com/x"))
        self.assert_single_error(errors, "s1-01", "https://", "http://example.com/x")

    def test_link_checked_after_profile_substitution(self):
        profile = fixture_profile()
        profile["vars"]["link_play"] = "ftp://example.com/play"
        data = build.apply_profile(raw_data(), profile)
        self.assert_single_error(build.validate(data), "s1-01", "ftp://example.com/play")

    def test_unresolved_profile_token_in_url_is_not_a_link_error(self):
        # неизвестная переменная в режиме --lenient-vars сообщается отдельным списком
        profile = fixture_profile()
        del profile["vars"]["link_play"]
        data = build.apply_profile(raw_data(), profile, strict=False)
        self.assertEqual(build.validate(data), [])

    def test_order_token_outside_send_text(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-01").update(lead="Скажи номер {{order}} диспетчеру."))
        self.assert_single_error(errors, "s1-01", "{{order}}", "lead")

    def test_phone_token_outside_send_text(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-02").update(tip="Позвони на {{phone}}."))
        self.assert_single_error(errors, "s1-02", "{{phone}}", "tip")

    def test_runtime_tokens_allowed_in_send_text_and_chain_chat(self):
        data = substituted()
        self.assertIn("{{order}}", find_step(data, "s2-03")["send"][0]["text"])
        self.assertIn("{{order}}", data["cheatsheet"]["chain"][0]["chat"])
        self.assertEqual(build.validate(data), [])

    def test_runtime_token_in_chain_when_is_an_error(self):
        errors = self.errors_after(lambda d: d["cheatsheet"]["chain"][0].update(when="Принял {{order}}"))
        self.assert_single_error(errors, "{{order}}", "when")

    def test_report_template_token_not_in_fields_or_computed(self):
        def mutate(data):
            report = find_report(data, "onsite")
            report["template"] += "\n{{missing_key}}"

        errors = self.errors_after(mutate)
        self.assert_single_error(errors, "onsite", "missing_key")

    def test_report_template_may_use_computed_key_and_runtime_tokens(self):
        report = find_report(substituted(), "onsite")
        self.assertIn("{{net}}", report["template"])
        self.assertIn("{{order}}", report["template"])
        self.assertEqual(build.validate(substituted()), [])

    def test_step_id_format(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-03").update(id="s1-3"))
        self.assert_single_error(errors, "s1-3")

    def test_step_id_with_stage_letter_is_allowed(self):
        def mutate(data):
            data["stages"][1]["id"] = "s2b"
            for index, step in enumerate(data["stages"][1]["steps"], start=1):
                step["id"] = f"s2b-{index:02d}"

        self.assertEqual(self.errors_after(mutate), [])

    def test_step_id_must_belong_to_its_stage(self):
        errors = self.errors_after(lambda d: find_step(d, "s2-03").update(id="s1-09"))
        self.assert_single_error(errors, "s1-09", "s2")

    def test_missing_required_field(self):
        for field in ("title", "lead", "reqs", "kind"):
            with self.subTest(field=field):
                errors = self.errors_after(lambda d, name=field: find_step(d, "s1-02").pop(name))
                self.assert_single_error(errors, "s1-02", field)

    def test_bad_requirement_id(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-02").update(reqs=["R08", "готово"]))
        self.assert_single_error(errors, "s1-02", "готово")

    def test_unknown_step_field(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-02").update(optinal=True))
        self.assert_single_error(errors, "s1-02", "optinal")

    def test_send_item_without_text(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-02")["send"][0].pop("text"))
        self.assert_single_error(errors, "s1-02", "send")

    def test_stray_braces_are_reported(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-02").update(lead="Привет, {{app_name!"))
        self.assert_single_error(errors, "s1-02", "{{")

    def test_no_stages(self):
        errors = build.validate({"stages": [], "cheatsheet": substituted()["cheatsheet"]})
        self.assertEqual(len(errors), 1, errors)

    def test_missing_cheatsheet(self):
        data = substituted()
        data["cheatsheet"] = None
        errors = build.validate(data)
        self.assert_single_error(errors, "памятк")

    def test_report_field_with_bad_type(self):
        errors = self.errors_after(lambda d: find_report(d, "onsite")["fields"][2].update(type="date"))
        self.assert_single_error(errors, "onsite", "works", "date")

    def test_computed_refers_to_text_field(self):
        errors = self.errors_after(lambda d: find_report(d, "onsite")["computed"][0].update(a="works"))
        self.assert_single_error(errors, "onsite", "net", "works")

    def test_computed_op_other_than_sub(self):
        errors = self.errors_after(lambda d: find_report(d, "onsite")["computed"][0].update(op="add"))
        self.assert_single_error(errors, "onsite", "net", "add")

    def test_computed_refers_to_unknown_field(self):
        errors = self.errors_after(lambda d: find_report(d, "onsite")["computed"][0].update(b="nothing"))
        self.assert_single_error(errors, "onsite", "net", "nothing")

    def test_duplicate_report_id(self):
        def mutate(data):
            data["cheatsheet"]["reports"].append(copy.deepcopy(find_report(data, "onsite")))

        errors = self.errors_after(mutate)
        self.assert_single_error(errors, "onsite", "повтор")

    # --- ссылки на вычисляемые поля: странные значения — ошибка, а не трассировка ---

    def test_computed_reference_that_is_not_a_field_name_is_an_error_not_a_crash(self):
        for bad in ([], {}, ["total"], {"total": 1}, 5, None, True):
            for side in ("a", "b"):
                with self.subTest(side=side, value=bad):
                    errors = self.errors_after(
                        lambda d, s=side, v=bad: find_report(d, "onsite")["computed"][0].update({s: v})
                    )
                    self.assert_single_error(errors, "onsite", "net")

    # --- идентификаторы: строго, без перевода строки и чужих цифр ---

    def test_step_id_with_a_trailing_newline_is_rejected(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-03").update(id="s1-03\n"))
        self.assertTrue(any("id должен выглядеть как" in e for e in errors), errors)

    def test_step_id_with_non_ascii_digits_is_rejected(self):
        errors = self.errors_after(lambda d: find_step(d, "s1-03").update(id="s1-٠٣"))
        self.assertTrue(any("id должен выглядеть как" in e for e in errors), errors)
        errors = self.errors_after(lambda d: find_step(d, "s1-03").update(id="s١-03"))
        self.assertTrue(any("id должен выглядеть как" in e for e in errors), errors)

    def test_stage_id_with_a_trailing_newline_or_non_ascii_digit_is_rejected(self):
        for bad in ("s2\n", "s٢"):
            with self.subTest(stage_id=bad):
                errors = self.errors_after(lambda d, v=bad: d["stages"][1].update(id=v))
                self.assertTrue(any("id этапа должен выглядеть" in e for e in errors), errors)

    def test_requirement_id_with_a_trailing_newline_or_non_ascii_digits_is_rejected(self):
        for bad in ("R05\n", "R٠٥", "G18\n"):
            with self.subTest(req=bad):
                errors = self.errors_after(lambda d, v=bad: find_step(d, "s1-02").update(reqs=["R08", v]))
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("идентификатор требования", errors[0])

    def test_report_key_with_a_trailing_newline_is_rejected(self):
        errors = self.errors_after(lambda d: find_report(d, "onsite")["fields"][0].update(key="total\n"))
        self.assertTrue(any("fields[0].key" in e for e in errors), errors)
        errors = self.errors_after(lambda d: find_report(d, "onsite")["computed"][0].update(key="net\n"))
        self.assertTrue(any("computed[0].key" in e for e in errors), errors)

    # --- ссылки: одно правило со страницей — https:// и хотя бы один символ без пробелов ---

    def link_errors(self, url):
        return self.errors_after(lambda d: find_step(d, "s1-01")["links"][0].update(url=url))

    def test_link_with_a_space_or_without_a_host_is_an_error(self):
        for url in ("https://x y", "https://", "https://example.com/a b", "https://x\ty", "https://x\n",
                    "https://x\u00a0y", "https://x\ufeffy", " https://example.com"):
            with self.subTest(url=url):
                errors = self.link_errors(url)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("s1-01", errors[0])
                self.assertIn("https://", errors[0])

    def test_ordinary_https_links_are_fine(self):
        for url in ("https://x", "https://example.com/a?b=1&c=2#d", "https://example.com/путь"):
            with self.subTest(url=url):
                self.assertEqual(self.link_errors(url), [])

    def test_unresolved_token_may_hide_the_host_but_not_a_dangerous_scheme(self):
        # переменная осталась неподставленной (--lenient-vars): проверяется то, что известно
        fine = ("{{link_play}}", "https://{{host}}/x", "{{scheme}}://example.com", "ht{{x}}", "https://x/{{path}}?a=1")
        for url in fine:
            with self.subTest(url=url):
                self.assertEqual(self.link_errors(url), [])
        for url in ("javascript:{{x}}", "data:text/html,{{x}}", "http://{{host}}", "ftp{{x}}", "https://{{host}} /x",
                    "https://a b/{{x}}"):
            with self.subTest(url=url):
                errors = self.link_errors(url)
                self.assertEqual(len(errors), 1, errors)
                self.assertIn("s1-01", errors[0])
                self.assertIn("https://", errors[0])

    def test_link_error_is_in_russian_and_names_the_link(self):
        errors = self.link_errors("https://x y")
        self.assertIn("ссылка", errors[0])
        self.assertIn("не должен содержать пробелов", errors[0])


class LinkRuleMatchesThePageTests(unittest.TestCase):
    """validate принимает ссылку, только если страница её выведет (safeUrl из template.html).

    Обратного не требуется: страница обрезает пробелы по краям и не различает регистр схемы, а validate
    строже и такую ссылку в контенте считает ошибкой, которую правит автор. Это безопасная сторона:
    лишнее отвергнутое лучше лишнего выведенного.
    """

    URLS = ["https://x", "https://example.com/a?b=1#c", "https://", "https://x y", "http://example.com",
            "ftp://example.com", "javascript:alert(1)", "data:text/html,x", "//example.com", "https:/x",
            "https://x\ty", "https://x\u00a0y", "https://x\ufeffy", "https://x\u2028y", "https://x\u3000y",
            "https://x\ny"]
    # страница такие ссылки выведет, validate — нет
    STRICTER = [" https://x", "https://x ", "HTTPS://x"]

    @staticmethod
    def verdicts(url):
        """(принимает ли validate, выводит ли страница) для ссылки первого шага."""
        data = substituted()
        find_step(data, "s1-01")["links"][0]["url"] = url
        accepted_by_python = not any("s1-01" in e and "ссылка" in e for e in build.validate(data))
        shown_by_page = call("safeUrl", url) != ""
        return accepted_by_python, shown_by_page

    @NEED_NODE
    def test_python_and_page_agree_on_links_the_page_would_drop(self):
        for url in self.URLS:
            with self.subTest(url=url):
                accepted_by_python, shown_by_page = self.verdicts(url)
                self.assertEqual(accepted_by_python, shown_by_page)

    @NEED_NODE
    def test_python_is_stricter_about_edge_spaces_and_scheme_case(self):
        for url in self.STRICTER:
            with self.subTest(url=url):
                accepted_by_python, _ = self.verdicts(url)
                self.assertFalse(accepted_by_python)

    @NEED_NODE
    def test_python_never_accepts_a_link_the_page_would_drop(self):
        for url in self.URLS + self.STRICTER:
            with self.subTest(url=url):
                accepted_by_python, shown_by_page = self.verdicts(url)
                if accepted_by_python:
                    self.assertTrue(shown_by_page)


MINUS = "−"  # знак «−» в разности полей отчёта


class RenderMdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.md = build.render_md(substituted())

    def test_runtime_tokens_become_visible_placeholders(self):
        self.assertIn("[НОМЕР ЗАЯВКИ] Я на месте. Мой телефон [ТВОЙ ТЕЛЕФОН С 8]", self.md)
        self.assertIn("[НОМЕР ЗАЯВКИ] Принял", self.md)
        self.assertNotIn("{{", self.md)

    def test_stages_and_steps_follow_content_order(self):
        positions = [
            self.md.index("## Старт: приложение и чат"),
            self.md.index("Скачай Приложение службы"),
            self.md.index("Напиши диспетчеру"),
            self.md.index("## Инструмент и первая заявка"),
            self.md.index("Купи набор отвёрток"),
            self.md.index("Запомни про пыль"),
            self.md.index("Сообщи, что приехал"),
            self.md.index("## Памятка на заявку"),
        ]
        self.assertEqual(positions, sorted(positions))

    def test_optional_steps_are_under_additional_heading(self):
        heading = self.md.index("### Дополнительно")
        self.assertLess(self.md.index("Напиши диспетчеру"), heading)
        self.assertLess(heading, self.md.index("Заведи отдельную почту"))
        self.assertLess(self.md.index("Заведи отдельную почту"), self.md.index("## Инструмент и первая заявка"))

    def test_additional_heading_only_where_optional_steps_exist(self):
        self.assertEqual(self.md.count("Дополнительно"), 1)

    def test_optional_step_in_the_middle_moves_under_additional(self):
        data = substituted()
        stage = data["stages"][0]
        stage["steps"][0], stage["steps"][2] = stage["steps"][2], stage["steps"][0]
        md = build.render_md(data)
        heading = md.index("### Дополнительно")
        self.assertLess(md.index("Напиши диспетчеру"), md.index("Скачай Приложение службы"))
        self.assertLess(md.index("Скачай Приложение службы"), heading)
        self.assertLess(heading, md.index("Заведи отдельную почту"))

    def test_step_shows_title_lead_points_links_proof(self):
        self.assertIn("### Скачай Приложение службы", self.md)
        self.assertIn("Установи приложение на телефон из магазина.", self.md)
        self.assertIn("1. Открой магазин приложений на телефоне", self.md)
        self.assertIn("2. Найди **Приложение службы** и нажми «Установить»", self.md)
        self.assertIn("- [Google Play](https://example.com/google-play)", self.md)
        self.assertIn("- [App Store](https://example.com/app-store)", self.md)
        self.assertIn("Готово, когда на телефоне есть Приложение службы", self.md)

    def test_points_of_know_step_are_a_bullet_list(self):
        self.assertIn("- Пыль забивает радиатор", self.md)

    def test_send_message_has_label_and_text_in_code_fence(self):
        self.assertIn("Первое сообщение", self.md)
        self.assertIn(
            "```\n@dispatcher Здравствуйте! Я новый мастер, прошу завести мне учётную запись.\n```",
            self.md,
        )

    def test_tip_is_shown(self):
        self.assertIn("Если техдир ещё не добавил тебя в чат, подожди его сообщения.", self.md)

    def test_done_marks_match_step_kind(self):
        self.assertIn("- [ ] Сделано", self.md)
        self.assertIn("- [ ] Куплено", self.md)
        self.assertIn("- [ ] Прочитал", self.md)

    def test_cheatsheet_chain_shows_only_present_parts(self):
        self.assertIn("- В приложении: «Принять»", self.md)
        self.assertIn("- В чат: «[НОМЕР ЗАЯВКИ] В пути»", self.md)
        self.assertIn("- В приложении: «Завершить заявку»", self.md)
        self.assertNotIn("«»", self.md)
        self.assertEqual(self.md.count("- В чат:"), 2)

    def test_report_template_uses_field_labels_as_placeholders(self):
        net = "[Всего (с чаевыми) " + MINUS + " Запчасти]"
        self.assertIn("Отчёт: сделал на месте", self.md)
        self.assertIn("[НОМЕР ЗАЯВКИ]\nСтатус - Готов", self.md)
        self.assertIn("Всего  - [Всего (с чаевыми)]", self.md)
        self.assertIn("Запчасти - [Запчасти]", self.md)
        self.assertIn("Чистыми " + net, self.md)
        self.assertIn("[Краткое описание работ]\nПрошу Проверить", self.md)
        self.assertIn("В поле «Сумма» в приложении впиши: " + net, self.md)

    def test_ends_with_single_newline(self):
        self.assertTrue(self.md.endswith("\n"))
        self.assertFalse(self.md.endswith("\n\n"))


class RenderHtmlTests(unittest.TestCase):
    TEMPLATE_TEXT = TEMPLATE.read_text(encoding="utf-8")
    BLOCK_RE = re.compile(r'<script type="application/json" id="guide-data">(.*?)</script>', re.DOTALL)

    def payloads(self, html):
        return self.BLOCK_RE.findall(html)

    def test_marker_replaced_with_one_data_block_that_round_trips(self):
        data = substituted()
        html = build.render_html(data, self.TEMPLATE_TEXT)
        self.assertNotIn(build.DATA_MARKER, html)
        self.assertEqual(html.count('id="guide-data"'), 1)
        blocks = self.payloads(html)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(json.loads(blocks[0]), data)
        self.assertIn("Заглушка вместо страницы", html)

    def test_non_ascii_text_stays_readable(self):
        html = build.render_html(substituted(), self.TEMPLATE_TEXT)
        self.assertIn("Скачай Приложение службы", html)

    def test_closing_sequences_are_escaped(self):
        data = substituted()
        data["stages"][0]["title"] = "</script><b>x</b>"
        html = build.render_html(data, self.TEMPLATE_TEXT)
        self.assertNotIn("</script><b>", html)
        self.assertEqual(html.count("</script>"), 1)
        block = self.payloads(html)[0]
        self.assertIn("<\\/script>", block)
        self.assertEqual(json.loads(block), data)

    def test_comment_opener_inside_data_is_escaped(self):
        data = substituted()
        data["stages"][0]["title"] = "<!-- не комментарий"
        html = build.render_html(data, self.TEMPLATE_TEXT)
        block = self.payloads(html)[0]
        self.assertNotIn("<!--", block)
        self.assertEqual(json.loads(block), data)

    def test_backslashes_and_dollars_survive(self):
        data = substituted()
        data["stages"][0]["title"] = "a\\b $1 \\1 \\g<0>"
        html = build.render_html(data, self.TEMPLATE_TEXT)
        self.assertEqual(json.loads(self.payloads(html)[0]), data)

    def test_template_without_marker_is_a_clear_error(self):
        with self.assertRaises(ValueError) as ctx:
            build.render_html(substituted(), "<html><body>без маркера</body></html>")
        self.assertIn("GUIDE_DATA", str(ctx.exception))

    def test_template_with_two_markers_is_an_error(self):
        with self.assertRaises(ValueError) as ctx:
            build.render_html(substituted(), self.TEMPLATE_TEXT + build.DATA_MARKER)
        self.assertIn("GUIDE_DATA", str(ctx.exception))


class RenderTechdirTests(unittest.TestCase):
    TEMPLATE_TEXT = TECHDIR_TEMPLATE.read_text(encoding="utf-8")

    def test_profile_variables_are_substituted(self):
        text = build.render_techdir(self.TEMPLATE_TEXT, fixture_profile())
        self.assertIn("# Записка для роли «техдир»", text)
        self.assertIn("- Приложение: Приложение службы.", text)
        self.assertIn("- Диспетчер в чате: @dispatcher.", text)
        self.assertNotIn("{{", text)

    def test_unknown_variable_raises_value_error_with_its_name(self):
        profile = fixture_profile()
        del profile["vars"]["techdir_name"]
        with self.assertRaises(ValueError) as ctx:
            build.render_techdir(self.TEMPLATE_TEXT, profile)
        self.assertIn("techdir_name", str(ctx.exception))

    def test_non_strict_leaves_unknown_token(self):
        profile = fixture_profile()
        del profile["vars"]["techdir_name"]
        text = build.render_techdir(self.TEMPLATE_TEXT, profile, strict=False)
        self.assertIn("«{{techdir_name}}»", text)
        self.assertIn("@dispatcher", text)


class CoverageTests(unittest.TestCase):
    MANIFEST_TEXT = MANIFEST.read_text(encoding="utf-8")

    def test_parse_manifest_reads_ids_and_statuses(self):
        statuses = build.parse_manifest(self.MANIFEST_TEXT)
        self.assertEqual(statuses["R05"], "in-ticket")
        self.assertEqual(statuses["R09"], "placeholder")
        self.assertEqual(statuses["R15"], "done")
        self.assertEqual(statuses["R21"], "deferred")
        self.assertEqual(statuses["R22"], "dropped")
        self.assertEqual(statuses["R75i"], "in-ticket")
        self.assertNotIn("ID", statuses)
        self.assertEqual(len(statuses), 15)

    def test_uncovered_requirement_is_missing_and_nothing_is_unknown(self):
        result = build.coverage(substituted(), self.MANIFEST_TEXT)
        self.assertEqual(result, {"missing": ["R20"], "unknown": []})

    def test_nonexistent_identifier_is_unknown(self):
        data = substituted()
        find_step(data, "s1-02")["reqs"].append("R99")
        result = build.coverage(data, self.MANIFEST_TEXT)
        self.assertEqual(result["unknown"], ["R99"])
        self.assertEqual(result["missing"], ["R20"])

    def test_exceptions_are_never_missing(self):
        manifest = self.MANIFEST_TEXT + "".join(
            f"| {req} | пример | in-ticket | — | — |\n" for req in ("R02", "R03", "G34", "G36")
        )
        result = build.coverage(substituted(), manifest)
        self.assertEqual(result["missing"], ["R20"])

    def test_requirements_about_the_product_as_a_whole_are_never_missing(self):
        # G38 (профиль открыт), G42, G43 — не про шаги гайда, а про продукт целиком
        manifest = self.MANIFEST_TEXT + "".join(
            f"| {req} | пример | in-ticket | — | — |\n" for req in ("G38", "G42", "G43")
        )
        result = build.coverage(substituted(), manifest)
        self.assertEqual(result["missing"], ["R20"])

    def test_showcase_requirements_are_never_missing(self):
        # G44–G46 — витрина на GitHub (README, снимки, документы проекта), а не шаги гайда
        manifest = self.MANIFEST_TEXT + "".join(
            f"| {req} | пример | in-ticket | — | — |\n" for req in ("G44", "G45", "G46")
        )
        result = build.coverage(substituted(), manifest)
        self.assertEqual(result, {"missing": ["R20"], "unknown": []})

    def test_the_exemption_does_not_hide_the_neighbouring_requirements(self):
        manifest = self.MANIFEST_TEXT + "".join(
            f"| {req} | пример | in-ticket | — | — |\n" for req in ("G43", "G44", "G46", "G47")
        )
        self.assertEqual(build.coverage(substituted(), manifest)["missing"], ["G47", "R20"])

    def test_requirement_id_with_a_trailing_newline_is_not_taken_from_the_manifest(self):
        statuses = build.parse_manifest("| R41٠ | пример | in-ticket | — | — |\n| R99 | пример | done | — | — |\n")
        self.assertEqual(statuses, {"R99": "done"})

    def test_deferred_and_dropped_are_not_required(self):
        missing = build.coverage(substituted(), self.MANIFEST_TEXT)["missing"]
        self.assertNotIn("R21", missing)
        self.assertNotIn("R22", missing)

    def test_done_and_placeholder_statuses_are_required(self):
        data = substituted()
        find_step(data, "s2-01")["reqs"] = ["R16"]  # R15 (done) больше не покрыт
        find_step(data, "s1-03")["reqs"] = ["R08"]  # R09 (placeholder) больше не покрыт
        missing = build.coverage(data, self.MANIFEST_TEXT)["missing"]
        self.assertEqual(missing, ["R09", "R15", "R20"])

    def test_spec_additions_are_not_unknown(self):
        # A01 есть в шаге s2-02, но не в манифесте: это требования из спецификации
        self.assertIn("A01", find_step(substituted(), "s2-02")["reqs"])
        self.assertEqual(build.coverage(substituted(), self.MANIFEST_TEXT)["unknown"], [])

    def test_lists_are_sorted_by_number_not_by_text(self):
        manifest = self.MANIFEST_TEXT + "| R100 | пример | in-ticket | — | — |\n| R9 | пример | in-ticket | — | — |\n"
        self.assertEqual(build.coverage(substituted(), manifest)["missing"], ["R9", "R20", "R100"])

    def test_status_is_found_even_if_quote_cell_contains_a_pipe(self):
        manifest = self.MANIFEST_TEXT + "| R41 | цитата | с палкой | in-ticket | — | — |\n"
        self.assertIn("R41", build.coverage(substituted(), manifest)["missing"])

    def test_rows_that_are_not_requirements_are_ignored(self):
        manifest = self.MANIFEST_TEXT + "| Итого | 15 | всего |\n| 2026 | год |\n"
        self.assertEqual(build.coverage(substituted(), manifest)["missing"], ["R20"])


class CliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.out = self.tmp / "out"

    def args(self, *extra, content=CONTENT, profile=PROFILE, template=TEMPLATE, techdir=TECHDIR_TEMPLATE):
        result = ["--content", str(content), "--profile", str(profile), "--template", str(template)]
        if techdir is not None:
            result += ["--techdir-template", str(techdir)]
        return result + ["--out", str(self.out)] + list(extra)

    def copy_content(self):
        target = self.tmp / "content"
        shutil.copytree(CONTENT, target)
        return target

    def write_profile(self, mutate):
        profile = json.loads(PROFILE.read_text(encoding="utf-8"))
        mutate(profile["vars"])
        path = self.tmp / "profile.json"
        write_json(path, profile)
        return path

    # --- сборка ---

    def test_build_writes_the_three_files(self):
        code, _out, err = run_cli(self.args())
        self.assertEqual(code, 0, err)
        for name in ("index.html", "guide.md", "for-techdir.md"):
            self.assertTrue((self.out / name).is_file(), name)
        html = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('id="guide-data"'), 1)
        self.assertIn("Скачай Приложение службы", html)
        self.assertIn("[НОМЕР ЗАЯВКИ]", (self.out / "guide.md").read_text(encoding="utf-8"))
        self.assertIn("@dispatcher", (self.out / "for-techdir.md").read_text(encoding="utf-8"))

    def test_build_without_techdir_flag_uses_the_default_note(self):
        code, _out, err = run_cli(self.args(techdir=None))
        self.assertEqual(code, 0, err)
        note = (self.out / "for-techdir.md").read_text(encoding="utf-8")
        self.assertTrue(note.strip())
        self.assertNotIn("{{", note)

    def test_default_techdir_template_prefers_docs_then_fixture(self):
        root = self.tmp / "repo"
        fallback = root / "guide" / "tests" / "fixtures" / "for-techdir.md"
        self.assertEqual(build.default_techdir_template(root), fallback)
        docs = root / "guide" / "docs" / "for-techdir.md"
        docs.parent.mkdir(parents=True)
        docs.write_text("записка", encoding="utf-8")
        self.assertEqual(build.default_techdir_template(root), docs)

    def test_output_folder_is_created_and_files_overwritten(self):
        self.out.mkdir()
        (self.out / "guide.md").write_text("старое", encoding="utf-8")
        code, _out, err = run_cli(self.args())
        self.assertEqual(code, 0, err)
        self.assertNotEqual((self.out / "guide.md").read_text(encoding="utf-8"), "старое")

    # --- --check ---

    def test_check_passes_on_fixture_and_writes_nothing(self):
        code, _out, err = run_cli(self.args("--check"))
        self.assertEqual(code, 0, err)
        self.assertFalse(self.out.exists())

    def test_check_needs_no_template(self):
        code, _out, err = run_cli(self.args("--check", template=self.tmp / "missing.html"))
        self.assertEqual(code, 0, err)

    def test_check_on_empty_folder_says_no_content_files(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        code, out, err = run_cli(self.args("--check", content=empty))
        self.assertEqual(code, 1)
        self.assertIn("нет файлов контента", err)
        self.assertNotIn("Traceback", err + out)

    def test_check_lists_validation_errors_with_step_ids(self):
        content = self.copy_content()
        path = content / "10-start.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["steps"][2]["id"] = "s1-02"
        write_json(path, data)
        code, _out, err = run_cli(self.args("--check", content=content))
        self.assertEqual(code, 1)
        self.assertIn("s1-02", err)
        self.assertIn("повтор", err)
        self.assertFalse(self.out.exists())

    def test_build_with_validation_errors_writes_nothing(self):
        content = self.copy_content()
        path = content / "20-orders.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["steps"][0]["proof"]
        write_json(path, data)
        code, _out, err = run_cli(self.args(content=content))
        self.assertEqual(code, 1)
        self.assertIn("s2-01", err)
        self.assertFalse(self.out.exists())

    # --- переменные профиля ---

    def test_unknown_variable_fails_by_default(self):
        profile = self.write_profile(lambda v: v.pop("link_play"))
        code, _out, err = run_cli(self.args("--check", profile=profile))
        self.assertEqual(code, 1)
        self.assertIn("link_play", err)
        self.assertNotIn("link_apple", err)

    def test_lenient_vars_prints_unknown_names_and_succeeds(self):
        profile = self.write_profile(lambda v: (v.pop("link_play"), v.pop("techdir_name")))
        code, out, err = run_cli(self.args("--check", "--lenient-vars", profile=profile))
        self.assertEqual(code, 0, err)
        self.assertIn("link_play", out)
        self.assertIn("techdir_name", out)
        self.assertNotIn("link_apple", out)

    def test_lenient_vars_also_lets_the_build_run(self):
        profile = self.write_profile(lambda v: v.pop("link_play"))
        code, _out, err = run_cli(self.args("--lenient-vars", profile=profile))
        self.assertEqual(code, 0, err)
        self.assertIn("{{link_play}}", (self.out / "index.html").read_text(encoding="utf-8"))

    def test_list_vars_prints_one_name_per_line_and_exits(self):
        code, out, err = run_cli(["--list-vars", "--content", str(CONTENT)])
        self.assertEqual(code, 0, err)
        self.assertEqual(
            out.splitlines(),
            ["app_name", "dispatcher_handle", "link_apple", "link_play", "techdir_name"],
        )

    def test_list_vars_does_not_build(self):
        code, _out, err = run_cli(self.args("--list-vars"))
        self.assertEqual(code, 0, err)
        self.assertFalse(self.out.exists())

    def test_list_vars_on_empty_folder_is_an_error(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        code, _out, err = run_cli(["--list-vars", "--content", str(empty)])
        self.assertEqual(code, 1)
        self.assertIn("нет файлов контента", err)

    def test_extra_profile_variable_is_only_a_warning(self):
        code, out, err = run_cli(self.args("--check"))
        self.assertEqual(code, 0, err)
        self.assertIn("spare_var", out)

    def test_unknown_variable_in_techdir_note_is_reported(self):
        note = self.tmp / "note.md"
        note.write_text("Привет, {{nobody_knows}}!", encoding="utf-8")
        code, _out, err = run_cli(self.args("--check", techdir=note))
        self.assertEqual(code, 1)
        self.assertIn("nobody_knows", err)

    # --- манифест ---

    def test_manifest_with_uncovered_requirement_fails_and_prints_both_lists(self):
        code, out, _err = run_cli(self.args("--check", "--manifest", str(MANIFEST)))
        self.assertEqual(code, 1)
        self.assertIn("R20", out)
        self.assertIn("Неизвестные", out)

    def test_manifest_prints_unknown_identifier(self):
        content = self.copy_content()
        path = content / "10-start.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["steps"][1]["reqs"].append("R99")
        write_json(path, data)
        code, out, _err = run_cli(self.args("--check", "--manifest", str(MANIFEST), content=content))
        self.assertEqual(code, 1)
        self.assertIn("R99", out)
        self.assertIn("R20", out)

    def test_manifest_fully_covered_passes(self):
        manifest = self.tmp / "manifest.md"
        text = MANIFEST.read_text(encoding="utf-8")
        manifest.write_text("\n".join(l for l in text.splitlines() if not l.startswith("| R20 ")), encoding="utf-8")
        code, out, err = run_cli(self.args("--check", "--manifest", str(manifest)))
        self.assertEqual(code, 0, err + out)

    def test_manifest_with_whole_product_requirements_passes(self):
        lines = [l for l in MANIFEST.read_text(encoding="utf-8").splitlines() if not l.startswith("| R20 ")]
        lines += [f"| {req} | пример | in-ticket | — | — |" for req in ("G38", "G42", "G43")]
        manifest = self.tmp / "manifest.md"
        manifest.write_text("\n".join(lines), encoding="utf-8")
        code, out, err = run_cli(self.args("--check", "--manifest", str(manifest)))
        self.assertEqual(code, 0, err + out)
        self.assertIn("Непокрытые требования манифеста: нет", out)

    def test_missing_manifest_file_is_a_clear_error(self):
        code, _out, err = run_cli(self.args("--check", "--manifest", str(self.tmp / "nope.md")))
        self.assertEqual(code, 1)
        self.assertIn("nope.md", err)
        self.assertNotIn("Traceback", err)

    # --- шаблон и файлы ---

    def test_template_without_marker_is_a_clear_error_and_writes_nothing(self):
        template = self.tmp / "bad.html"
        template.write_text("<html><body>без маркера</body></html>", encoding="utf-8")
        code, _out, err = run_cli(self.args(template=template))
        self.assertEqual(code, 1)
        self.assertIn("GUIDE_DATA", err)
        self.assertNotIn("Traceback", err)
        self.assertFalse((self.out / "index.html").exists())
        self.assertFalse((self.out / "guide.md").exists())

    def test_missing_template_file_is_a_clear_error(self):
        code, _out, err = run_cli(self.args(template=self.tmp / "nope.html"))
        self.assertEqual(code, 1)
        self.assertIn("nope.html", err)
        self.assertNotIn("Traceback", err)

    def test_missing_profile_file_is_a_clear_error(self):
        code, _out, err = run_cli(self.args("--check", profile=self.tmp / "nope.json"))
        self.assertEqual(code, 1)
        self.assertIn("nope.json", err)
        self.assertNotIn("Traceback", err)

    def test_help_lists_every_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            build.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        for flag in ("--content", "--profile", "--out", "--check", "--manifest", "--lenient-vars",
                     "--list-vars", "--template", "--techdir-template"):
            self.assertIn(flag, out.getvalue())


class ScriptTests(unittest.TestCase):
    """build.py запускается и как скрипт."""

    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, str(BUILD_SCRIPT), *args],
            capture_output=True, text=True, encoding="utf-8", timeout=60, check=False,
        )

    def test_runs_as_a_script_and_builds(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_script(
                "--content", str(CONTENT), "--profile", str(PROFILE), "--template", str(TEMPLATE),
                "--techdir-template", str(TECHDIR_TEMPLATE), "--out", tmp,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ("index.html", "guide.md", "for-techdir.md"):
                self.assertTrue((Path(tmp) / name).is_file(), name)

    def test_check_on_empty_folder_prints_a_message_not_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_script("--check", "--content", tmp)
        self.assertEqual(result.returncode, 1)
        self.assertIn("нет файлов контента", result.stderr)
        self.assertNotIn("Traceback", result.stderr + result.stdout)

    def test_check_with_default_arguments_passes_without_a_traceback(self):
        # гайд и демо-профиль на месте: команда проверки из AGENTS.md обязана проходить
        result = self.run_script("--check")
        self.assertNotIn("Traceback", result.stderr + result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)


# --- КОНЕЦ ТЕСТОВ ---
