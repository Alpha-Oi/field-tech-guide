#!/usr/bin/env python3
"""Проверка репозитория на утечку внутренних данных компании.

Запуск (из корня репозитория):

    python tools/check_public.py [--markers FILE]

Что проверяется в файлах, которые видит git (`git ls-files`: отслеживаемые и
ещё не добавленные, но не игнорируемые); если git недоступен — во всём дереве,
кроме `.git`, `dist`, `private` и служебных каталогов:

* запрещённые расширения: docx, pdf, jpg, jpeg;
* строки с путями к диску D и к папке пользователей C: со слэшами любого вида
  (обратный, двойной обратный, прямой); перед буквой диска не должно стоять
  буквы или цифры, поэтому `ID:` и `Android:` путём не считаются; обратный
  слэш перед буквой диска — экранирование (как в регулярном выражении JS), а не
  путь, кроме префиксов расширенного пути с двумя слэшами и «?» или «.». Шаблоны
  собраны из частей, чтобы этот файл сам не попадал под проверку;
* слова из списка маркеров (по умолчанию `private/markers.txt`, если он есть;
  по одному на строку, строки с `#` — комментарии, без учёта регистра). Пробелы,
  табы, неразрывные пробелы, переносы строк и невидимые символы (мягкий перенос
  U+00AD, U+034F, U+180E, U+200B–U+200F, U+202A–U+202E, U+2060–U+2064, U+FE0F,
  U+FEFF) между словами и внутри слов не мешают: поиск идёт по тексту файла
  целиком. Между словами многословного маркера пробелом считаются ещё дефис и
  сущность `&nbsp;` (`&#160;`, `&#xA0;`); у маркера из одного слова такой подмены
  нет. Сами слова в вывод не попадают: в именах файлов они заменены на `***`.

Файл с текстовым расширением, в котором есть нулевые байты (и он не UTF-16),
пропускается с предупреждением; если в файле маркеров нет ни одного слова,
тоже предупреждение. Предупреждения код возврата не меняют.

Код возврата: 0 — чисто, 1 — есть находки. Находки печатаются как
`файл:строка — причина` (для файла целиком — `файл — причина`).
Только стандартная библиотека.
"""

from __future__ import annotations

import argparse
import bisect
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_EXTENSIONS = frozenset({"docx", "pdf", "jpg", "jpeg"})

# Расширения текстовых форматов: файл с нулевыми байтами и таким расширением пропускается не молча.
TEXT_EXTENSIONS = frozenset({"md", "txt", "json", "html", "py", "yml", "yaml", "csv"})

# Каталоги, которые пропускаются при обходе дерева без git (то, что .gitignore не пускает в репозиторий).
SKIP_DIRS = frozenset({
    ".git", "dist", "private", "_private", ".autopilot", "__pycache__", "node_modules", ".ruff_cache",
})

# Перед буквой диска не должно быть буквы или цифры: `ID:`, `Android:` — не путь.
_NOT_AFTER_ALNUM = r"(?<![^\W_])"
# Обратный слэш прямо перед буквой диска — экранирование (`\d:\s` в регулярном выражении), а не путь,
# кроме префиксов расширенного пути: два слэша, «?» или «.» и слэш.
_NOT_ESCAPED = r"(?:(?<!\\)|(?<=\\\\[?.]\\))"
# Между диском и каталогом — один или несколько слэшей любого вида.
_SEPARATORS = r"[\\/]+"
PATH_PATTERNS = (
    (re.compile(_NOT_AFTER_ALNUM + _NOT_ESCAPED + "d:" + _SEPARATORS, re.IGNORECASE), "путь к диску D"),
    (re.compile(_NOT_AFTER_ALNUM + _NOT_ESCAPED + "c:" + _SEPARATORS + "users", re.IGNORECASE), "путь к папке пользователей C"),
)

# Невидимые символы: между словами они заменяют пробел, внутри слов ничего не значат. Приходят из PDF и Word:
# мягкий перенос U+00AD, соединитель графем U+034F, монгольский разделитель U+180E, нулевая ширина и метки
# направления U+200B–U+200F, управление направлением U+202A–U+202E, U+2060–U+2064, селектор вариантов
# U+FE0F, метка порядка байтов U+FEFF.
_INVISIBLE = "\u00ad\u034f\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufe0f\ufeff"
_GAP_CHARS = r"\s" + _INVISIBLE
_GAP = "[" + _GAP_CHARS + "]+"
_HIDDEN = "[" + _INVISIBLE + "]*"
# Между словами многословного маркера пробелом считаются ещё дефис (в том числе U+2010 и неразрывный U+2011)
# и неразрывный пробел в виде HTML-сущности. У маркера из одного слова такой подмены нет.
_WORD_GAP = "(?:[" + _GAP_CHARS + r"\-\u2010\u2011]|&nbsp;|&#160;|&#x[aA]0;)+"

MASK = "***"


class Finding(NamedTuple):
    path: str  # путь для вывода: слова маркеров в нём заменены на «***»
    line: int  # 0 — относится к файлу целиком
    reason: str

    def format(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where} — {self.reason}"


def _git_files(root: Path) -> list[str] | None:
    """Файлы, которые видит git, или None, если git недоступен."""
    if not (root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            capture_output=True, timeout=120, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    names = result.stdout.decode("utf-8", errors="replace").split("\0")
    return sorted({name for name in names if name})


def _walk_files(root: Path) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for filename in filenames:
            found.append((Path(dirpath) / filename).relative_to(root).as_posix())
    return sorted(found)


def list_files(root) -> list[str]:
    """Пути (через «/», от корня) файлов, которые нужно проверить."""
    root = Path(root)
    tracked = _git_files(root)
    return tracked if tracked is not None else _walk_files(root)


def _is_usual_char(char: str) -> bool:
    """Символ из тех, что бывают в русско-английском тексте: ASCII, латиница, кириллица, пунктуация, валюты, «№»."""
    code = ord(char)
    return (
        char in "\t\r\n"
        or 0x20 <= code <= 0x7E
        or 0xA0 <= code <= 0x24F
        or 0x400 <= code <= 0x52F
        or 0x2000 <= code <= 0x206F
        or 0x20A0 <= code <= 0x20CF
        or 0x2100 <= code <= 0x214F
    )


def _utf16_without_bom(raw: bytes) -> str | None:
    """Текст UTF-16 без BOM или None, если файл на него не похож.

    В таком тексте нулевые байты стоят на одной чётности позиций (старший байт
    латиницы и переводов строк), раскодированный текст не содержит U+0000 и
    почти целиком состоит из обычных для русско-английского текста символов.
    """
    sample = raw[: len(raw) // 2 * 2]
    nul_even, nul_odd = sample[0::2].count(0), sample[1::2].count(0)
    total = nul_even + nul_odd
    if total == 0 or max(nul_even, nul_odd) * 10 < total * 9:
        return None
    try:
        text = sample.decode("utf-16-le" if nul_odd >= nul_even else "utf-16-be")
    except UnicodeDecodeError:
        return None
    if not text or "\0" in text:
        return None
    usual = sum(1 for char in text if _is_usual_char(char))
    if usual * 10 < len(text) * 9:
        return None
    return text


def _decode(raw: bytes) -> str | None:
    """Текст файла или None, если файл двоичный."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if b"\0" in raw[:8192]:
        return _utf16_without_bom(raw)
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def load_markers(path) -> list[str]:
    """Слова из файла маркеров: по одному на строку; пустые строки и строки с `#` пропускаются."""
    text = _decode(Path(path).read_bytes()) or ""
    stripped = (line.strip() for line in text.splitlines())
    return [line for line in stripped if line and not line.startswith("#")]


def _marker_pattern(marker: str):
    """Выражение для поиска маркера: слова разделены любыми пробелами, невидимыми символами, дефисами
    и сущностью «&nbsp;», внутри слов невидимые символы допустимы; регистр не важен. Нет слов — None."""
    words = [word for word in re.split(_GAP, marker) if word]
    if not words:
        return None
    parts = [_HIDDEN.join(re.escape(char) for char in word) for word in words]
    return re.compile(_WORD_GAP.join(parts), re.IGNORECASE)


def compile_markers(markers) -> list:
    """Выражения для маркеров, в которых есть хотя бы одно слово; номер маркера — позиция в списке с 1."""
    patterns = (_marker_pattern(marker) for marker in markers)
    return [pattern for pattern in patterns if pattern is not None]


def _mask(text: str, patterns) -> str:
    """Слова маркеров в тексте (имени файла) заменены на «***»; пересекающиеся слова сливаются."""
    spans = sorted(span for pattern in patterns for match in pattern.finditer(text) for span in [match.span()])
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    result, position = [], 0
    for start, end in merged:
        result.append(text[position:start])
        result.append(MASK)
        position = end
    result.append(text[position:])
    return "".join(result)


def _marker_hits(text: str, patterns) -> dict:
    """{номер строки с началом совпадения: [номера маркеров]} по тексту файла целиком."""
    starts, position = [], 0
    for line in text.splitlines(keepends=True):
        starts.append(position)
        position += len(line)
    hits: dict = {}
    for number, pattern in enumerate(patterns, start=1):
        for match in pattern.finditer(text):
            line = bisect.bisect_right(starts, match.start())
            if number not in hits.setdefault(line, []):
                hits[line].append(number)
    return hits


def scan(root, markers=(), skip_files=(), warnings=None) -> list[Finding]:
    """Находки по всем файлам репозитория; skip_files — файлы, которые не проверяются (список маркеров).

    Если передан список `warnings`, в него добавляются предупреждения (строки):
    файлы с текстовым расширением, пропущенные как двоичные.
    """
    root = Path(root)
    patterns = compile_markers(markers)
    skipped = {Path(p).resolve() for p in skip_files}
    findings: list[Finding] = []

    for rel in list_files(root):
        full = root / rel
        if full.resolve() in skipped:
            continue

        shown = _mask(rel, patterns)
        name = rel.rsplit("/", 1)[-1]
        extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        if extension in FORBIDDEN_EXTENSIONS:
            findings.append(Finding(shown, 0, f"запрещённое расширение .{extension}: внутренние документы в репозиторий не кладём"))

        in_name = [str(i) for i, pattern in enumerate(patterns, start=1) if pattern.search(rel)]
        if in_name:
            findings.append(Finding(shown, 0, "в имени файла слово из списка маркеров (№ " + ", ".join(in_name) + ")"))

        try:
            raw = full.read_bytes()
        except OSError:
            continue  # файл есть в индексе git, но удалён с диска
        text = _decode(raw)
        if text is None:
            if extension in TEXT_EXTENSIONS and warnings is not None:
                warnings.append(f"пропущен как бинарный: {shown}")
            continue

        for number, line in enumerate(text.splitlines(), start=1):
            for pattern, reason in PATH_PATTERNS:
                if pattern.search(line):
                    findings.append(Finding(shown, number, reason))
        for number, hit in sorted(_marker_hits(text, patterns).items()):
            findings.append(Finding(shown, number, "слово из списка маркеров (№ " + ", ".join(map(str, sorted(hit))) + ")"))

    return sorted(findings, key=lambda f: (f.path, f.line, f.reason))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_public.py",
        description="Проверка репозитория на утечку внутренних данных компании. Код возврата: 0 — чисто, 1 — есть находки.",
    )
    parser.add_argument(
        "--markers", metavar="FILE",
        help="файл со словами-маркерами, по одному на строку, `#` — комментарий (по умолчанию private/markers.txt, если он есть)",
    )
    parser.add_argument("--root", metavar="DIR", help="корень проверяемого репозитория (по умолчанию корень этого проекта)")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve() if args.root else ROOT
    markers_path = None
    if args.markers:
        markers_path = Path(args.markers)
        if not markers_path.is_file():
            print(f"Ошибка: файл маркеров не найден: {markers_path}")
            return 1
    elif (root / "private" / "markers.txt").is_file():
        markers_path = root / "private" / "markers.txt"

    warnings: list[str] = []
    try:
        markers = load_markers(markers_path) if markers_path else []
        if markers_path and not compile_markers(markers):
            warnings.append(f"в файле маркеров {markers_path} слов нет, проверка по словам не выполняется")
        findings = scan(root, markers, skip_files=[markers_path] if markers_path else [], warnings=warnings)
    except OSError as exc:
        print(f"Ошибка: {exc}")
        return 1

    for warning in warnings:
        print(f"Предупреждение: {warning}")
    for finding in findings:
        print(finding.format())
    if findings:
        print(f"Найдено проблем: {len(findings)}. Убери их из публичных файлов или добавь файл в .gitignore.")
        return 1
    print("Проверка пройдена: утечек не найдено.")
    return 0


def _use_utf8_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


if __name__ == "__main__":
    _use_utf8_streams()
    sys.exit(main())
