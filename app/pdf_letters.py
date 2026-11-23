"""Сборка персональных PDF из шаблона с {{полями}} или из HTML-письма."""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf


ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "data" / "fonts"
FONT_CANDIDATES = [
    FONT_DIR / "TimesNewRoman.ttf",
    Path("/System/Library/Fonts/Supplemental/Times New Roman.ttf"),
    Path("/Library/Fonts/Times New Roman.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
    Path("/Library/Fonts/Arial.ttf"),
]
FONT_BOLD_CANDIDATES = [
    FONT_DIR / "TimesNewRoman-Bold.ttf",
    Path("/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf"),
    Path("/Library/Fonts/Times New Roman Bold.ttf"),
    Path("/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    Path("/Library/Fonts/Arial Bold.ttf"),
]

# Поля, которые подставляются в шаблон.
PLACEHOLDERS = [
    ("school", "Название школы / центра"),
    ("city", "Город с «г.» — например: г. Москва"),
    ("address", "Почтовый адрес"),
    ("email", "Электронная почта получателя"),
    ("phone", "Телефон"),
    ("director", "ФИО директора / контакта"),
    ("addressee", "Кому в шапке — Е. Ш. Ямбургу"),
    ("role_line", "Строка роли — Директору ГБОУ Школа № 109"),
    ("greeting", "Обращение — Уважаемый Евгений Шоломович,"),
    ("date", "Дата письма — «16» декабря 2025 г."),
    ("region", "Регион"),
    ("teams", "Команды через запятую"),
]

PLACEHOLDER_HELP = """
Рекомендуемый способ: режим «Текст на сайте» — поля {{…}} подставляются с нормальным переносом строк.

Режим «PDF-шаблон» подходит только если каждое поле стоит отдельно (в шапке), а не внутри абзаца.
Длинная подстановка внутрь строки в PDF наезжает на соседний текст — PDF не умеет переносить абзац.

Как разметить текст на сайте или PDF
1. В нужных местах впишите поле латиницей в двойных фигурных скобках.
2. Для PDF: экспорт текстом (не картинкой).
3. Примеры:
  {{city}}        →  г. Москва
  {{date}}        →  «16» декабря 2025 г.
  {{addressee}}   →  Е. Ш. Ямбургу
  {{role_line}}   →  Директору ГБОУ Школа № 109
  {{greeting}}    →  Уважаемый Евгений Шоломович,
  {{school}}      →  название школы

Важно: {{school}}, не {school}. Имя поля — латиница, цифры и _.
""".strip()

NBSP = "&nbsp;"  # сущность HTML — Story надёжнее держит неразрывность, чем U+00A0


# Контакты в шапке и подписи (полные имя + отчество)
CONTACT_NIKITA = f"Никита{NBSP}Евгеньевич{NBSP}Карпенко-Черников"
CONTACT_ALEX = f"Александр{NBSP}Алексеевич{NBSP}Зинкевич"
PHONE_NIKITA = f"+7{NBSP}(923){NBSP}147-42-44"
PHONE_ALEX = f"+7{NBSP}(913){NBSP}899-27-59"
MAIL_NIKITA = "chernikov.nikita@gmail.com"

BRAND_HEADER = f"""
<header class="brand">
  <p class="brand-title">Открытый Всероссийский Турнир Юных Физиков</p>
  <div class="rule"></div>
  <p class="brand-logos"><img src="emblem-institutes.png" width="455" height="63" /></p>
  <table class="meta" width="100%">
    <tr>
      <td class="meta-left" width="38%" valign="top">
        rosypt.ru<br/>
        t.me/open_RYPT<br/>
        vk.com/open_rypt
      </td>
      <td class="meta-right" width="62%" valign="top">
        {CONTACT_NIKITA}<br/>
        {PHONE_NIKITA}, {MAIL_NIKITA}<br/>
        {CONTACT_ALEX}<br/>
        {PHONE_ALEX}
      </td>
    </tr>
  </table>
  <div class="rule rule-soft"></div>
</header>
""".strip()

DEFAULT_LETTER_HTML = f"""
<section class="letter">
  {BRAND_HEADER}

  <table class="head" width="100%">
    <tr>
      <td class="head-left" valign="top" style="width:130px;">
        {{{{city}}}}<br/>
        {{{{date}}}}
      </td>
      <td class="head-right" valign="top">
        {{{{addressee}}}}<br/>
        {{{{role_line}}}}<br/>
        {{{{address}}}}<br/>
        {{{{email}}}}
      </td>
    </tr>
  </table>

  <p class="greet">{{{{greeting}}}}</p>

  <p>
    Всероссийский оргкомитет Открытого Всероссийского турнира юных физиков
    приглашает команды <b>из{NBSP}{{{{school}}}}</b> принять участие
    <b>в{NBSP}XLIX{NBSP}Открытом Всероссийском турнире юных физиков</b>,
    который состоится <b>с{NBSP}15{NBSP}по{NBSP}21{NBSP}марта 2027{NBSP}года</b>
    <b>в{NBSP}Москве</b>.
  </p>
  <p>
    Турнир <b>открытый</b>: в{NBSP}нём может принять участие любая команда.
    Отборочные физические бои планируется провести в{NBSP}помещениях
    <b>СУНЦ{NBSP}МГУ</b>; церемония открытия, финальный физический бой
    и{NBSP}церемония закрытия — <b>на{NBSP}Физическом факультете МГУ</b>.
  </p>
  <p>
    Турнир юных физиков (<b>ТЮФ</b>) — ежегодное командное состязание школьников
    старших классов в{NBSP}умении решать сложные исследовательские задачи,
    убедительно представлять результаты своей работы и{NBSP}отстаивать их
    в{NBSP}научной дискуссии — физическом бое. Турнир даёт возможность
    в{NBSP}полной мере проявить творческие способности, решая задания, которых
    нет в{NBSP}школьных задачниках или на{NBSP}предметных олимпиадах.
  </p>
  <p>
    В{NBSP}команде, включая капитана, обычно участвует 4–5 школьников;
    команду сопровождает один или два научных руководителя. Участие для{NBSP}команд,
    прошедших регистрацию, <b>бесплатное</b>. Оргкомитет дополнительно приглашает
    независимых членов жюри.
  </p>
  <p>
    Просим рассмотреть возможность формирования школьной команды и{NBSP}пройти
    предварительную регистрацию:<br/>
    <b>https://forms.gle/eGmvdaaVeoZREoPH9</b><br/>
    Регламент турнира: <b>https://rosypt.ru/rules/</b>
  </p>
  <p>
    Вскоре будет опубликовано второе информационное письмо: в{NBSP}нём мы укажем
    расписание турнира, варианты размещения команд и{NBSP}другую полезную
    организационную информацию. Руководители команд, прошедших
    предрегистрацию, будут получать эти материалы на{NBSP}почту.
  </p>
  <p class="flush">
    На{NBSP}вопросы о{NBSP}регистрации, подготовке и{NBSP}организации поездки ответят:
  </p>
  <p class="contact-line">
    <b>{CONTACT_NIKITA}</b><br/>
    {PHONE_NIKITA}, {MAIL_NIKITA}
  </p>
  <p class="contact-line">
    <b>{CONTACT_ALEX}</b><br/>
    {PHONE_ALEX}
  </p>
  <p class="flush">
    Будем благодарны, если Вы направите информацию о{NBSP}турнире
    заинтересованным преподавателям и{NBSP}учащимся.
  </p>
  <p>
    <b>Приложения:</b> 1){NBSP}первое информационное письмо от{NBSP}8{NBSP}октября
    2026{NBSP}г.; 2){NBSP}задания Российского и{NBSP}Международного ТЮФ 2027{NBSP}г.
  </p>

  <p class="sign">С{NBSP}уважением,</p>
  <table class="signers" width="100%">
    <tr>
      <td width="50%" valign="top">
        <b>{CONTACT_NIKITA}</b><br/>
        Президент Открытого Всероссийского<br/>турнира юных физиков
      </td>
      <td width="50%" valign="top">
        <b>{CONTACT_ALEX}</b><br/>
        Руководитель методической комиссии
      </td>
    </tr>
  </table>
</section>
""".strip()

INFO_LETTER_HTML = f"""
<section class="letter">
  {BRAND_HEADER}

  <p class="doc-title">XLIX Открытый Всероссийский турнир юных физиков</p>
  <p class="doc-sub">Первое информационное письмо · 8 октября 2026 г.</p>

  <p>
    Оргкомитет Открытого Всероссийского турнира юных физиков приглашает команды
    школьников и{NBSP}их научных руководителей принять участие в{NBSP}XLIX Открытом
    Всероссийском турнире юных физиков, который состоится
    <b>15–21{NBSP}марта 2027{NBSP}года</b> в{NBSP}<b>Москве</b>.
  </p>
  <p>
    Турнир юных физиков (<b>ТЮФ</b>) — ежегодное командное состязание школьников
    в{NBSP}умении решать сложные исследовательские задачи, представлять результаты
    своей работы и{NBSP}отстаивать их в{NBSP}научной дискуссии — физическом бое.
    Турнир <b>открытый</b>: в{NBSP}нём может принять участие любая команда.
  </p>
  <p>
    <b>Сроки и{NBSP}площадки.</b> 15–21{NBSP}марта 2027{NBSP}года, Москва.
    Отборочные физические бои — в{NBSP}помещениях <b>СУНЦ{NBSP}МГУ</b>; открытие,
    финальный бой и{NBSP}закрытие — на{NBSP}<b>Физическом факультете МГУ</b>.
  </p>
  <p>
    <b>Предварительная регистрация.</b>
    <b>https://forms.gle/eGmvdaaVeoZREoPH9</b><br/>
    Руководители команд, прошедших предрегистрацию, будут получать организационную
    информацию на{NBSP}почту.
  </p>
  <p>
    <b>Второе информационное письмо</b> будет опубликовано вскоре: в{NBSP}нём мы
    укажем расписание турнира, варианты размещения команд и{NBSP}другую полезную
    организационную информацию.
  </p>
  <p>
    <b>Контакты.</b> {CONTACT_NIKITA} — {MAIL_NIKITA}, {PHONE_NIKITA};
    {CONTACT_ALEX} — {PHONE_ALEX}.
  </p>

  <p class="sign">С{NBSP}уважением,<br/>Оргкомитет Открытого Всероссийского турнира юных физиков</p>
</section>
""".strip()


def _font_path(candidates: list[Path]) -> str | None:
    for path in candidates:
        if path.exists():
            return str(path)
    return None


def fontfile() -> str:
    path = _font_path(FONT_CANDIDATES)
    if not path:
        raise RuntimeError("Не найден шрифт с кириллицей (Times New Roman / Arial).")
    return path


EMBLEM_DIR = ROOT / "data" / "emblems"
EMBLEM_INSTITUTES = EMBLEM_DIR / "institutes_row.png"


def letter_archive() -> pymupdf.Archive:
    """Шрифты и эмблемы для Story: в CSS/HTML — короткие имена из архива."""
    arch = pymupdf.Archive()
    regular = fontfile()
    bold = _font_path(FONT_BOLD_CANDIDATES) or regular
    arch.add(regular, "letter-regular.ttf")
    arch.add(bold, "letter-bold.ttf")
    if EMBLEM_INSTITUTES.exists():
        arch.add(str(EMBLEM_INSTITUTES), "emblem-institutes.png")
    return arch


def letter_css() -> str:
    return """
@font-face { font-family: LetterFace; src: url(letter-regular.ttf); }
@font-face { font-family: LetterFace; src: url(letter-bold.ttf); font-weight: bold; }
body {
  font-family: LetterFace, times, serif;
  font-size: 13pt;
  line-height: 1.38;
  color: #111;
}
.letter { padding: 0; }
table.meta, table.head, table.signers {
  width: 100%;
  border-collapse: collapse;
  font-size: 13pt;
  line-height: 1.38;
}
table.meta td, table.head td, table.signers td {
  font-size: 13pt;
  line-height: 1.38;
  vertical-align: top;
  padding: 0;
}
.brand-title {
  font-size: 14pt;
  font-weight: bold;
  text-align: center;
  margin: 0 0 2px;
}
.brand-logos { text-align: center; margin: 5px 0 6px; }
.rule {
  border-top: 1.15px solid #222;
  margin: 3px 0 5px;
}
.rule-soft { margin: 6px 0 0; }
.meta-left { text-align: left; padding-right: 12px; padding-top: 2px; line-height: 1.35; font-size: 11pt; }
.meta-right { text-align: right; padding-left: 12px; padding-top: 2px; line-height: 1.35; font-size: 11pt; }
b, strong { font-weight: bold; }
table.head { margin: 16px 0 14px; }
.head-left {
  text-align: left;
  padding-right: 32px;
  font-size: 12pt;
  line-height: 1.55;
  width: 130px;
}
.head-right {
  text-align: left;
  padding-left: 32px;
  font-size: 12pt;
  line-height: 1.55;
}
.greet { margin: 0 0 10px; text-align: left; }
.flush { text-align: left; }
.contact-line {
  text-align: left;
  margin: 0 0 10px;
  line-height: 1.4;
}
.doc-title {
  font-size: 13pt;
  font-weight: bold;
  text-align: center;
  margin: 12px 0 2px;
}
.doc-sub {
  font-size: 13pt;
  text-align: center;
  margin: 0 0 10px;
}
.sign { margin-top: 14px; text-align: left; }
table.signers { margin-top: 8px; }
p { margin: 0 0 8px; text-align: justify; font-size: 13pt; }
""".strip()


def list_placeholders(text: str) -> list[str]:
    return sorted(set(re.findall(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", text or "")))


def _nbsp_phone(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    value = re.sub(r"\+7\s+", "+7&nbsp;", value)
    value = re.sub(r"(\+7(?:&nbsp;|\u00a0)?)\((\d{3})\)\s*", r"\1(\2)&nbsp;", value)
    return value


def _nbsp_name(value: str) -> str:
    """Неразрывные пробелы между частями ФИО / обращения."""
    parts = [p for p in re.split(r"\s+", (value or "").strip()) if p]
    return NBSP.join(parts)


def typography_fill(value: str) -> str:
    """Телефоны и короткие имена — с неразрывными пробелами."""
    text = value or ""
    if "@" in text and " " not in text.strip():
        return text
    if re.search(r"\+7|\d{3}[-–]\d{2}", text):
        return _nbsp_phone(text)
    if len(text.split()) <= 5 and re.search(r"[А-ЯЁа-яё]", text):
        return _nbsp_name(text)
    return text


def format_letter_date(value: str) -> str:
    """Дата в две строки, чтобы не прижималась к блоку «кому»."""
    text = (value or "").strip()
    match = re.match(r"^(«?\d+»?\s+\S+)\s+(\d{4}\s*г\.?)$", text, flags=re.IGNORECASE)
    if match:
        left = match.group(1).replace(" ", "&nbsp;")
        right = match.group(2).replace(" ", "&nbsp;")
        return f"{left}<br/>{right}"
    return typography_fill(text)


def render_text(template: str, data: dict | None) -> str:
    """Если data is None — оставляем {{поля}} как есть (шаблон)."""
    if data is None:
        return template or ""

    def repl(match):
        key = match.group(1)
        raw = str(data.get(key, "") or "")
        if key == "date":
            return format_letter_date(raw)
        if key in {"school", "address", "role_line"}:
            return raw
        if key in {"greeting", "addressee", "director", "city", "phone"}:
            return typography_fill(raw)
        return raw

    return re.sub(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", repl, template or "")


def _fill_pdf_placeholders(src: Path, dst: Path, data: dict) -> list[str]:
    """Подставляет {{field}} в PDF. Возвращает список полей, которых не нашли."""
    doc = pymupdf.open(src)
    font = pymupdf.Font(fontfile=fontfile())
    found = set()
    try:
        for page in doc:
            jobs = []
            for key in sorted(data.keys(), key=len, reverse=True):
                for needle in (f"{{{{{key}}}}}", f"{{{{ {key} }}}}"):
                    for rect in page.search_for(needle):
                        jobs.append((pymupdf.Rect(rect), str(data.get(key) or "—")))
                        found.add(key)
            if not jobs:
                continue
            for rect, _ in jobs:
                page.add_redact_annot(rect, fill=(1, 1, 1))
            page.apply_redactions(images=0)
            tw = pymupdf.TextWriter(page.rect)
            for rect, value in jobs:
                point = pymupdf.Point(rect.x0, rect.y1 - 1)
                fontsize = 12 if rect.height < 1 else min(14, max(10, rect.height * 0.9))
                tw.append(point, value, font=font, fontsize=fontsize)
            tw.write_text(page)
        dst.parent.mkdir(parents=True, exist_ok=True)
        doc.save(dst, garbage=4, deflate=True)
    finally:
        doc.close()
    probe = pymupdf.open(src)
    try:
        blob = "\n".join(page.get_text() for page in probe)
    finally:
        probe.close()
    expected = set(list_placeholders(blob))
    return sorted(expected - found)


_HANGING = (
    "или",
    "для",
    "при",
    "над",
    "под",
    "без",
    "из",
    "от",
    "до",
    "по",
    "со",
    "об",
    "ко",
    "во",
    "на",
    "не",
    "ни",
    "но",
    "то",
    "а",
    "и",
    "с",
    "к",
    "у",
    "о",
    "в",
)


def fix_hanging_prepositions(html: str) -> str:
    """Неразрывный пробел после предлогов/союзов в текстовых узлах HTML."""
    parts = re.split(r"(<[^>]+>)", html or "")
    out: list[str] = []
    for part in parts:
        if part.startswith("<"):
            out.append(part)
            continue
        for word in _HANGING:
            part = re.sub(
                rf"(?<![А-Яа-яЁёA-Za-z0-9])({word}) +(?=[А-Яа-яЁёA-Za-z0-9«\"(+])",
                rf"\1{NBSP}",
                part,
                flags=re.IGNORECASE,
            )
        out.append(part)
    return "".join(out)


def html_to_pdf(html_body: str, dst: Path, data: dict | None) -> None:
    filled = fix_hanging_prepositions(render_text(html_body, data))
    html = f"<html><head><style>{letter_css()}</style></head><body>{filled}</body></html>"
    tmp = dst.with_suffix(".tmp.pdf")
    writer = pymupdf.DocumentWriter(str(tmp))
    story = pymupdf.Story(html=html, archive=letter_archive())
    mediabox = pymupdf.paper_rect("a4")
    where = mediabox + (50, 42, -50, -42)
    more = True
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(device)
        writer.end_page()
    writer.close()
    tmp.replace(dst)


def append_pdf(base: Path, annex: Path | None, dst: Path, annex_from_page: int = 0) -> None:
    out = pymupdf.open()
    try:
        first = pymupdf.open(base)
        out.insert_pdf(first)
        first.close()
        if annex and annex.exists():
            extra = pymupdf.open(annex)
            if annex_from_page < extra.page_count:
                out.insert_pdf(extra, from_page=annex_from_page)
            extra.close()
        dst.parent.mkdir(parents=True, exist_ok=True)
        out.save(dst, garbage=4, deflate=True)
    finally:
        out.close()


def merge_pdfs(parts: list[Path], dst: Path) -> None:
    out = pymupdf.open()
    try:
        for part in parts:
            if not part or not part.exists():
                continue
            src = pymupdf.open(part)
            out.insert_pdf(src)
            src.close()
        dst.parent.mkdir(parents=True, exist_ok=True)
        out.save(dst, garbage=4, deflate=True)
    finally:
        out.close()


def build_info_and_tasks_annex(*, info_html: str, tasks_pdf: Path | None, dst: Path) -> Path:
    """Инфописьмо в общем стиле + страница заданий без общей шапки."""
    info = dst.with_name(dst.stem + ".info.pdf")
    html_to_pdf(info_html or INFO_LETTER_HTML, info, data=None)
    parts = [info]
    if tasks_pdf and tasks_pdf.exists():
        tasks_only = dst.with_name(dst.stem + ".tasks.pdf")
        src = pymupdf.open(tasks_pdf)
        try:
            out = pymupdf.open()
            if src.page_count >= 2:
                # комбинированный PDF: 1-я стр. — чужое инфо, задания со 2-й
                out.insert_pdf(src, from_page=1)
            else:
                out.insert_pdf(src)
            if out.page_count:
                out.save(tasks_only, garbage=4, deflate=True)
                parts.append(tasks_only)
            out.close()
        finally:
            src.close()
    merge_pdfs(parts, dst)
    info.unlink(missing_ok=True)
    tasks_only = dst.with_name(dst.stem + ".tasks.pdf")
    tasks_only.unlink(missing_ok=True)
    return dst


def build_letter_pdf(
    *,
    dst: Path,
    data: dict | None,
    mode: str,
    html_template: str,
    template_pdf: Path | None,
    annex_pdf: Path | None,
) -> dict:
    """
    mode: 'html' | 'pdf'
    data=None — оставить {{поля}} (чистый шаблон).
    Возвращает {ok, missing, path}.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    # PDF-подстановка не умеет раздвигать абзац — для {{school}} в тексте всегда HTML
    html_body = (html_template or "").strip() or DEFAULT_LETTER_HTML
    if mode == "pdf" and data is not None and "{{school}}" in html_body:
        mode = "html"

    if mode == "pdf":
        if not template_pdf or not template_pdf.exists():
            return {"ok": False, "missing": [], "error": "Нет PDF-шаблона"}
        if data is None:
            # шаблон как есть
            dst.write_bytes(template_pdf.read_bytes())
            return {"ok": True, "missing": [], "path": dst, "error": ""}
        missing = _fill_pdf_placeholders(template_pdf, dst, data)
        return {"ok": True, "missing": missing, "path": dst, "error": ""}

    if annex_pdf and annex_pdf.exists():
        letter = dst.with_name(dst.stem + ".letter.pdf")
        html_to_pdf(html_body, letter, data)
        append_pdf(letter, annex_pdf, dst, annex_from_page=0)
        letter.unlink(missing_ok=True)
    else:
        html_to_pdf(html_body, dst, data)
    return {"ok": True, "missing": [], "path": dst, "error": ""}


# Обратная совместимость для импортов, ожидающих LETTER_CSS
LETTER_CSS = ""  # заполняется лениво через letter_css()
