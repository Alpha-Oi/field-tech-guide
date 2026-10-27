"""График «Шагов по этапам» для README: читает guide/content/*.json и пишет SVG.

    python tools/make_charts.py            записать docs/img/steps-by-stage.svg
    python tools/make_charts.py --check    код 1, если файл в репозитории не совпадает с тем,
                                           что получится из контента (устарел или правился руками)

Только стандартная библиотека. Картинка рисуется на сплошной светлой подложке с явными цветами:
так подписи и столбцы читаются и в светлой, и в тёмной теме GitHub, где SVG показывается как
обычная картинка и стилей страницы не видит.
"""

import argparse
import json
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTENT = ROOT / "guide" / "content"
DEFAULT_OUT = ROOT / "docs" / "img" / "steps-by-stage.svg"
HOW_TO_UPDATE = "python tools/make_charts.py"

# Цвета проверены по контрасту WCAG на подложке: текст не ниже 4.5:1, столбцы не ниже 3:1.
BACKGROUND = "#ffffff"
BORDER = "#d0d6df"
TEXT = "#151a21"
MUTED = "#4b5565"
REQUIRED_FILL = "#1d4ed8"
OPTIONAL_FILL = "#b45309"

FONT = "system-ui, -apple-system, 'Segoe UI', Roboto, Arial, sans-serif"
PAD = 24
UNIT = 20  # пикселей на один шаг
BAR_HEIGHT = 20
ROW_HEIGHT = 34
LABEL_CHAR_WIDTH = 8  # запас на букву: подпись выравнивается по правому краю и не должна выйти за левый
COUNT_AREA = 120  # место под число справа от столбца
HEADER_HEIGHT = 124


class ChartError(ValueError):
    """Контент нельзя превратить в график; текст ошибки — для человека."""


def plural(number, one, few, many):
    """Русское окончание по числу: 1 шаг, 3 шага, 11 шагов, 21 шаг."""
    tail = abs(number) % 100
    if 11 <= tail <= 19:
        return many
    last = tail % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def load_stages(content_dir):
    """Этапы из файлов контента по порядку имён: [{id, title, required, optional}, ...].

    Файлы без блока `stage` (например, памятка) пропускаются.
    """
    stages = []
    for path in sorted(Path(content_dir).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ChartError(f"{path.name}: не удалось прочитать файл ({exc})") from None
        if not isinstance(data, dict) or "stage" not in data:
            continue
        try:
            stage = data["stage"]
            steps = data.get("steps", [])
            optional = sum(1 for item in steps if item.get("optional") is True)
            stages.append({
                "id": str(stage["id"]),
                "title": str(stage["title"]),
                "required": len(steps) - optional,
                "optional": optional,
            })
        except (KeyError, TypeError, AttributeError):
            raise ChartError(f"{path.name}: у этапа нет id или title, либо steps — не список объектов") from None
    if not stages:
        raise ChartError(f"в папке {content_dir} нет ни одного файла с этапом")
    return stages


def attr(value):
    return escape(str(value), {'"': "&quot;"})


def number(value):
    return str(int(value)) if float(value) == int(value) else f"{value:.2f}"


def render_svg(stages):
    """SVG-текст графика (с переводом строки в конце, без \\r): результат один и тот же при каждом запуске."""
    total_required = sum(item["required"] for item in stages)
    total_optional = sum(item["optional"] for item in stages)
    total = total_required + total_optional
    longest = max(len(item["title"]) for item in stages)
    label_x = PAD + longest * LABEL_CHAR_WIDTH
    bar_x = label_x + 16
    longest_bar = max(item["required"] + item["optional"] for item in stages)
    width = bar_x + longest_bar * UNIT + COUNT_AREA
    height = HEADER_HEIGHT + len(stages) * ROW_HEIGHT + PAD

    summary = f"Всего {total} {plural(total, 'шаг', 'шага', 'шагов')}"
    if total_optional:
        summary += f", из них {total_optional} {plural(total_optional, 'дополнительный', 'дополнительных', 'дополнительных')}"
    heading = "Шагов по этапам"
    desc = (f"Горизонтальный столбчатый график: число шагов в каждом из {len(stages)} этапов гайда. "
            f"{summary}.")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"'
        f' role="img" aria-labelledby="chart-title chart-desc" font-family="{attr(FONT)}">',
        f'<title id="chart-title">{escape(heading)}</title>',
        f'<desc id="chart-desc">{escape(desc)}</desc>',
        f'<rect class="bg" x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="12" fill="{BACKGROUND}" stroke="{BORDER}"/>',
        f'<text class="heading" x="{PAD}" y="44" font-size="22" font-weight="700" fill="{TEXT}">{escape(heading)}</text>',
        f'<text class="summary" x="{PAD}" y="70" font-size="15" fill="{MUTED}">{escape(summary)}</text>',
        f'<rect class="swatch req" x="{PAD}" y="86" width="14" height="14" fill="{REQUIRED_FILL}"/>',
        f'<text class="legend" x="{PAD + 22}" y="98" font-size="14" fill="{TEXT}">Обязательные шаги</text>',
    ]
    if total_optional:
        legend_x = PAD + 22 + 190
        lines += [
            f'<rect class="swatch opt" x="{legend_x}" y="86" width="14" height="14" fill="{OPTIONAL_FILL}"/>',
            f'<text class="legend" x="{legend_x + 22}" y="98" font-size="14" fill="{TEXT}">'
            "Дополнительно (не нужны для первой заявки)</text>",
        ]

    for index, item in enumerate(stages):
        top = HEADER_HEIGHT + index * ROW_HEIGHT
        middle = top + BAR_HEIGHT // 2 + 5  # базовая линия текста по центру столбца
        stage_id = attr(item["id"])
        lines.append(f'<g class="bar" data-stage="{stage_id}">')
        lines.append(
            f'<text class="label" x="{label_x}" y="{middle}" text-anchor="end" font-size="15" fill="{TEXT}">'
            f'{escape(item["title"])}</text>'
        )
        cursor = bar_x
        for kind, steps, color in (("req", item["required"], REQUIRED_FILL), ("opt", item["optional"], OPTIONAL_FILL)):
            if not steps:
                continue
            length = steps * UNIT
            lines.append(
                f'<rect class="seg {kind}" data-stage="{stage_id}" data-steps="{steps}" '
                f'x="{number(cursor)}" y="{top}" width="{number(length)}" height="{BAR_HEIGHT}" fill="{color}"/>'
            )
            cursor += length
        count = str(item["required"] + item["optional"])
        if item["optional"]:
            count += f" ({item['optional']} доп.)"
        lines.append(
            f'<text class="count" x="{number(cursor + 8)}" y="{middle}" font-size="15" font-weight="700" fill="{TEXT}">'
            f"{escape(count)}</text>"
        )
        lines.append("</g>")
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def shown(path):
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="График «Шагов по этапам» из guide/content/*.json.")
    parser.add_argument("--check", action="store_true", help="ничего не писать: код 1, если файл графика устарел")
    parser.add_argument("--content", default=str(DEFAULT_CONTENT), help="папка с файлами этапов (guide/content)")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="куда писать SVG (docs/img/steps-by-stage.svg)")
    args = parser.parse_args(argv)
    out = Path(args.out)

    try:
        wanted = render_svg(load_stages(args.content)).encode("utf-8")
    except ChartError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2

    if args.check:
        if not out.is_file():
            print(f"Проверка не пройдена: нет файла {shown(out)}. Запусти {HOW_TO_UPDATE}")
            return 1
        # git на Windows мог превратить переводы строк в CRLF: на сравнение это не влияет
        if out.read_bytes().replace(b"\r\n", b"\n") != wanted:
            print(f"Проверка не пройдена: график {shown(out)} устарел. Запусти {HOW_TO_UPDATE} и закоммить файл")
            return 1
        print(f"График актуален: {shown(out)}")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(wanted)
    print(f"Записан {shown(out)}")
    return 0


def _use_utf8_streams():
    """Русский текст в консоли и в пайпах Windows идёт в UTF-8, какая бы ни была кодировка консоли."""
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
