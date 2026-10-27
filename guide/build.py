#!/usr/bin/env python3
"""Сборка гайда: чтение и проверка контента, подстановка профиля, рендер.

Только стандартная библиотека. Запускается как скрипт (`python guide/build.py`)
и импортируется как модуль (`from guide import build`).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

GUIDE_DIR = Path(__file__).resolve().parent
ROOT = GUIDE_DIR.parent

DATA_MARKER = "<!--GUIDE_DATA-->"

# Рантайм-токены подставляет страница из полей «Памятки»; в guide.md они
# превращаются в видимые заглушки.
RUNTIME_TOKENS = {
    "order": "[НОМЕР ЗАЯВКИ]",
    "phone": "[ТВОЙ ТЕЛЕФОН С 8]",
}

TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
# Идентификаторы проверяются через fullmatch и только по ASCII: `$` пропускает перевод строки
# в конце, а `\d` без re.ASCII принимает цифры любых письменностей.
VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)


class ContentError(ValueError):
    """Ошибка чтения контента, профиля или шаблона (сообщение — по-русски)."""


class UnknownVarsError(ContentError):
    """В контенте есть переменные профиля, которых нет в профиле."""

    def __init__(self, names):
        self.names = sorted(names)
        super().__init__(
            "Неизвестные переменные профиля: " + ", ".join(self.names)
        )


# ---------------------------------------------------------------------------
# Чтение контента и профиля
# ---------------------------------------------------------------------------

def _read_json(path: Path):
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContentError(f"{path.name}: файл не в кодировке UTF-8 ({exc.reason})") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContentError(
            f"{path.name}: некорректный JSON — {exc.msg} (строка {exc.lineno}, столбец {exc.colno})"
        ) from exc


def load_content(content_dir: str) -> dict:
    """Читает все `*.json` из папки по порядку имён файлов.

    Возвращает {"stages": [...], "cheatsheet": {...} | None}. Проверку смысла
    выполняет validate(); здесь — только чтение и разбор файлов.
    """
    base = Path(content_dir)
    if not base.is_dir():
        raise ContentError(f"Папка контента не найдена: {content_dir} (нет файлов контента)")
    files = sorted(
        (p for p in base.iterdir() if p.is_file() and p.suffix.lower() == ".json"),
        key=lambda p: p.name,
    )
    if not files:
        raise ContentError(f"В папке {content_dir} нет файлов контента (*.json)")

    stages = []
    cheatsheet = None
    cheatsheet_file = None
    for path in files:
        obj = _read_json(path)
        if not isinstance(obj, dict):
            raise ContentError(f"{path.name}: в корне файла должен быть объект {{...}}")
        if "stage" not in obj and "cheatsheet" not in obj:
            raise ContentError(f"{path.name}: нет ключа «stage» (файл этапа) или «cheatsheet» (памятка)")
        if "stage" in obj:
            stage_info = obj["stage"]
            if not isinstance(stage_info, dict):
                raise ContentError(f"{path.name}: «stage» должен быть объектом с полями id, title, intro")
            stage = dict(stage_info)
            stage["steps"] = obj.get("steps", [])
            stages.append(stage)
        if "cheatsheet" in obj:
            if cheatsheet is not None:
                raise ContentError(
                    f"{path.name}: памятка уже описана в {cheatsheet_file}; нужен один файл с «cheatsheet»"
                )
            cheatsheet = obj["cheatsheet"]
            cheatsheet_file = path.name
    return {"stages": stages, "cheatsheet": cheatsheet}


def load_profile(path: str) -> dict:
    """Читает профиль {"name": str, "vars": {str: str}} и проверяет его форму."""
    p = Path(path)
    if not p.is_file():
        raise ContentError(f"Профиль не найден: {path}")
    obj = _read_json(p)
    if not isinstance(obj, dict):
        raise ContentError(f"{p.name}: в корне профиля должен быть объект {{...}}")
    name = obj.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ContentError(f"{p.name}: у профиля нет строкового поля «name»")
    values = obj.get("vars")
    if not isinstance(values, dict):
        raise ContentError(f"{p.name}: у профиля нет объекта «vars» с переменными")
    for key, value in values.items():
        if not VAR_NAME_RE.fullmatch(key):
            raise ContentError(f"{p.name}: имя переменной «{key}» должно состоять из латинских букв, цифр и «_»")
        if key in RUNTIME_TOKENS:
            raise ContentError(
                f"{p.name}: имя «{key}» зарезервировано под рантайм-токен страницы, возьми другое"
            )
        if not isinstance(value, str):
            raise ContentError(f"{p.name}: значение переменной «{key}» должно быть строкой")
    return {"name": name, "vars": dict(values)}


# ---------------------------------------------------------------------------
# Обход строк контента и подстановка профиля
# ---------------------------------------------------------------------------

def _report_keys(report) -> frozenset:
    """Ключи полей и вычисляемых значений отчёта (не переменные профиля)."""
    keys = set()
    if isinstance(report, dict):
        for section in ("fields", "computed"):
            items = report.get(section)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get("key"), str):
                        keys.add(item["key"])
    return frozenset(keys)


def _map_strings(node, fn, protected=frozenset(), literal=False):
    """Новая структура, в которой каждая строка заменена на fn(строка, protected, literal)."""
    if isinstance(node, str):
        return fn(node, protected, literal)
    if isinstance(node, list):
        return [_map_strings(item, fn, protected, literal) for item in node]
    if isinstance(node, dict):
        return {key: _map_strings(value, fn, protected, literal) for key, value in node.items()}
    return node


def _map_report(report, fn):
    keys = _report_keys(report)
    result = {}
    for key, value in report.items():
        if key == "template":
            # в шаблоне отчёта допустимы только ключи отчёта и рантайм-токены
            result[key] = _map_strings(value, fn, keys, True)
        elif key == "note":
            result[key] = _map_strings(value, fn, keys, False)
        else:
            result[key] = _map_strings(value, fn)
    return result


def _map_cheatsheet(cheatsheet, fn):
    result = {}
    for key, value in cheatsheet.items():
        if key == "reports" and isinstance(value, list):
            result[key] = [
                _map_report(item, fn) if isinstance(item, dict) else _map_strings(item, fn)
                for item in value
            ]
        else:
            result[key] = _map_strings(value, fn)
    return result


def _map_data(data, fn):
    """Обходит все строки контента, учитывая, где токены — ключи отчёта."""
    if not isinstance(data, dict):
        return _map_strings(data, fn)
    result = {}
    for key, value in data.items():
        if key == "cheatsheet" and isinstance(value, dict):
            result[key] = _map_cheatsheet(value, fn)
        elif key == "pricelist":
            # прайс — обычный текст без переменных профиля (interfaces.md, U7)
            result[key] = value
        else:
            result[key] = _map_strings(value, fn)
    return result


def collect_vars(data: dict) -> list:
    """Имена переменных профиля, встретившихся в контенте (по алфавиту).

    Рантайм-токены, ключи отчётов и шаблоны отчётов не считаются.
    """
    found = set()

    def scan(text, protected, literal):
        if not literal:
            for match in TOKEN_RE.finditer(text):
                name = match.group(1)
                if name not in RUNTIME_TOKENS and name not in protected:
                    found.add(name)
        return text

    _map_data(data, scan)
    return sorted(found)


def _substitute(text: str, values: dict, protected, unknown: set) -> str:
    def replace(match):
        name = match.group(1)
        if name in RUNTIME_TOKENS or name in protected:
            return match.group(0)
        if name in values:
            return values[name]
        unknown.add(name)
        return match.group(0)

    return TOKEN_RE.sub(replace, text)


def apply_profile(data: dict, profile: dict, strict: bool = True) -> dict:
    """Новая структура с подставленными переменными профиля.

    Рантайм-токены ({{order}}, {{phone}}) и ключи отчётов остаются нетронутыми.
    Неизвестная переменная — UnknownVarsError (это ValueError) с именами; при
    strict=False такие метки остаются в тексте как есть.
    """
    values = profile.get("vars", {}) if isinstance(profile, dict) else {}
    unknown: set = set()

    def substitute(text, protected, literal):
        if literal:
            return text
        return _substitute(text, values, protected, unknown)

    result = _map_data(data, substitute)
    if strict and unknown:
        raise UnknownVarsError(unknown)
    return result


# ---------------------------------------------------------------------------
# Проверка контента
# ---------------------------------------------------------------------------

KINDS = ("do", "buy", "know")
FIELD_TYPES = ("number", "text")
STAGE_ID_RE = re.compile(r"s\d+[a-z]?", re.ASCII)
STEP_ID_RE = re.compile(r"s\d+[a-z]?-\d{2}", re.ASCII)
REQ_ID_RE = re.compile(r"[A-Z]\d+[a-z]?", re.ASCII)
# Ссылка гайда: одно правило с safeUrl в template.html — «https://» и хотя бы один символ без пробелов.
# [^\s\ufeff]: в JS пробельным считается и U+FEFF, в Python — нет; исключаем его, чтобы Python
# не пропускал то, что страница отбросит.
LINK_RE = re.compile(r"https://[^\s\ufeff]+")
LINK_SPACE_RE = re.compile(r"[\s\ufeff]")

STAGE_FIELDS = ("id", "title", "intro", "steps")
STEP_FIELDS = ("id", "title", "kind", "optional", "lead", "points", "send", "links", "tip", "proof", "reqs")
CHEATSHEET_FIELDS = ("title", "chain", "reports")
CHAIN_FIELDS = ("when", "app", "chat")
REPORT_FIELDS = ("id", "title", "fields", "computed", "template", "note")


def _is_text(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _fmt_path(path) -> str:
    result = ""
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            result += ("." if result else "") + str(part)
    return result


def _iter_strings(node, path=()):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _iter_strings(item, path + (index,))
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from _iter_strings(value, path + (key,))


def _check_text(errors, owner, path, text, allow_runtime=False):
    """Рантайм-токены только там, где можно; фигурные скобки парные."""
    field = _fmt_path(path)
    reported = set()
    for match in TOKEN_RE.finditer(text):
        name = match.group(1)
        if name in RUNTIME_TOKENS and not allow_runtime and name not in reported:
            reported.add(name)
            token = "{{" + name + "}}"
            errors.append(
                f"{owner}: метка {token} допустима только в send[].text и в chat цепочки памятки, "
                f"а стоит в поле «{field}»"
            )
    rest = TOKEN_RE.sub("", text)
    if "{{" in rest or "}}" in rest:
        errors.append(f"{owner}: в поле «{field}» непарные фигурные скобки {{{{ }}}} — проверь метку")


def _check_text_list(errors, owner, field, value, required=False):
    """Список непустых строк (для points)."""
    if value is None:
        return
    if not isinstance(value, list) or not all(_is_text(item) for item in value):
        errors.append(f"{owner}: {field} должен быть списком непустых строк")
    elif required and not value:
        errors.append(f"{owner}: {field} не должен быть пустым")


def _link_is_acceptable(url: str) -> bool:
    """Ссылка шага после подстановки профиля: `https://` и хотя бы один символ, пробелов нет.

    Метка профиля, оставшаяся в тексте в режиме --lenient-vars (о ней сообщается отдельным
    списком), может скрывать любую часть адреса. Проверяется только известное: в постоянных
    кусках нет пробелов, а то, что стоит перед первой меткой, не противоречит «https://»
    (иначе, например, `javascript:{{x}}` прошло бы).
    """
    first = TOKEN_RE.search(url)
    if first is None:
        return LINK_RE.fullmatch(url) is not None
    literal = TOKEN_RE.sub("", url)
    if LINK_SPACE_RE.search(literal):
        return False
    prefix = url[:first.start()]
    return "https://".startswith(prefix) or prefix.startswith("https://")


def _validate_step(step, position, stage_id, seen_ids, errors):
    if not isinstance(step, dict):
        errors.append(f"Этап {stage_id or position}: шаг №{position} должен быть объектом")
        return
    sid = step.get("id")
    owner = f"Шаг {sid}" if _is_text(sid) else f"Шаг №{position} этапа {stage_id}"

    if "id" not in step:
        errors.append(f"{owner}: нет поля id")
    elif not isinstance(sid, str) or not STEP_ID_RE.fullmatch(sid):
        errors.append(f"{owner}: id должен выглядеть как s1-01 (s<номер этапа>[буква]-<две цифры>)")
    else:
        if stage_id and sid.split("-")[0] != stage_id:
            errors.append(f"{owner}: id не относится к этапу {stage_id} — он должен начинаться с «{stage_id}-»")
        if sid in seen_ids:
            errors.append(f"{owner}: id повторяется — в гайде уже есть шаг с таким id")
        seen_ids.add(sid)

    for key in step:
        if key not in STEP_FIELDS:
            errors.append(f"{owner}: неизвестное поле «{key}»")

    if not _is_text(step.get("title")):
        errors.append(f"{owner}: нет поля title (заголовок шага)")
    if not _is_text(step.get("lead")):
        errors.append(f"{owner}: нет поля lead (одно–два предложения о шаге)")

    kind = step.get("kind")
    if "kind" not in step:
        errors.append(f"{owner}: нет поля kind (допустимо do, buy или know)")
    elif kind not in KINDS:
        errors.append(f"{owner}: kind «{kind}» не подходит — допустимо do, buy или know")

    if "optional" in step and not isinstance(step["optional"], bool):
        errors.append(f"{owner}: optional должен быть true или false")

    _check_text_list(errors, owner, "points", step.get("points"))

    send = step.get("send")
    if send is not None:
        if not isinstance(send, list):
            errors.append(f"{owner}: send должен быть списком сообщений")
        else:
            for index, item in enumerate(send):
                if not (isinstance(item, dict) and _is_text(item.get("label")) and _is_text(item.get("text"))):
                    errors.append(f"{owner}: send[{index}] должен содержать непустые строки label и text")

    links = step.get("links")
    if links is not None:
        if not isinstance(links, list):
            errors.append(f"{owner}: links должен быть списком ссылок")
        else:
            for index, item in enumerate(links):
                if not (isinstance(item, dict) and _is_text(item.get("label")) and _is_text(item.get("url"))):
                    errors.append(f"{owner}: links[{index}] должен содержать непустые строки label и url")
                    continue
                url = item["url"]
                if not _link_is_acceptable(url):
                    errors.append(
                        f"{owner}: ссылка «{item['label']}» ведёт на «{url}» — "
                        f"адрес должен начинаться с https:// и не должен содержать пробелов"
                    )

    if step.get("tip") is not None and not _is_text(step["tip"]):
        errors.append(f"{owner}: tip должен быть непустой строкой")

    proof = step.get("proof")
    if proof is None:
        if kind in ("do", "buy"):
            errors.append(f"{owner}: у шага вида {kind} нет поля proof (строка «Готово, когда …»)")
    elif not isinstance(proof, str) or not proof.startswith("Готово,"):
        errors.append(f"{owner}: proof должен начинаться со слов «Готово,»")

    reqs = step.get("reqs")
    if "reqs" not in step:
        errors.append(f"{owner}: нет поля reqs (список требований, например [\"R05\"])")
    elif not isinstance(reqs, list) or not reqs:
        errors.append(f"{owner}: reqs должен быть непустым списком идентификаторов требований")
    else:
        for req in reqs:
            if not isinstance(req, str) or not REQ_ID_RE.fullmatch(req):
                errors.append(
                    f"{owner}: в reqs стоит «{req}» — идентификатор требования выглядит как R05, G18 или R75i"
                )

    for path, text in _iter_strings(step):
        _check_text(errors, owner, path, text, allow_runtime=path[0] == "send" and path[-1] == "text")


def _validate_stage(stage, position, seen_stage_ids, seen_step_ids, errors):
    if not isinstance(stage, dict):
        errors.append(f"Этап №{position} должен быть объектом")
        return
    sid = stage.get("id")
    owner = f"Этап {sid}" if _is_text(sid) else f"Этап №{position}"

    valid_id = isinstance(sid, str) and bool(STAGE_ID_RE.fullmatch(sid))
    if not valid_id:
        errors.append(f"{owner}: id этапа должен выглядеть как s1 или s4b")
    elif sid in seen_stage_ids:
        errors.append(f"{owner}: id этапа повторяется")
    if valid_id:
        seen_stage_ids.add(sid)

    for key in stage:
        if key not in STAGE_FIELDS:
            errors.append(f"{owner}: неизвестное поле «{key}»")
    if not _is_text(stage.get("title")):
        errors.append(f"{owner}: нет поля title (название этапа)")
    if stage.get("intro") is not None and not _is_text(stage["intro"]):
        errors.append(f"{owner}: intro должен быть непустой строкой")

    steps = stage.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append(f"{owner}: в этапе нет шагов (steps)")
        steps = []
    for index, step in enumerate(steps, start=1):
        _validate_step(step, index, sid if valid_id else None, seen_step_ids, errors)

    for key in ("title", "intro"):
        if isinstance(stage.get(key), str):
            _check_text(errors, owner, (key,), stage[key])


def _validate_report(report, position, seen_ids, errors):
    if not isinstance(report, dict):
        errors.append(f"Памятка: отчёт №{position} должен быть объектом")
        return
    rid = report.get("id")
    owner = f"Отчёт {rid}" if _is_text(rid) else f"Отчёт №{position}"

    if not _is_text(rid):
        errors.append(f"{owner}: нет поля id")
    elif rid in seen_ids:
        errors.append(f"{owner}: id отчёта повторяется")
    else:
        seen_ids.add(rid)
    for key in report:
        if key not in REPORT_FIELDS:
            errors.append(f"{owner}: неизвестное поле «{key}»")
    if not _is_text(report.get("title")):
        errors.append(f"{owner}: нет поля title (название отчёта)")

    keys = {}  # ключ -> тип поля или "computed"
    fields = report.get("fields")
    if not isinstance(fields, list) or not fields:
        errors.append(f"{owner}: нет списка fields (поля отчёта)")
        fields = []
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            errors.append(f"{owner}: fields[{index}] должен быть объектом с key, label и type")
            continue
        key = field.get("key")
        if not (isinstance(key, str) and VAR_NAME_RE.fullmatch(key)) or key in RUNTIME_TOKENS:
            errors.append(f"{owner}: fields[{index}].key «{key}» не подходит — нужны латинские буквы, цифры и «_», не order и не phone")
            continue
        if key in keys:
            errors.append(f"{owner}: ключ «{key}» повторяется")
        if not _is_text(field.get("label")):
            errors.append(f"{owner}: у поля «{key}» нет label")
        if field.get("type") not in FIELD_TYPES:
            errors.append(f"{owner}: поле «{key}» имеет type «{field.get('type')}» — допустимо number или text")
        keys[key] = field.get("type")

    computed = report.get("computed", [])
    if not isinstance(computed, list):
        errors.append(f"{owner}: computed должен быть списком")
        computed = []
    field_types = dict(keys)
    for index, item in enumerate(computed):
        if not isinstance(item, dict):
            errors.append(f"{owner}: computed[{index}] должен быть объектом с key, op, a и b")
            continue
        key = item.get("key")
        if not (isinstance(key, str) and VAR_NAME_RE.fullmatch(key)) or key in RUNTIME_TOKENS:
            errors.append(f"{owner}: computed[{index}].key «{key}» не подходит — нужны латинские буквы, цифры и «_», не order и не phone")
            continue
        if key in keys:
            errors.append(f"{owner}: ключ «{key}» повторяется")
        if item.get("op") != "sub":
            errors.append(f"{owner}: вычисляемое «{key}» с op «{item.get('op')}» — поддерживается только sub")
        for side in ("a", "b"):
            ref = item.get(side)
            # ссылка — это имя поля; список или объект в JSON не должны ронять проверку
            if not isinstance(ref, str) or field_types.get(ref) != "number":
                errors.append(
                    f"{owner}: вычисляемое «{key}» ссылается в {side} на «{ref}», "
                    f"а такого числового поля среди fields нет"
                )
        keys[key] = "computed"

    template = report.get("template")
    if not _is_text(template):
        errors.append(f"{owner}: нет поля template (текст отчёта)")
    else:
        seen_tokens = set()
        for match in TOKEN_RE.finditer(template):
            name = match.group(1)
            if name in keys or name in RUNTIME_TOKENS or name in seen_tokens:
                continue
            seen_tokens.add(name)
            errors.append(f"{owner}: в шаблоне метка {{{{{name}}}}} не найдена среди fields и computed")
        _check_text(errors, owner, ("template",), template, allow_runtime=True)
    note = report.get("note")
    if note is not None:
        if not _is_text(note):
            errors.append(f"{owner}: note должен быть непустой строкой")
        else:
            _check_text(errors, owner, ("note",), note)
    if isinstance(report.get("title"), str):
        _check_text(errors, owner, ("title",), report["title"])
    for index, field in enumerate(fields):
        if isinstance(field, dict) and isinstance(field.get("label"), str):
            _check_text(errors, owner, ("fields", index, "label"), field["label"])


def _validate_cheatsheet(cheatsheet, errors):
    if not isinstance(cheatsheet, dict):
        errors.append("Нет памятки на заявку: добавь файл контента с ключом «cheatsheet» (например, 90-cheatsheet.json)")
        return
    owner = "Памятка"
    for key in cheatsheet:
        if key not in CHEATSHEET_FIELDS:
            errors.append(f"{owner}: неизвестное поле «{key}»")
    if not _is_text(cheatsheet.get("title")):
        errors.append(f"{owner}: нет поля title")
    else:
        _check_text(errors, owner, ("title",), cheatsheet["title"])

    chain = cheatsheet.get("chain")
    if not isinstance(chain, list) or not chain:
        errors.append(f"{owner}: нет цепочки chain («когда → что нажать → что написать»)")
        chain = []
    for index, item in enumerate(chain, start=1):
        link_owner = f"{owner}, цепочка №{index}"
        if not isinstance(item, dict):
            errors.append(f"{link_owner}: должен быть объектом с полями when, app и chat")
            continue
        for key in item:
            if key not in CHAIN_FIELDS:
                errors.append(f"{link_owner}: неизвестное поле «{key}»")
        if not _is_text(item.get("when")):
            errors.append(f"{link_owner}: нет поля when")
        for key in ("app", "chat"):
            if key in item and not _is_text(item[key]):
                errors.append(f"{link_owner}: {key} должен быть непустой строкой")
        for key, value in item.items():
            if isinstance(value, str):
                _check_text(errors, link_owner, (key,), value, allow_runtime=key == "chat")

    reports = cheatsheet.get("reports")
    if not isinstance(reports, list) or not reports:
        errors.append(f"{owner}: нет списка reports (формы отчётов)")
        reports = []
    seen_ids: set = set()
    for index, report in enumerate(reports, start=1):
        _validate_report(report, index, seen_ids, errors)


def validate(data: dict) -> list:
    """Человекочитаемые ошибки контента (по-русски); пустой список — всё в порядке.

    Ожидает данные после apply_profile. Метки профиля, оставшиеся из-за
    --lenient-vars, здесь не ошибка: их сообщает отдельный список.
    """
    if not isinstance(data, dict):
        return ["Данные гайда должны быть объектом со списком этапов stages"]
    errors: list = []
    stages = data.get("stages")
    if not isinstance(stages, list) or not stages:
        errors.append("В контенте нет ни одного этапа: добавь файл с ключом «stage»")
        stages = []
    seen_stage_ids: set = set()
    seen_step_ids: set = set()
    for index, stage in enumerate(stages, start=1):
        _validate_stage(stage, index, seen_stage_ids, seen_step_ids, errors)
    _validate_cheatsheet(data.get("cheatsheet"), errors)
    if data.get("pricelist") is not None:
        errors.extend(validate_pricelist(data["pricelist"]))
    return errors


# ---------------------------------------------------------------------------
# Прайс (interfaces.md, U7)
# ---------------------------------------------------------------------------

PRICE_KINDS = ("fixed", "from", "ask", "na")
PRICELIST_FIELDS = ("title", "currency", "note", "items", "matrices")
PRICE_ITEM_FIELDS = ("id", "no", "group", "name", "kind", "price", "description")
PRICE_MATRIX_FIELDS = ("id", "title", "note", "columns", "rows")
PRICE_COLUMN_FIELDS = ("id", "name")
PRICE_ROW_FIELDS = ("id", "label", "prices")
PRICE_HINT_FIELDS = ("hint", "from")
ASK_CELL = "ask"


def load_pricelist(path: str) -> dict:
    """Читает прайс (JSON-объект по схеме U7); смысл проверяет validate_pricelist()."""
    p = Path(path)
    if not p.is_file():
        raise ContentError(f"Прайс не найден: {path}")
    obj = _read_json(p)
    if not isinstance(obj, dict):
        raise ContentError(f"{p.name}: в корне прайса должен быть объект {{...}}")
    return obj


def _is_whole_price(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _price_text_errors(errors, owner, field, value):
    """Прайс — обычный текст: меток профиля {{…}} в нём быть не должно."""
    if isinstance(value, str) and ("{{" in value or "}}" in value):
        errors.append(f"{owner}: в поле {field} есть метка {{{{…}}}} — в прайсе переменных профиля нет, пиши обычный текст")


def _validate_price_item(item, position, seen_ids, errors):
    if not isinstance(item, dict):
        errors.append(f"Позиция №{position}: должна быть объектом с полями id, no, group, name, kind, price")
        return
    iid = item.get("id")
    owner = f"Позиция {iid}" if _is_text(iid) else f"Позиция №{position}"

    if not _is_text(iid):
        errors.append(f"{owner}: нет поля id")
    elif iid in seen_ids:
        errors.append(f"{owner}: id повторяется — в прайсе уже есть позиция с таким id")
    else:
        seen_ids.add(iid)

    for key in item:
        if key not in PRICE_ITEM_FIELDS:
            errors.append(f"{owner}: неизвестное поле «{key}»")

    if not isinstance(item.get("no"), str) or not item["no"].strip():
        errors.append(f"{owner}: no должен быть непустой строкой (номер как в прайсе, например \"14.1\")")
    if not _is_text(item.get("group")):
        errors.append(f"{owner}: нет поля group (группа для навигации)")
    if not _is_text(item.get("name")):
        errors.append(f"{owner}: нет поля name (название услуги)")
    if "description" in item and not isinstance(item["description"], str):
        errors.append(f"{owner}: description должен быть строкой (может быть пустой)")

    kind = item.get("kind")
    price = item.get("price")
    if kind not in PRICE_KINDS:
        errors.append(f"{owner}: kind «{kind}» не подходит — допустимо fixed, from, ask или na")
    elif kind in ("fixed", "from"):
        if not _is_whole_price(price):
            errors.append(f"{owner}: для вида {kind} нужен price — целое число от 0, а стоит {price!r}")
    elif price is not None:
        errors.append(f"{owner}: для вида {kind} price должен быть null, а стоит {price!r}")

    for key in ("no", "group", "name", "description"):
        _price_text_errors(errors, owner, key, item.get(key))


def _validate_price_hint_cell(cell, owner, errors):
    """Ячейка-объект {"hint": "от 2900 ₽", "from": 2900}: цену вводит мастер, hint — подсказка у поля."""
    for key in cell:
        if key not in PRICE_HINT_FIELDS:
            errors.append(f"{owner}: в ячейке-объекте неизвестное поле «{key}» (допустимо hint и from)")
    if not _is_text(cell.get("hint")):
        errors.append(f"{owner}: в ячейке-объекте нужен hint — непустая строка, например \"от 2900 ₽\"")
    else:
        _price_text_errors(errors, owner, "hint", cell["hint"])
    floor = cell.get("from")
    if floor is not None and not _is_whole_price(floor):
        errors.append(f"{owner}: from в ячейке-объекте должен быть целым числом от 0 или null, а стоит {floor!r}")


def _validate_price_matrix(matrix, position, seen_ids, errors):
    if not isinstance(matrix, dict):
        errors.append(f"Матрица №{position}: должна быть объектом с полями id, title, columns, rows")
        return
    mid = matrix.get("id")
    owner = f"Матрица {mid}" if _is_text(mid) else f"Матрица №{position}"

    if not _is_text(mid):
        errors.append(f"{owner}: нет поля id")
    elif mid in seen_ids:
        errors.append(f"{owner}: id повторяется — в прайсе уже есть матрица с таким id")
    else:
        seen_ids.add(mid)

    for key in matrix:
        if key not in PRICE_MATRIX_FIELDS:
            errors.append(f"{owner}: неизвестное поле «{key}»")
    if not _is_text(matrix.get("title")):
        errors.append(f"{owner}: нет поля title (название таблицы)")
    if "note" in matrix and not isinstance(matrix["note"], str):
        errors.append(f"{owner}: note должен быть строкой")
    for key in ("title", "note"):
        _price_text_errors(errors, owner, key, matrix.get(key))

    column_ids = set()
    columns = matrix.get("columns")
    if not isinstance(columns, list) or not columns:
        errors.append(f"{owner}: нет списка columns (услуги — столбцы таблицы)")
        columns = []
    for index, column in enumerate(columns, start=1):
        if not isinstance(column, dict):
            errors.append(f"{owner}: столбец №{index} должен быть объектом с полями id и name")
            continue
        cid = column.get("id")
        if not _is_text(cid):
            errors.append(f"{owner}: у столбца №{index} нет id")
        elif cid in column_ids:
            errors.append(f"{owner}: id столбца {cid} повторяется")
        else:
            column_ids.add(cid)
        for key in column:
            if key not in PRICE_COLUMN_FIELDS:
                errors.append(f"{owner}, столбец {cid or index}: неизвестное поле «{key}»")
        if not _is_text(column.get("name")):
            errors.append(f"{owner}: у столбца {cid or index} нет name (название услуги)")
        _price_text_errors(errors, f"{owner}, столбец {cid or index}", "name", column.get("name"))

    row_ids = set()
    rows = matrix.get("rows")
    if not isinstance(rows, list) or not rows:
        errors.append(f"{owner}: нет списка rows (модели — строки таблицы)")
        rows = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            errors.append(f"{owner}: строка №{index} должна быть объектом с полями id, label, prices")
            continue
        rid = row.get("id")
        row_owner = f"{owner}, строка {rid if _is_text(rid) else '№' + str(index)}"
        if not _is_text(rid):
            errors.append(f"{row_owner}: нет id")
        elif rid in row_ids:
            errors.append(f"{row_owner}: id строки {rid} повторяется")
        else:
            row_ids.add(rid)
        for key in row:
            if key not in PRICE_ROW_FIELDS:
                errors.append(f"{row_owner}: неизвестное поле «{key}»")
        if not _is_text(row.get("label")):
            errors.append(f"{row_owner}: нет label (название модели)")
        _price_text_errors(errors, row_owner, "label", row.get("label"))

        prices = row.get("prices", {})
        if not isinstance(prices, dict):
            errors.append(f"{row_owner}: prices должен быть объектом {{id столбца: цена}}")
            continue
        for column_id, cell in prices.items():
            if column_id not in column_ids:
                errors.append(f"{row_owner}: в prices есть ключ «{column_id}», а столбца с таким id нет")
            elif isinstance(cell, dict):
                _validate_price_hint_cell(cell, f"{row_owner}, столбец {column_id}", errors)
            elif not (cell is None or cell == ASK_CELL or _is_whole_price(cell)):
                errors.append(
                    f"{row_owner}: цена в столбце {column_id} — {cell!r} не подходит: "
                    f"нужно целое число от 0, \"ask\", null или объект {{\"hint\": …, \"from\": …}}"
                )


def validate_pricelist(pricelist) -> list:
    """Человекочитаемые ошибки прайса (по-русски); пустой список — всё в порядке."""
    if not isinstance(pricelist, dict):
        return ["Прайс должен быть объектом {...} с полями title, currency, items"]
    errors: list = []
    for key in pricelist:
        if key not in PRICELIST_FIELDS:
            errors.append(f"Прайс: неизвестное поле «{key}»")
    if not _is_text(pricelist.get("title")):
        errors.append("Прайс: нет поля title (название прайса)")
    if not _is_text(pricelist.get("currency")):
        errors.append("Прайс: нет поля currency (например, \"₽\")")
    if "note" in pricelist and not isinstance(pricelist["note"], str):
        errors.append("Прайс: note должен быть строкой")
    for key in ("title", "currency", "note"):
        _price_text_errors(errors, "Прайс", key, pricelist.get(key))

    items = pricelist.get("items")
    if not isinstance(items, list):
        errors.append("Прайс: нет списка items (позиции; может быть пустым, если есть матрицы)")
        items = []
    matrices = pricelist.get("matrices", [])
    if not isinstance(matrices, list):
        errors.append("Прайс: matrices должен быть списком матриц")
        matrices = []

    seen_items: set = set()
    for index, item in enumerate(items, start=1):
        _validate_price_item(item, index, seen_items, errors)
    seen_matrices: set = set()
    for index, matrix in enumerate(matrices, start=1):
        _validate_price_matrix(matrix, index, seen_matrices, errors)

    if isinstance(pricelist.get("items"), list) and not items and not matrices:
        errors.append("В прайсе нет ни позиций, ни матриц")
    return errors


# ---------------------------------------------------------------------------
# Рендер: guide.md, index.html, for-techdir.md
# ---------------------------------------------------------------------------

MD_TITLE = "# Гайд для мастеров ИТ-ремонта"
MINUS = "−"
DONE_LABELS = {"do": "Сделано", "buy": "Куплено", "know": "Прочитал"}
FIELD_TYPE_LABELS = {"number": "число", "text": "текст"}


def _md_text(text) -> str:
    """Рантайм-токены → видимые заглушки; остальное без изменений."""
    return TOKEN_RE.sub(lambda m: RUNTIME_TOKENS.get(m.group(1), m.group(0)), str(text))


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    mark = "`" * max(3, longest + 1)
    return f"{mark}\n{text}\n{mark}"


def _md_step(step: dict, level: str) -> list:
    blocks = [f"{level} {_md_text(step.get('title', ''))}", _md_text(step.get("lead", ""))]

    points = step.get("points") or []
    if points:
        numbered = step.get("kind") == "do"  # подшаги действия идут по порядку
        blocks.append("\n".join(
            (f"{index}. " if numbered else "- ") + _md_text(point)
            for index, point in enumerate(points, start=1)
        ))

    for message in step.get("send") or []:
        blocks.append(f"**Сообщение в чат: {_md_text(message.get('label', ''))}**")
        blocks.append(_fence(_md_text(message.get("text", ""))))

    links = step.get("links") or []
    if links:
        blocks.append("\n".join(
            f"- [{_md_text(link.get('label', ''))}]({link.get('url', '')})" for link in links
        ))

    tip = step.get("tip")
    if tip:
        lines = _md_text(tip).splitlines() or [""]
        blocks.append("\n".join(
            ("> **Совет.** " if index == 0 else "> ") + line for index, line in enumerate(lines)
        ))

    if step.get("proof"):
        blocks.append(_md_text(step["proof"]))

    blocks.append("- [ ] " + DONE_LABELS.get(step.get("kind"), "Сделано"))
    return blocks


def _md_report_text(text: str, report: dict) -> str:
    """Шаблон отчёта для текстовой версии: метки → [подпись поля]."""
    labels = {}
    for field in report.get("fields") or []:
        labels[field.get("key")] = field.get("label") or field.get("key")
    for item in report.get("computed") or []:
        if item.get("op") == "sub":
            labels[item.get("key")] = (
                f"{labels.get(item.get('a'), item.get('a'))} {MINUS} {labels.get(item.get('b'), item.get('b'))}"
            )

    def replace(match):
        name = match.group(1)
        if name in RUNTIME_TOKENS:
            return RUNTIME_TOKENS[name]
        if name in labels:
            return "[" + str(labels[name]) + "]"
        return match.group(0)

    return TOKEN_RE.sub(replace, text)


def _md_cheatsheet(cheatsheet: dict) -> list:
    blocks = [f"## {_md_text(cheatsheet.get('title', 'Памятка на заявку'))}"]

    chain = cheatsheet.get("chain") or []
    if chain:
        blocks.append("### Цепочка по заявке")
        lines = []
        for index, item in enumerate(chain, start=1):
            lines.append(f"{index}. **{_md_text(item.get('when', ''))}**")
            if item.get("app"):
                lines.append(f"   - В приложении: «{_md_text(item['app'])}»")
            if item.get("chat"):
                lines.append(f"   - В чат: «{_md_text(item['chat'])}»")
        blocks.append("\n".join(lines))

    for report in cheatsheet.get("reports") or []:
        blocks.append(f"### {_md_text(report.get('title', ''))}")
        fields = report.get("fields") or []
        if fields:
            lines = []
            for field in fields:
                type_label = FIELD_TYPE_LABELS.get(field.get("type"))
                lines.append(f"- {_md_text(field.get('label', ''))}" + (f" — {type_label}" if type_label else ""))
            blocks.append("Заполни поля:")
            blocks.append("\n".join(lines))
        blocks.append("Готовый отчёт для чата:")
        blocks.append(_fence(_md_report_text(report.get("template", ""), report)))
        if report.get("note"):
            blocks.append(_md_report_text(report["note"], report))
    return blocks


def _md_cell(value) -> str:
    """Ячейка таблицы: переносы строк — пробелами, «|» экранируется."""
    return " ".join(str(value).split()).replace("|", "\\|")


def _md_price(kind, price, currency) -> str:
    if kind == "fixed" and isinstance(price, int):
        return f"{price} {currency}"
    if kind == "from" and isinstance(price, int):
        return f"от {price} {currency}"
    if kind == "na":
        return "недоступно"
    return "уточнять"


def _md_cell_price(cell, currency) -> str:
    if isinstance(cell, int) and not isinstance(cell, bool):
        return f"{cell} {currency}"
    if cell == ASK_CELL:
        return "уточнять"
    if isinstance(cell, dict) and cell.get("hint"):
        return _md_cell(cell["hint"])  # цену называет мастер, в таблице — подсказка из прайса
    return "—"  # null или нет ключа — услуга для этой модели недоступна


def _md_table(header, rows) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _md_pricelist(pricelist: dict) -> list:
    """Раздел «Прайс» для guide.md: таблицы позиций по группам и таблицы моделей."""
    currency = pricelist.get("currency") or "₽"
    blocks = ["## Прайс"]
    intro = f"**{_md_cell(pricelist.get('title', 'Прайс'))}**"
    if pricelist.get("note"):
        intro += " " + _md_cell(pricelist["note"])
    blocks.append(intro)

    groups: dict = {}
    for item in pricelist.get("items") or []:
        groups.setdefault(item.get("group") or "Без группы", []).append(item)
    for group, items in groups.items():
        blocks.append(f"### {_md_cell(group)}")
        blocks.append(_md_table(
            ["№", "Услуга", "Цена", "Описание"],
            [
                [
                    _md_cell(item.get("no", "")),
                    _md_cell(item.get("name", "")),
                    _md_price(item.get("kind"), item.get("price"), currency),
                    _md_cell(item.get("description", "")),
                ]
                for item in items
            ],
        ))

    for matrix in pricelist.get("matrices") or []:
        blocks.append(f"### {_md_cell(matrix.get('title', ''))}")
        if matrix.get("note"):
            blocks.append(_md_cell(matrix["note"]))
        columns = matrix.get("columns") or []
        rows = []
        for row in matrix.get("rows") or []:
            prices = row.get("prices") or {}
            rows.append(
                [_md_cell(row.get("label", ""))]
                + [_md_cell_price(prices.get(column.get("id")), currency) for column in columns]
            )
        blocks.append(_md_table(["Модель"] + [_md_cell(column.get("name", "")) for column in columns], rows))
    return blocks


def render_md(data: dict) -> str:
    """Текстовая версия гайда `guide.md` — для чата и печати.

    Этапы и шаги идут в порядке файлов контента. Внутри этапа сначала обычные
    шаги, затем под подзаголовком «Дополнительно» — шаги с optional: true.
    Если в данных есть прайс (ключ `pricelist`), в конце — раздел «Прайс».
    """
    blocks = [MD_TITLE]
    for stage in data.get("stages") or []:
        blocks.append(f"## {_md_text(stage.get('title', ''))}")
        if stage.get("intro"):
            blocks.append(_md_text(stage["intro"]))
        steps = stage.get("steps") or []
        for step in (s for s in steps if not s.get("optional")):
            blocks.extend(_md_step(step, "###"))
        optional = [s for s in steps if s.get("optional")]
        if optional:
            blocks.append("### Дополнительно")
            for step in optional:
                blocks.extend(_md_step(step, "####"))
    if isinstance(data.get("cheatsheet"), dict):
        blocks.extend(_md_cheatsheet(data["cheatsheet"]))
    if isinstance(data.get("pricelist"), dict):
        blocks.extend(_md_pricelist(data["pricelist"]))
    return "\n\n".join(blocks) + "\n"


def _embed_json(data) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # «</» закрыл бы <script>, «<!--» переключает разбор скрипта в особый режим
    return payload.replace("</", "<\\/").replace("<!--", "\\u003c!--")


def render_html(data: dict, template: str) -> str:
    """Вставляет данные гайда в шаблон вместо маркера <!--GUIDE_DATA-->."""
    found = template.count(DATA_MARKER)
    if found == 0:
        raise ContentError(f"В шаблоне страницы нет маркера {DATA_MARKER}: данные вставить некуда")
    if found > 1:
        raise ContentError(f"Маркер {DATA_MARKER} встречается в шаблоне {found} раза, а нужен один")
    block = '<script type="application/json" id="guide-data">' + _embed_json(data) + "</script>"
    return template.replace(DATA_MARKER, block, 1)


def render_techdir(template_text: str, profile: dict, strict: bool = True) -> str:
    """Записка техдиру: переменные профиля подставляются в шаблон."""
    values = profile.get("vars", {}) if isinstance(profile, dict) else {}
    unknown: set = set()
    text = _substitute(template_text, values, frozenset(), unknown)
    if strict and unknown:
        raise UnknownVarsError(unknown)
    return text


def _text_vars(text: str) -> list:
    """Переменные профиля в произвольном тексте (по алфавиту)."""
    return sorted({m.group(1) for m in TOKEN_RE.finditer(text)} - set(RUNTIME_TOKENS))


# ---------------------------------------------------------------------------
# Покрытие манифеста требований
# ---------------------------------------------------------------------------

# Относятся к продукту в целом, в reqs шагов не обязаны встречаться (interfaces.md, U3).
# G44–G46 — витрина на GitHub: README, снимки, документы проекта.
COVERAGE_EXEMPT = frozenset({
    "R01", "R02", "R03", "R04", "R75i", "G34", "G35", "G36", "G38", "G42", "G43", "G44", "G45", "G46",
})
REQUIRED_STATUSES = frozenset({"in-ticket", "done", "placeholder"})
KNOWN_STATUSES = REQUIRED_STATUSES | {"deferred", "dropped"}
# A01, A02… — требования, добавленные спецификацией; в манифесте их строк нет.
SPEC_ADDITION_RE = re.compile(r"A\d+", re.ASCII)
REQ_SORT_RE = re.compile(r"([A-Za-z]+)(\d+)(.*)", re.ASCII | re.DOTALL)


def _req_sort_key(req: str):
    match = REQ_SORT_RE.fullmatch(req)
    if match:
        return (match.group(1), int(match.group(2)), match.group(3))
    return (req, 0, "")


def parse_manifest(manifest_text: str) -> dict:
    """Строки таблицы манифеста → {id: статус}; статус None, если он не распознан.

    Статус ищется среди ячеек строки, а не по номеру колонки: в цитате из брифа
    может оказаться вертикальная черта.
    """
    statuses = {}
    for raw in manifest_text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip("|"))]
        if not cells or not REQ_ID_RE.fullmatch(cells[0]):
            continue
        status = next((c.lower() for c in cells[1:] if c.lower() in KNOWN_STATUSES), None)
        statuses[cells[0]] = status
    return statuses


def coverage(data: dict, manifest_text: str) -> dict:
    """{"missing": [...], "unknown": [...]} — непокрытые и несуществующие требования."""
    statuses = parse_manifest(manifest_text)
    referenced = set()
    for stage in data.get("stages") or []:
        for step in stage.get("steps") or []:
            reqs = step.get("reqs") if isinstance(step, dict) else None
            for req in reqs if isinstance(reqs, list) else []:
                if isinstance(req, str):
                    referenced.add(req)
    required = {req for req, status in statuses.items() if status in REQUIRED_STATUSES} - COVERAGE_EXEMPT
    missing = sorted(required - referenced, key=_req_sort_key)
    unknown = sorted(
        (req for req in referenced if req not in statuses and not SPEC_ADDITION_RE.fullmatch(req)),
        key=_req_sort_key,
    )
    return {"missing": missing, "unknown": unknown}


# ---------------------------------------------------------------------------
# Командная строка
# ---------------------------------------------------------------------------

DEFAULT_CONTENT = GUIDE_DIR / "content"
DEFAULT_PROFILE = GUIDE_DIR / "profiles" / "demo.json"
DEFAULT_PRICELIST = GUIDE_DIR / "profiles" / "demo-pricelist.json"
DEFAULT_TEMPLATE = GUIDE_DIR / "template.html"
DEFAULT_OUT = ROOT / "dist"


def default_techdir_template(root=None) -> Path:
    """Шаблон записки техдиру: guide/docs/for-techdir.md, а если его нет — образец из фикстур."""
    base = Path(root) if root is not None else ROOT
    primary = base / "guide" / "docs" / "for-techdir.md"
    if primary.is_file():
        return primary
    return base / "guide" / "tests" / "fixtures" / "for-techdir.md"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build.py",
        description=(
            "Собирает гайд из контента (JSON) и профиля компании: index.html, guide.md и for-techdir.md. "
            "Запускай из корня репозитория."
        ),
    )
    parser.add_argument("--content", metavar="DIR", help="папка с контентом (по умолчанию guide/content)")
    parser.add_argument("--profile", metavar="FILE", help="профиль компании (по умолчанию guide/profiles/demo.json)")
    parser.add_argument("--out", metavar="DIR", help="куда писать результат (по умолчанию dist)")
    parser.add_argument("--check", action="store_true", help="только проверить контент и профиль, ничего не собирать")
    parser.add_argument("--manifest", metavar="PATH", help="манифест требований: проверить покрытие шагами")
    parser.add_argument(
        "--lenient-vars", action="store_true",
        help="неизвестные переменные профиля не ошибка: напечатать список и продолжить",
    )
    parser.add_argument(
        "--list-vars", action="store_true",
        help="напечатать переменные профиля, найденные в контенте, и выйти",
    )
    parser.add_argument("--template", metavar="FILE", help="шаблон страницы (по умолчанию guide/template.html)")
    parser.add_argument(
        "--pricelist", metavar="FILE",
        help="прайс для панели «Прайс и акт» (по умолчанию guide/profiles/demo-pricelist.json, если файл есть)",
    )
    parser.add_argument(
        "--no-pricelist", action="store_true",
        help="собрать без прайса, даже если демо-прайс по умолчанию на месте",
    )
    parser.add_argument(
        "--techdir-template", metavar="FILE",
        help="шаблон записки техдиру (по умолчанию guide/docs/for-techdir.md, а если его нет — образец из tests/fixtures)",
    )
    return parser


def _error(message: str) -> None:
    print(message, file=sys.stderr)


def _read_text(path: Path, what: str) -> str:
    if not path.is_file():
        raise ContentError(f"{what} не найден: {path}")
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ContentError(f"{path.name}: файл не в кодировке UTF-8 ({exc.reason})") from exc


def _run(args) -> int:
    content_dir = Path(args.content) if args.content else DEFAULT_CONTENT
    data = load_content(str(content_dir))
    content_vars = collect_vars(data)

    if args.list_vars:
        for name in content_vars:
            print(name)
        return 0

    profile_path = Path(args.profile) if args.profile else DEFAULT_PROFILE
    profile = load_profile(str(profile_path))

    if args.pricelist and args.no_pricelist:
        raise ContentError("Флаги --pricelist и --no-pricelist вместе не работают: оставь один")
    pricelist = None
    if args.pricelist:
        pricelist = load_pricelist(args.pricelist)
    elif not args.no_pricelist and Path(DEFAULT_PRICELIST).is_file():
        pricelist = load_pricelist(str(DEFAULT_PRICELIST))

    if args.techdir_template:
        techdir_path = Path(args.techdir_template)
    else:
        techdir_path = default_techdir_template()
        if techdir_path != ROOT / "guide" / "docs" / "for-techdir.md":
            print(f"Записка техдиру: guide/docs/for-techdir.md не найден, взят образец {techdir_path}")
    techdir_text = _read_text(techdir_path, "Шаблон записки техдиру")
    techdir_vars = _text_vars(techdir_text)

    errors = []
    known = set(profile["vars"])
    unknown = sorted((set(content_vars) | set(techdir_vars)) - known)
    if unknown and args.lenient_vars:
        print("Неизвестные переменные профиля (разрешено флагом --lenient-vars, метки останутся в тексте):")
        for name in unknown:
            print(f"  {name}")
    else:
        for name in unknown:
            where = "в контенте" if name in content_vars else "в записке техдиру"
            errors.append(f"Переменной «{name}» нет в профиле «{profile['name']}» ({where} есть метка {{{{{name}}}}})")

    extra = sorted(known - set(content_vars) - set(techdir_vars))
    if extra:
        print("Предупреждение: в профиле есть переменные, которых нет в контенте: " + ", ".join(extra))

    resolved = apply_profile(data, profile, strict=False)
    if pricelist is not None:
        resolved["pricelist"] = pricelist
    errors.extend(validate(resolved))

    if args.manifest:
        manifest_text = _read_text(Path(args.manifest), "Манифест")
        result = coverage(resolved, manifest_text)
        print("Непокрытые требования манифеста: " + (", ".join(result["missing"]) or "нет"))
        print("Неизвестные идентификаторы в reqs: " + (", ".join(result["unknown"]) or "нет"))
        if result["missing"]:
            errors.append("Не все требования манифеста есть в reqs шагов: " + ", ".join(result["missing"]))
        if result["unknown"]:
            errors.append("В reqs шагов есть идентификаторы, которых нет в манифесте: " + ", ".join(result["unknown"]))

    if errors:
        _error(f"Найдено проблем: {len(errors)}")
        for message in errors:
            _error(f"  - {message}")
        return 1

    step_count = sum(len(stage["steps"]) for stage in resolved["stages"])
    if pricelist is not None:
        print(f"Прайс: позиций {len(pricelist['items'])}, матриц {len(pricelist.get('matrices') or [])}.")
    if args.check:
        print(f"Проверка пройдена: этапов {len(resolved['stages'])}, шагов {step_count}.")
        return 0

    template_path = Path(args.template) if args.template else DEFAULT_TEMPLATE
    template = _read_text(template_path, "Шаблон страницы")
    # сначала всё рендерим, потом пишем: при ошибке в выходной папке ничего не меняется
    outputs = {
        "index.html": render_html(resolved, template),
        "guide.md": render_md(resolved),
        "for-techdir.md": render_techdir(techdir_text, profile, strict=not args.lenient_vars),
    }
    out_dir = Path(args.out) if args.out else DEFAULT_OUT
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, text in outputs.items():
        (out_dir / name).write_text(text, encoding="utf-8", newline="\n")
    print(f"Собрано в {out_dir}: " + ", ".join(outputs) + f" (этапов {len(resolved['stages'])}, шагов {step_count}).")
    return 0


def main(argv=None) -> int:
    """Точка входа командной строки; возвращает код возврата процесса."""
    args = _build_parser().parse_args(argv)
    try:
        return _run(args)
    except (ContentError, OSError) as exc:
        _error(f"Ошибка: {exc}")
        return 1


def _use_utf8_streams() -> None:
    """Русский текст в консоли и в пайпах Windows не должен падать на кодировке."""
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
