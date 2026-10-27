"""Тесты tools/check_public.py — на временных каталогах.

Строки вроде «диск D с обратной косой чертой» собираются из частей: иначе сам
этот файл попал бы под проверку на утечку.
"""

import contextlib
import io
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools import check_public

BACKSLASH = chr(92)
DRIVE_D = "D:" + BACKSLASH
USERS_DIR = "C:" + BACKSLASH + "Users"


def run(root, *args):
    """Запуск main в этом же процессе; возвращает (код, stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = check_public.main(["--root", str(root), *args])
    return code, out.getvalue()


class CheckPublicTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def make(self, rel, content="", encoding="utf-8"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding=encoding)
        return path


class WalkModeTests(CheckPublicTestCase):
    """В каталоге без .git проверяется всё дерево."""

    def test_clean_tree_returns_zero(self):
        self.make("README.md", "# Гайд\nОбычный текст.\n")
        self.make("guide/content/10.json", '{"a": 1}')
        code, out = run(self.root)
        self.assertEqual(code, 0, out)
        self.assertNotIn(" — ", out)

    def test_forbidden_extensions_are_reported_case_insensitively(self):
        for name in ("a.docx", "b.PDF", "c.jpg", "d.jpeg", "sub/e.JPG", "ok.txt", "docx", "notes.docx.txt"):
            self.make(name, "x")
        code, out = run(self.root)
        self.assertEqual(code, 1)
        for name in ("a.docx", "b.PDF", "c.jpg", "d.jpeg", "sub/e.JPG"):
            self.assertRegex(out, re.escape(name) + r" — ", name)
        for name in ("ok.txt", "notes.docx.txt"):
            self.assertNotIn(name, out)

    def test_windows_paths_are_reported_with_line_number(self):
        self.make("notes.md", "первая строка\nсм. " + DRIVE_D + "Projects\nещё " + USERS_DIR + "\\x\nа " + DRIVE_D.lower() + "tmp\nчисто\n")
        code, out = run(self.root)
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertTrue(any(re.match(r"notes\.md:2 — ", l) for l in lines), out)
        self.assertTrue(any(re.match(r"notes\.md:3 — ", l) for l in lines), out)
        self.assertTrue(any(re.match(r"notes\.md:4 — ", l) for l in lines), out)
        self.assertFalse(any(re.match(r"notes\.md:(1|5) — ", l) for l in lines), out)

    def test_unrelated_colon_text_is_not_a_path(self):
        self.make("notes.md", "Время 10:30, раздел D: описание, ссылка https://example.com/d:x\n")
        code, out = run(self.root)
        self.assertEqual(code, 0, out)

    def test_skips_git_dist_and_private_folders(self):
        self.make(".git/config.jpg", "x")
        self.make("dist/leak.docx", "x")
        self.make("dist/page.html", "см. " + DRIVE_D + "x")
        self.make("private/notes.pdf", "x")
        self.make("README.md", "чисто")
        code, out = run(self.root)
        self.assertEqual(code, 0, out)

    def test_binary_files_are_not_scanned_for_text(self):
        self.make("image.png", b"\x89PNG\x00\x00zorkmid " + DRIVE_D.encode("ascii"))
        markers = self.make("markers.txt", "zorkmid\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 0, out)

    def test_windows_1251_text_is_scanned_too(self):
        self.make("old.txt", "Секретное слово здесь\n".encode("cp1251"))
        markers = self.make("markers.txt", "секретное\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1)
        self.assertRegex(out, r"old\.txt:1 — ")

    def test_utf16_text_is_scanned_too(self):
        self.make("reg.txt", "строка\nzorkmid\n".encode("utf-16"))
        markers = self.make("markers.txt", "zorkmid\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1)
        self.assertRegex(out, r"reg\.txt:2 — ")


class MarkersTests(CheckPublicTestCase):
    def test_markers_are_found_ignoring_case_with_line_numbers(self):
        self.make("notes.md", "чисто\nОплата в ZORKMID за час\nа тут Blue Falcon.\nконец\n")
        markers = self.make("markers.txt", "zorkmid\n\n  blue falcon  \n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1)
        lines = out.splitlines()
        self.assertTrue(any(re.match(r"notes\.md:2 — ", l) for l in lines), out)
        self.assertTrue(any(re.match(r"notes\.md:3 — ", l) for l in lines), out)
        self.assertFalse(any(re.match(r"notes\.md:(1|4) — ", l) for l in lines), out)

    def test_output_never_repeats_the_marker_words(self):
        self.make("notes.md", "Оплата в ZORKMID\n")
        markers = self.make("markers.txt", "zorkmid\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1)
        self.assertNotIn("zorkmid", out.lower())

    def test_marker_in_file_name_is_reported(self):
        self.make("zorkmid-notes.md", "чисто")
        markers = self.make("markers.txt", "zorkmid\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1)
        self.assertRegex(out, r"-notes\.md — ")

    def test_default_markers_file_is_private_markers_txt(self):
        self.make("private/markers.txt", "zorkmid\n")
        self.make("notes.md", "про zorkmid\n")
        code, out = run(self.root)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"notes\.md:1 — ")

    def test_without_markers_file_only_other_checks_run(self):
        self.make("notes.md", "про zorkmid\n")
        code, out = run(self.root)
        self.assertEqual(code, 0, out)

    def test_markers_file_inside_the_tree_is_not_reported_against_itself(self):
        markers = self.make("tools-markers.txt", "zorkmid\n")
        self.make("notes.md", "чисто\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 0, out)

    def test_missing_explicit_markers_file_is_an_error(self):
        code, out = run(self.root, "--markers", str(self.root / "nope.txt"))
        self.assertEqual(code, 1)
        self.assertIn("nope.txt", out)

    def test_load_markers_strips_blanks(self):
        path = self.make("m.txt", "\ufeff one \n\n\ttwo\n   \n")
        self.assertEqual(check_public.load_markers(path), ["one", "two"])


SLASH = chr(47)


def reported_lines(out, name):
    """Номера строк файла `name`, по которым есть находки в выводе."""
    return sorted(int(n) for n in re.findall(r"^" + re.escape(name) + r":(\d+) — ", out, flags=re.M))


class MarkerEvasionTests(CheckPublicTestCase):
    """Многословный маркер находится, как бы ни были разорваны слова пробелами и невидимыми символами."""

    def found(self, text, marker="blue falcon"):
        self.make("notes.md", "первая\n" + text + "\nпоследняя\n")
        markers = self.make("markers.txt", marker + "\n")
        code, out = run(self.root, "--markers", str(markers))
        return code, out

    def test_different_gaps_between_words_are_still_found(self):
        gaps = {
            "nbsp": "Blue\u00a0Falcon",
            "double space": "Blue  Falcon",
            "tab": "Blue\tFalcon",
            "narrow nbsp": "Blue\u202fFalcon",
            "ideographic space": "Blue\u3000Falcon",
            "zero-width space instead of the gap": "Blue\u200bFalcon",
            "zero-width joiner around the gap": "Blue \u200d Falcon",
            "word joiner": "Blue\u2060Falcon",
            "byte order mark in the middle": "Blue\ufeffFalcon",
            "zero-width non-joiner": "Blue\u200cFalcon",
        }
        for label, text in gaps.items():
            with self.subTest(label):
                code, out = self.found("Оплата: " + text + " за час")
                self.assertEqual(code, 1, out)
                self.assertEqual(reported_lines(out, "notes.md"), [2], out)

    def test_invisible_characters_inside_a_word_are_ignored(self):
        for text in ("Bl\u200bue Falcon", "Blue Fal\u2060con", "zork\u200bmid"):
            with self.subTest(text):
                marker = "zorkmid" if text.startswith("zork") else "blue falcon"
                code, out = self.found(text, marker)
                self.assertEqual(code, 1, out)
                self.assertEqual(reported_lines(out, "notes.md"), [2], out)

    # --- невидимые символы из PDF и Word: мягкий перенос, метки направления, соединители ---

    INVISIBLE_CODES = (
        [0x00AD, 0x034F, 0x180E, 0x200E, 0x200F, 0xFE0F]
        + list(range(0x202A, 0x202F))  # U+202A–U+202E
        + list(range(0x2060, 0x2065))  # U+2060–U+2064
    )

    def test_soft_hyphen_inside_a_word_or_between_words_is_ignored(self):
        for text in ("Blu\u00ade Falcon", "Blue\u00adFalcon", "Blue \u00ad Falcon", "Bl\u00adue Fal\u00adcon"):
            with self.subTest(text=text):
                code, out = self.found("Оплата: " + text + " за час")
                self.assertEqual(code, 1, out)
                self.assertEqual(reported_lines(out, "notes.md"), [2], out)

    def test_every_listed_invisible_character_is_ignored_inside_and_between_words(self):
        for code_point in self.INVISIBLE_CODES:
            char = chr(code_point)
            for text in ("Blu" + char + "e Falcon", "Blue" + char + "Falcon"):
                with self.subTest(code_point=f"U+{code_point:04X}", text=text):
                    found_code, out = self.found("Оплата: " + text + " за час")
                    self.assertEqual(found_code, 1, out)
                    self.assertEqual(reported_lines(out, "notes.md"), [2], out)

    def test_invisible_characters_inside_a_single_word_marker_are_ignored_too(self):
        for char in ("\u00ad", "\u200e", "\u202e", "\u2062", "\u034f", "\ufe0f", "\u180e"):
            with self.subTest(code_point=f"U+{ord(char):04X}"):
                code, out = self.found("про zork" + char + "mid", "zorkmid")
                self.assertEqual(code, 1, out)
                self.assertEqual(reported_lines(out, "notes.md"), [2], out)

    # --- дефис и «&nbsp;» вместо пробела между словами многословного маркера ---

    def test_hyphen_or_nbsp_entity_between_the_words_of_a_marker_is_found(self):
        gaps = {
            "hyphen": "Blue-Falcon",
            "hyphen with spaces": "Blue - Falcon",
            "non-breaking hyphen": "Blue\u2011Falcon",
            "hyphen character": "Blue\u2010Falcon",
            "nbsp entity": "Blue&nbsp;Falcon",
            "nbsp entity in capitals": "BLUE&NBSP;FALCON",
            "nbsp entity as number": "Blue&#160;Falcon",
            "nbsp entity as hex number": "Blue&#xA0;Falcon",
            "nbsp entity next to a space": "Blue &nbsp;Falcon",
            "hyphen and soft hyphen": "Blue-\u00adFalcon",
        }
        for label, text in gaps.items():
            with self.subTest(label):
                code, out = self.found("Оплата: " + text + " за час")
                self.assertEqual(code, 1, out)
                self.assertEqual(reported_lines(out, "notes.md"), [2], out)

    def test_hyphen_and_entity_do_not_replace_the_word_boundaries_of_a_one_word_marker(self):
        for text, marker in (("zork-mid", "zorkmid"), ("zork&nbsp;mid", "zorkmid"), ("Blue Falcon", "blue-falcon")):
            with self.subTest(text=text, marker=marker):
                code, out = self.found("Оплата: " + text, marker)
                self.assertEqual(code, 0, out)

    def test_a_hyphenated_one_word_marker_is_still_found_as_written(self):
        code, out = self.found("Оплата: Blue-Falcon", "blue-falcon")
        self.assertEqual(code, 1, out)
        self.assertEqual(reported_lines(out, "notes.md"), [2], out)

    def test_hyphen_between_other_words_or_inside_a_word_is_not_a_hit(self):
        for text in ("Blue-sky Falcon", "Bl-ue Falcon", "Blue Fal-con", "Blue&amp;Falcon", "Blue--x--Falcon"):
            with self.subTest(text=text):
                code, out = self.found("Оплата: " + text)
                self.assertEqual(code, 0, out)

    def test_output_does_not_repeat_the_words_found_through_a_hyphen_or_entity(self):
        for text in ("Blue-Falcon", "Blue&nbsp;Falcon"):
            with self.subTest(text=text):
                code, out = self.found(text)
                self.assertEqual(code, 1)
                self.assertNotIn("falcon", out.lower())

    def test_marker_in_a_file_name_is_found_through_a_hyphen_and_hidden_in_the_output(self):
        self.make("blue-falcon-notes.md", "чисто\n")
        markers = self.make("markers.txt", "blue falcon\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1, out)
        self.assertNotIn("falcon", out.lower())
        self.assertIn("***-notes.md — ", out)

    def test_marker_split_by_a_line_break_is_found_on_its_first_line(self):
        self.make("notes.md", "первая\nпро Blue\nFalcon тут\nпоследняя\n")
        markers = self.make("markers.txt", "blue falcon\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1, out)
        self.assertEqual(reported_lines(out, "notes.md"), [2], out)

    def test_marker_split_by_windows_line_break_and_blank_line_is_found(self):
        self.make("notes.md", b"a\r\nBlue\r\n\r\n  Falcon\r\nb\r\n")
        markers = self.make("markers.txt", "blue falcon\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1, out)
        self.assertEqual(reported_lines(out, "notes.md"), [2], out)

    def test_case_is_ignored_for_cyrillic_too(self):
        self.make("notes.md", "Пишем ЗОРКМИД Иванович\n")
        markers = self.make("markers.txt", "зоркмид иванович\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1, out)
        self.assertEqual(reported_lines(out, "notes.md"), [1], out)

    def test_gaps_inside_the_marker_file_are_normalised_too(self):
        self.make("notes.md", "про blue falcon\n")
        markers = self.make("markers.txt", "blue\u00a0\u00a0 \tfalcon\u200b\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1, out)
        self.assertEqual(reported_lines(out, "notes.md"), [1], out)

    def test_two_markers_on_the_same_line_are_reported_once_with_both_numbers(self):
        self.make("notes.md", "Blue\u00a0Falcon и zorkmid\n")
        markers = self.make("markers.txt", "blue falcon\nzorkmid\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1, out)
        self.assertEqual(reported_lines(out, "notes.md"), [1], out)
        self.assertIn("1, 2", out)

    def test_words_that_are_not_next_to_each_other_are_not_a_hit(self):
        self.make("notes.md", "Blue sky. A falcon flies.\n")
        markers = self.make("markers.txt", "blue falcon\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 0, out)

    def test_output_still_never_repeats_the_words(self):
        code, out = self.found("Blue\u00a0Falcon")
        self.assertEqual(code, 1)
        self.assertNotIn("falcon", out.lower())


class MarkersFileTests(CheckPublicTestCase):
    def test_load_markers_skips_comment_and_blank_lines(self):
        path = self.make("m.txt", "# комментарий\n\none\n   # другой комментарий\n  two  \n#\n")
        self.assertEqual(check_public.load_markers(path), ["one", "two"])

    def test_a_commented_word_is_not_a_marker(self):
        self.make("notes.md", "про zorkmid\n")
        markers = self.make("markers.txt", "# zorkmid\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 0, out)

    def test_file_with_only_a_comment_warns_and_keeps_the_exit_code(self):
        self.make("notes.md", "чисто\n")
        markers = self.make("markers.txt", "# сюда по одному слову на строку\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 0, out)
        self.assertIn("слов нет, проверка по словам не выполняется", out)

    def test_empty_default_markers_file_warns_too(self):
        self.make("private/markers.txt", "\n  \n")
        self.make("notes.md", "чисто\n")
        code, out = run(self.root)
        self.assertEqual(code, 0, out)
        self.assertIn("слов нет, проверка по словам не выполняется", out)

    def test_the_warning_does_not_hide_other_findings(self):
        self.make("leak.docx", "x")
        markers = self.make("markers.txt", "# пусто\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1)
        self.assertIn("слов нет, проверка по словам не выполняется", out)
        self.assertRegex(out, r"leak\.docx — ")

    def test_no_warning_when_there_are_words_or_no_file(self):
        self.make("notes.md", "чисто\n")
        markers = self.make("markers.txt", "# комментарий\nzorkmid\n")
        _, with_words = run(self.root, "--markers", str(markers))
        _, without_file = run(self.root)
        self.assertNotIn("слов нет", with_words)
        self.assertNotIn("слов нет", without_file)


class DiskPathTests(CheckPublicTestCase):
    """Путь к диску ищется как путь, а не как кусок слова; слэши бывают разные."""

    def flagged_lines(self, text):
        self.make("notes.md", text)
        code, out = run(self.root)
        return code, reported_lines(out, "notes.md"), out

    def test_a_letter_before_the_drive_letter_means_it_is_not_a_path(self):
        clean = [
            "const re = /ID:" + BACKSLASH + "s/;",
            "'Android:" + BACKSLASH + "n'",
            "Android:" + BACKSLASH + "nНастройки",
            "Userid:" + BACKSLASH + "x",
            "5D:" + BACKSLASH + "x",
            "ID:" + SLASH + "x",
            "Android:" + SLASH + "x",
            "АйД:" + BACKSLASH + "x",
        ]
        for text in clean:
            with self.subTest(text):
                code, lines, out = self.flagged_lines(text + "\n")
                self.assertEqual((code, lines), (0, []), out)

    def test_a_regex_escape_before_the_drive_letter_is_not_a_path(self):
        # в JS-выражениях «\d:» — класс цифр и двоеточие, а не диск D; «\s» и «\d» после двоеточия — тоже классы
        clean = [
            "const re = /^" + BACKSLASH + "d:" + BACKSLASH + "s*/;",
            "x = /" + BACKSLASH + "d:" + BACKSLASH + "d+/",
            "(" + BACKSLASH + "d:" + BACKSLASH + "s)",
            "a" + BACKSLASH + "d:" + BACKSLASH + "x",
            "new RegExp(\"^" + BACKSLASH * 2 + "d:" + BACKSLASH * 2 + "s*\")",
            "x = /" + BACKSLASH + "c:" + BACKSLASH + "users/",
        ]
        for text in clean:
            with self.subTest(text=text):
                code, lines, out = self.flagged_lines(text + "\n")
                self.assertEqual((code, lines), (0, []), out)

    def test_extended_length_and_device_prefixes_are_still_paths(self):
        found = [
            BACKSLASH * 2 + "?" + BACKSLASH + "D:" + BACKSLASH + "Projects",
            "path = " + BACKSLASH * 2 + "?" + BACKSLASH + "d:" + BACKSLASH + "x",
            BACKSLASH * 2 + "." + BACKSLASH + "D:" + BACKSLASH + "x",
            BACKSLASH * 2 + "?" + BACKSLASH + "C:" + BACKSLASH + "Users",
            "(D:" + BACKSLASH + "x)",
            "(C:" + BACKSLASH + "Users)",
        ]
        for text in found:
            with self.subTest(text=text):
                code, lines, out = self.flagged_lines("чисто\n" + text + "\n")
                self.assertEqual((code, lines), (1, [2]), out)

    def test_a_single_backslash_after_other_characters_is_an_escape_not_a_prefix(self):
        for text in ("a?" + BACKSLASH + "d:" + BACKSLASH + "s", "/.?" + BACKSLASH + "d:" + BACKSLASH + "s/", BACKSLASH + "?" + BACKSLASH + "d:" + BACKSLASH + "s"):
            with self.subTest(text=text):
                code, lines, out = self.flagged_lines(text + "\n")
                self.assertEqual((code, lines), (0, []), out)

    def test_a_real_path_after_punctuation_is_still_found(self):
        paths = [
            "(" + DRIVE_D + "Projects)",
            "\"" + DRIVE_D + "Projects\"",
            "путь:" + DRIVE_D + "x",
            "d:" + BACKSLASH + "tmp",
            "- " + DRIVE_D,
        ]
        for text in paths:
            with self.subTest(text):
                code, lines, out = self.flagged_lines("чисто\n" + text + "\n")
                self.assertEqual((code, lines), (1, [2]), out)

    def test_escaped_backslashes_are_found(self):
        for text in (
            "\"C:" + BACKSLASH * 2 + "Users" + BACKSLASH * 2 + "name\"",
            "\"D:" + BACKSLASH * 2 + "Projects\"",
            "\"d:" + BACKSLASH * 4 + "x\"",
        ):
            with self.subTest(text):
                code, lines, out = self.flagged_lines("чисто\n" + text + "\n")
                self.assertEqual((code, lines), (1, [2]), out)

    def test_forward_slashes_are_found(self):
        for text in (
            "cd D:" + SLASH + "Projects" + SLASH + "x",
            "open c:" + SLASH + "users" + SLASH + "name",
            "см. C:" + SLASH + "Users",
            "file:" + SLASH * 3 + "C:" + SLASH + "Users" + SLASH + "x",
        ):
            with self.subTest(text):
                code, lines, out = self.flagged_lines("чисто\n" + text + "\n")
                self.assertEqual((code, lines), (1, [2]), out)

    def test_other_drives_and_folders_are_not_a_hit(self):
        for text in ("C:" + SLASH + "Windows", "E:" + BACKSLASH + "x", "Время 10:30 и 12:" + SLASH + "7", "раздел D: описание"):
            with self.subTest(text):
                code, lines, out = self.flagged_lines(text + "\n")
                self.assertEqual((code, lines), (0, []), out)

    def test_reason_names_the_drive(self):
        self.make("notes.md", DRIVE_D + "x\n" + "C:" + SLASH + "Users\n")
        _, out = run(self.root)
        self.assertRegex(out, r"notes\.md:1 — путь к диску D")
        self.assertRegex(out, r"notes\.md:2 — путь к папке пользователей C")


BINARY = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00\x02"


class BinaryAndEncodingTests(CheckPublicTestCase):
    def test_text_file_with_nul_bytes_is_skipped_with_a_warning(self):
        self.make("data.json", b'{"a": "x\x00y"}\n' + DRIVE_D.encode("ascii"))
        code, out = run(self.root)
        self.assertEqual(code, 0, out)
        self.assertIn("пропущен как бинарный: data.json", out)

    def test_every_text_extension_gets_the_warning(self):
        extensions = ("md", "txt", "json", "html", "py", "yml", "yaml", "csv")
        for ext in extensions:
            self.make("f/file." + ext, BINARY)
        self.make("f/upper.JSON", BINARY)
        _, out = run(self.root)
        for ext in extensions:
            self.assertIn("пропущен как бинарный: f/file." + ext, out)
        self.assertIn("пропущен как бинарный: f/upper.JSON", out)

    def test_real_binary_formats_get_no_warning(self):
        self.make("image.png", b"\x89PNG\x00\x00abc")
        self.make("archive.zip", b"PK\x00\x00")
        self.make("noext", b"\x00\x01")
        code, out = run(self.root)
        self.assertEqual(code, 0, out)
        self.assertNotIn("пропущен", out)

    def test_warning_does_not_repeat_marker_words_from_the_file_name(self):
        self.make("zorkmid-data.json", BINARY)
        markers = self.make("markers.txt", "zorkmid\n")
        _, out = run(self.root, "--markers", str(markers))
        self.assertIn("пропущен как бинарный", out)
        self.assertNotIn("zorkmid", out.lower())

    def test_utf16_without_bom_is_read_as_text(self):
        markers = self.make("markers.txt", "zorkmid\n")
        for name, codec in (("le.txt", "utf-16-le"), ("be.txt", "utf-16-be")):
            with self.subTest(codec):
                self.make(name, "строка\nпро zorkmid\n".encode(codec))
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1, out)
        self.assertEqual(reported_lines(out, "le.txt"), [2], out)
        self.assertEqual(reported_lines(out, "be.txt"), [2], out)
        self.assertNotIn("пропущен", out)

    def test_utf16_with_bom_is_read_as_text_without_warning(self):
        self.make("reg.txt", ("строка\nпро " + DRIVE_D + "x\n").encode("utf-16"))
        code, out = run(self.root)
        self.assertEqual(code, 1, out)
        self.assertEqual(reported_lines(out, "reg.txt"), [2], out)
        self.assertNotIn("пропущен", out)

    def test_binary_with_even_length_is_not_mistaken_for_utf16(self):
        self.make("blob.md", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x01\x00")
        self.make("blob2.md", bytes(range(256)))
        code, out = run(self.root)
        self.assertEqual(code, 0, out)
        self.assertIn("пропущен как бинарный: blob.md", out)
        self.assertIn("пропущен как бинарный: blob2.md", out)


class FileNameMaskingTests(CheckPublicTestCase):
    def test_marker_in_file_name_is_hidden_in_the_output(self):
        self.make("Zorkmid-notes.md", "чисто")
        markers = self.make("markers.txt", "zorkmid\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1)
        self.assertNotIn("zorkmid", out.lower())
        self.assertIn("***-notes.md — ", out)

    def test_marker_in_a_folder_name_and_in_a_forbidden_file_is_hidden_too(self):
        self.make("zorkmid/readme.md", "чисто")
        self.make("zorkmid/doc.docx", "x")
        self.make("blue falcon.docx", "x")
        markers = self.make("markers.txt", "zorkmid\nblue falcon\n")
        code, out = run(self.root, "--markers", str(markers))
        self.assertEqual(code, 1)
        self.assertNotIn("zorkmid", out.lower())
        self.assertNotIn("falcon", out.lower())
        self.assertIn("***/readme.md — ", out)
        self.assertIn("***/doc.docx — запрещённое расширение", out)

    def test_marker_in_file_name_with_line_findings_is_hidden_too(self):
        self.make("zorkmid.md", "про " + DRIVE_D + "x\n")
        markers = self.make("markers.txt", "zorkmid\n")
        _, out = run(self.root, "--markers", str(markers))
        self.assertRegex(out, r"\*\*\*\.md:1 — путь к диску D")
        self.assertNotIn("zorkmid", out.lower())


@unittest.skipUnless(shutil.which("git"), "git не найден")
class GitModeTests(CheckPublicTestCase):
    """В каталоге с .git проверяются файлы git: отслеживаемые и неотслеживаемые, но не игнорируемые."""

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def setUp(self):
        super().setUp()
        self.git("init", "-q")

    def test_tracked_forbidden_file_is_reported(self):
        self.make("leak.docx", "x")
        self.make("fine.md", "чисто")
        self.git("add", "leak.docx", "fine.md")
        code, out = run(self.root)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"leak\.docx — ")
        self.assertNotIn("fine.md", out)

    def test_ignored_files_are_not_checked(self):
        self.make(".gitignore", "*.docx\nprivate/\n")
        self.make("internal.docx", "x")
        self.make("private/notes.txt", "см. " + DRIVE_D + "x")
        self.make("README.md", "чисто")
        self.git("add", ".gitignore", "README.md")
        code, out = run(self.root)
        self.assertEqual(code, 0, out)

    def test_untracked_but_not_ignored_files_are_checked_before_they_are_added(self):
        self.make("new.jpg", "x")
        code, out = run(self.root)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"new\.jpg — ")

    def test_tracked_file_deleted_from_disk_does_not_crash(self):
        path = self.make("gone.md", "x")
        self.git("add", "gone.md")
        path.unlink()
        code, out = run(self.root)
        self.assertEqual(code, 0, out)

    def test_non_ascii_file_names_are_reported_readably(self):
        self.make("записка.docx", "x")
        self.git("add", "записка.docx")
        code, out = run(self.root)
        self.assertEqual(code, 1)
        self.assertIn("записка.docx — ", out)


if __name__ == "__main__":
    unittest.main()
