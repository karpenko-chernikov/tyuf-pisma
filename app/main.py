import zlib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app import db
from app import mailings
from app import outbound
from app import pdf_letters

db.init()

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=db.os.environ.get("SECRET_KEY", "tyuf-pisma-local"))
app.mount("/static", StaticFiles(directory=str(db.ROOT / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(db.ROOT / "app" / "templates"))


def current_user(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.user_by_id(user_id)


def page(request, name, **context):
    user = current_user(request)
    if user is None and name != "login.html":
        return RedirectResponse("/login", status_code=303)
    context.setdefault("user", user)
    context.setdefault("title", "ТЮФ")
    return templates.TemplateResponse(request, name, context)


def ru_count(number, one, few, many):
    tail = abs(number) % 100
    if tail % 10 == 1 and tail != 11:
        word = one
    elif 2 <= tail % 10 <= 4 and not 12 <= tail <= 14:
        word = few
    else:
        word = many
    return f"{number} {word}"


@app.get("/login")
def login_form(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return page(request, "login.html", title="Вход", error="")


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    user = db.user_by_name(str(form.get("name") or ""))
    if user is None or not db.check_password(str(form.get("password") or ""), user["password"]):
        return page(request, "login.html", title="Вход", error="Неверное имя или пароль.")
    request.session["user_id"] = user["id"]
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# Twenty reusable tones. A region keeps its number; past the twentieth,
# a new region reuses a tone and still appears under its own name.
REGION_TONE = {
    "Волгоградская область": 0,
    "Новосибирская область": 1,
    "Иркутская область": 2,
    "Калининградская область": 3,
    "Москва": 4,
    "Санкт-Петербург": 5,
    "Республика Бурятия": 6,
    "Воронежская область": 7,
    "Беларусь": 8,
    "Свердловская область": 9,
    "Республика Башкортостан": 10,
    "Казахстан": 11,
    "Кемеровская область — Кузбасс": 12,
    "Ленинградская область": 13,
    "Московская область": 14,
    "Республика Крым": 15,
    "Республика Саха (Якутия)": 16,
    "Республика Татарстан": 17,
    "Ростовская область": 18,
    "Томская область": 19,
    "Ульяновская область": 11,
    "Челябинская область": 14,
    "Калужская область": 18,
}
TONE_COUNT = 20
EMPTY_PLACE = "__none__"


def tone_for(region):
    if region in REGION_TONE:
        return REGION_TONE[region]
    return zlib.crc32(region.encode("utf-8")) % TONE_COUNT


def paint(row):
    region = row.get("region") or ""
    if not region:
        row["tone"] = "missing"
        row["place_label"] = "Без региона"
    else:
        row["tone"] = str(tone_for(region))
        row["place_label"] = region
    return row


def book_dataset(
    kind: str,
    q: str = "",
    place: str = "",
    team: str = "",
    *,
    exclude_ids: set | None = None,
    dedupe_centers: bool = False,
):
    """Те же фильтры, что на странице школ/центров."""
    kind = kind if kind in {db.KIND_SCHOOL, db.KIND_CENTER} else db.KIND_SCHOOL
    q = (q or "").strip()
    team = team if team in {"yes", "no"} and kind == db.KIND_SCHOOL else ""
    rows = db.schools(q) if kind == db.KIND_SCHOOL else db.centers(q)
    if kind == db.KIND_CENTER and dedupe_centers:
        # centers() размножает строки по людям — для выбора орг оставляем одну на id
        seen = set()
        unique = []
        for row in rows:
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            unique.append(row)
        rows = unique
    if exclude_ids:
        rows = [row for row in rows if row["id"] not in exclude_ids]
    for row in rows:
        paint(row)
    if team == "yes":
        rows = [row for row in rows if row.get("teams")]
    elif team == "no":
        rows = [row for row in rows if not row.get("teams")]
    known = {row.get("region") or "" for row in rows}
    if place != EMPTY_PLACE and place not in known:
        place = ""
    counts = {}
    for row in rows:
        counts[row.get("region") or ""] = counts.get(row.get("region") or "", 0) + 1
    if place == EMPTY_PLACE:
        rows = [row for row in rows if not row.get("region")]
    elif place:
        rows = [row for row in rows if row.get("region") == place]
    if kind == db.KIND_SCHOOL:
        label = ru_count(len(rows), "школа", "школы", "школ")
    else:
        label = ru_count(len(rows), "центр", "центра", "центров")
    return {
        "kind": kind,
        "q": q,
        "place": place,
        "team": team,
        "rows": rows,
        "counts": counts,
        "count_label": label,
    }


def book_href_builder(kind, q, place, team, *, base="/"):
    def href(
        next_kind=None,
        next_place=None,
        drop_place=False,
        next_team=None,
        drop_team=False,
        next_q=None,
        drop_q=False,
    ):
        next_k = next_kind or kind
        params = {"kind": next_k}
        if not drop_q:
            chosen_q = q if next_q is None else next_q
            if chosen_q:
                params["q"] = chosen_q
        if not drop_place:
            chosen = place if next_place is None else next_place
            if chosen:
                params["place"] = chosen
        if next_k == db.KIND_SCHOOL and not drop_team:
            chosen_team = team if next_team is None else next_team
            if chosen_team:
                params["team"] = chosen_team
        return base + ("&" if "?" in base else "?") + urlencode(params)

    return href


@app.get("/")
def book(
    request: Request,
    kind: str = "school",
    q: str = "",
    place: str = "",
    team: str = "",
    flash: str = "",
):
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    data = book_dataset(kind, q, place, team)
    kind = data["kind"]
    q = data["q"]
    place = data["place"]
    team = data["team"]
    href = book_href_builder(kind, q, place, team, base="/")
    legend = []
    for region in sorted(data["counts"], key=lambda name: (name == "", name.casefold())):
        key = EMPTY_PLACE if region == "" else region
        legend.append(
            {
                "tone": "missing" if region == "" else str(tone_for(region)),
                "label": "Без региона" if region == "" else region,
                "count": data["counts"][region],
                "on": place == key,
                "href": href(next_place=key, drop_place=place == key),
            }
        )
    team_filters = []
    if kind == db.KIND_SCHOOL:
        team_filters = [
            {"key": "", "label": "Все", "on": team == ""},
            {"key": "yes", "label": "Есть", "on": team == "yes"},
            {"key": "no", "label": "Нет", "on": team == "no"},
        ]
    return page(
        request,
        "book.html",
        title="Школы" if kind == db.KIND_SCHOOL else "Центры",
        kind=kind,
        q=q,
        place=place,
        team=team,
        rows=data["rows"],
        legend=legend,
        team_filters=team_filters,
        count_label=data["count_label"],
        school_href=href(db.KIND_SCHOOL),
        center_href=href(db.KIND_CENTER),
        clear_q_href=href(drop_q=True),
        flash=flash,
        regions=db.all_regions(),
    )


@app.get("/orgs/{org_id}")
def org_card(request: Request, org_id: int, flash: str = ""):
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    org = db.org_by_id(org_id)
    if org is None:
        return RedirectResponse("/", status_code=303)
    history = mailings.history_for_org(org_id)
    edits = db.org_edits(org_id)
    title = org.get("name") or ("Школа" if org["kind"] == db.KIND_SCHOOL else "Центр")
    return page(
        request,
        "org.html",
        title=title,
        org=org,
        history=history,
        edits=edits,
        flash=flash,
        status_label=mailings.STATUS_LABEL,
        nav="school" if org["kind"] == db.KIND_SCHOOL else "center",
        regions=db.all_regions(),
    )


@app.post("/orgs/{org_id}")
async def org_save(request: Request, org_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    org = db.org_by_id(org_id)
    if org is None:
        return RedirectResponse("/", status_code=303)
    if org.get("is_deleted"):
        return RedirectResponse(f"/orgs/{org_id}?flash=deleted", status_code=303)
    before = org
    form = await request.form()
    db.update_org(
        org_id,
        {
            "name": form.get("name"),
            "letter_name": form.get("letter_name"),
            "region": form.get("region"),
            "city": form.get("city"),
            "address": form.get("address"),
            "phone": form.get("phone"),
            "note": form.get("note"),
            "coverage": form.get("coverage"),
        },
    )
    if org["kind"] == db.KIND_SCHOOL:
        db.upsert_person(
            org_id,
            "Директор",
            {
                "full_name": form.get("director_name"),
                "greeting": form.get("director_greeting"),
                "email": form.get("director_email"),
                "phone": form.get("director_phone"),
            },
        )
        db.upsert_person(
            org_id,
            "Руководитель",
            {
                "full_name": form.get("coach_name"),
                "greeting": form.get("coach_greeting"),
                "email": form.get("coach_email"),
                "phone": form.get("coach_phone"),
            },
        )
        team_names = form.getlist("team_name")
        team_parts = form.getlist("team_participation")
        teams = []
        for idx, name in enumerate(team_names):
            part = team_parts[idx] if idx < len(team_parts) else ""
            teams.append({"name": name, "participation": part})
        # пустая последняя строка для добавления
        db.replace_teams(org_id, teams)
    else:
        people = org.get("people") or []
        role = str(form.get("person_role") or (people[0]["role"] if people else "Контакт"))
        db.upsert_person(
            org_id,
            role if people else role,
            {
                "person_id": people[0]["id"] if people else "",
                "role": form.get("person_role"),
                "full_name": form.get("person_name"),
                "greeting": form.get("person_greeting"),
                "email": form.get("person_email"),
                "phone": form.get("person_phone"),
            },
        )
    db.log_org_diff(org_id, user.get("name") or "", before)
    return RedirectResponse(f"/orgs/{org_id}?flash=saved", status_code=303)


@app.post("/orgs/{org_id}/delete")
def org_delete(request: Request, org_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    org = db.org_by_id(org_id)
    if org is None:
        return RedirectResponse("/", status_code=303)
    db.soft_delete_org(org_id, user.get("name") or "")
    kind = "school" if org["kind"] == db.KIND_SCHOOL else "center"
    return RedirectResponse(f"/?kind={kind}&flash=org-deleted", status_code=303)


@app.post("/orgs/{org_id}/restore")
def org_restore(request: Request, org_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if not db.restore_org(org_id, user.get("name") or ""):
        return RedirectResponse("/journal", status_code=303)
    return RedirectResponse(f"/orgs/{org_id}?flash=restored", status_code=303)


@app.post("/orgs/{org_id}/quick")
async def org_quick(request: Request, org_id: int):
    user = current_user(request)
    if user is None:
        return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
    form = await request.form()
    field = str(form.get("field") or "")
    value = str(form.get("value") or "")
    role = str(form.get("role") or "")
    ok = db.quick_set(org_id, field, value, role=role, by_user=user.get("name") or "")
    return JSONResponse({"ok": ok})


def _account_page(request, *, error="", saved=False, mail_flash="", mail_error="", form=None):
    return page(
        request,
        "account.html",
        title="Кабинет",
        error=error,
        saved=saved,
        mail_flash=mail_flash,
        mail_error=mail_error,
        mail_accounts=outbound.list_accounts(),
        mail_form=form or {},
        smtp_presets=sorted(outbound.SMTP_PRESETS.keys()),
    )


@app.get("/account")
def account(request: Request, saved: str = "", mail: str = ""):
    flashes = {
        "added": "Ящик сохранён.",
        "updated": "Ящик обновлён.",
        "deleted": "Ящик удалён.",
        "tested": "Вход на SMTP прошёл — можно отправлять.",
    }
    return _account_page(
        request,
        saved=saved == "1",
        mail_flash=flashes.get(mail, ""),
    )


@app.post("/account")
async def account_save(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    new = str(form.get("new") or "")
    again = str(form.get("again") or "")
    if new != again:
        return _account_page(request, error="Новый пароль и повтор не совпадают.")
    result = db.change_password(user["id"], str(form.get("current") or ""), new)
    if result == "current":
        return _account_page(request, error="Текущий пароль не подошёл.")
    if result == "empty":
        return _account_page(request, error="Введите новый пароль.")
    return RedirectResponse("/account?saved=1", status_code=303)


@app.post("/account/mailboxes")
async def account_mailbox_save(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    raw_id = str(form.get("account_id") or "").strip()
    account_id = int(raw_id) if raw_id.isdigit() else None
    data = {
        "label": str(form.get("label") or "").strip(),
        "email": str(form.get("email") or "").strip(),
        "password": str(form.get("password") or ""),
        "smtp_host": str(form.get("smtp_host") or "").strip(),
        "smtp_port": str(form.get("smtp_port") or "587").strip(),
        "security": str(form.get("security") or "starttls").strip(),
        "username": str(form.get("username") or "").strip(),
    }
    try:
        new_id = outbound.upsert_account(account_id=account_id, **data)
    except ValueError as exc:
        return _account_page(request, mail_error=str(exc), form=data)
    db.log_activity(
        by_user=user.get("name") or "",
        kind="mail_account",
        detail=f"{'Обновлён' if account_id else 'Добавлен'}: {data['email'] or new_id}",
        href="/account",
        ref_id=new_id,
    )
    return RedirectResponse(
        f"/account?mail={'updated' if account_id else 'added'}",
        status_code=303,
    )


@app.post("/account/mailboxes/{account_id}/test")
def account_mailbox_test(request: Request, account_id: int):
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    result = outbound.test_account(account_id)
    if not result.get("ok"):
        return _account_page(request, mail_error=f"Не удалось войти: {result.get('error')}")
    return RedirectResponse("/account?mail=tested", status_code=303)


@app.post("/account/mailboxes/{account_id}/delete")
def account_mailbox_delete(request: Request, account_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    acc = outbound.get_account(account_id)
    outbound.delete_account(account_id)
    if acc:
        db.log_activity(
            by_user=user.get("name") or "",
            kind="mail_account",
            detail=f"Удалён: {acc.get('email') or account_id}",
            href="/account",
            ref_id=account_id,
        )
    return RedirectResponse("/account?mail=deleted", status_code=303)


@app.get("/account/smtp-suggest")
def account_smtp_suggest(request: Request, email: str = ""):
    if current_user(request) is None:
        return JSONResponse({"ok": False}, status_code=401)
    return JSONResponse({"ok": True, **outbound.suggest_smtp(email)})


MONTHS_RU = [
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]


def ru_letter_date(when: datetime | None = None) -> str:
    when = when or datetime.now()
    return f"«{when.day:02d}» {MONTHS_RU[when.month]} {when.year} г."


@app.get("/journal")
def journal(request: Request):
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    rows = db.list_activity(limit=400)
    return page(request, "journal.html", title="Журнал", rows=rows, nav="journal")


@app.get("/mailings")
def mailings_list(request: Request, flash: str = ""):
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    rows = mailings.list_mailings()
    return page(request, "mailings.html", title="Рассылки", rows=rows, nav="mailings", flash=flash)


@app.get("/schools/new")
def school_new_form(request: Request):
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return page(
        request,
        "school_new.html",
        title="Новая школа",
        nav="school",
        error="",
        form={},
        regions=db.all_regions(),
    )


@app.post("/schools/new")
async def school_new_save(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    data = {key: str(form.get(key) or "").strip() for key in (
        "region", "city", "name", "address", "letter_name", "phone", "note",
        "director_name", "director_greeting", "director_email",
        "coach_name", "coach_greeting", "coach_email",
        "team", "participation",
    )}
    try:
        org_id = db.create_school(**data)
    except ValueError as exc:
        return page(
            request,
            "school_new.html",
            title="Новая школа",
            nav="school",
            error=str(exc),
            form=data,
            regions=db.all_regions(),
        )
    db.log_activity(
        by_user=user.get("name") or "",
        kind="org_create",
        detail=f"Школа: {data.get('name') or org_id}",
        href=f"/orgs/{org_id}",
        ref_id=org_id,
    )
    return RedirectResponse("/?kind=school&flash=school-added", status_code=303)


@app.get("/centers/new")
def center_new_form(request: Request):
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return page(
        request,
        "center_new.html",
        title="Новый центр",
        nav="center",
        error="",
        form={},
        regions=db.all_regions(),
    )


@app.post("/centers/new")
async def center_new_save(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    data = {key: str(form.get(key) or "").strip() for key in (
        "region", "city", "name", "address", "letter_name", "coverage", "phone", "note",
        "person_name", "person_role", "person_greeting", "person_email", "person_phone",
    )}
    try:
        org_id = db.create_center(**data)
    except ValueError as exc:
        return page(
            request,
            "center_new.html",
            title="Новый центр",
            nav="center",
            error=str(exc),
            form=data,
            regions=db.all_regions(),
        )
    db.log_activity(
        by_user=user.get("name") or "",
        kind="org_create",
        detail=f"Центр: {data.get('name') or org_id}",
        href=f"/orgs/{org_id}",
        ref_id=org_id,
    )
    return RedirectResponse("/?kind=center&flash=center-added", status_code=303)


@app.get("/schools/import")
def school_import_form(request: Request, flash: str = ""):
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return page(
        request,
        "school_import.html",
        title="Импорт школ",
        nav="school",
        headers=db.SCHOOL_EXCEL_HEADERS,
        flash=flash,
        error="",
        result=None,
    )


@app.get("/schools/template.xlsx")
def school_template(request: Request):
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    data = db.schools_template_xlsx()
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="tyuf_schools_template.xlsx"; filename*=UTF-8\'\'%D1%88%D0%B0%D0%B1%D0%BB%D0%BE%D0%BD_%D1%88%D0%BA%D0%BE%D0%BB%D1%8B_%D0%A2%D0%AE%D0%A4.xlsx'
        },
    )


@app.post("/schools/import")
async def school_import_save(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", None):
        return page(
            request,
            "school_import.html",
            title="Импорт школ",
            nav="school",
            headers=db.SCHOOL_EXCEL_HEADERS,
            flash="",
            error="Выберите файл Excel (.xlsx)",
            result=None,
        )
    raw = await upload.read()
    if not raw:
        return page(
            request,
            "school_import.html",
            title="Импорт школ",
            nav="school",
            headers=db.SCHOOL_EXCEL_HEADERS,
            flash="",
            error="Файл пустой",
            result=None,
        )
    try:
        result = db.import_schools_xlsx(raw)
    except Exception as exc:
        return page(
            request,
            "school_import.html",
            title="Импорт школ",
            nav="school",
            headers=db.SCHOOL_EXCEL_HEADERS,
            flash="",
            error=f"Не удалось прочитать файл: {exc}",
            result=None,
        )
    if result.get("error"):
        return page(
            request,
            "school_import.html",
            title="Импорт школ",
            nav="school",
            headers=db.SCHOOL_EXCEL_HEADERS,
            flash="",
            error=result["error"],
            result=None,
        )
    db.log_activity(
        by_user=user.get("name") or "",
        kind="org_import",
        detail=f"Excel · новых {result['ok']}, пропущено {result['skip']}",
        href="/?kind=school",
    )
    return RedirectResponse(
        f"/?kind=school&flash=imported-{result['ok']}-{result['skip']}",
        status_code=303,
    )


@app.get("/mailings/new")
def mailing_new_form(request: Request):
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    return page(
        request,
        "mailing_new.html",
        title="Новая рассылка",
        nav="mailings",
        letter_html=pdf_letters.DEFAULT_LETTER_HTML,
        letter_date=ru_letter_date(),
        placeholders=pdf_letters.PLACEHOLDERS,
        placeholder_help=pdf_letters.PLACEHOLDER_HELP,
        error="",
    )


@app.post("/mailings/new")
async def mailing_new_save(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    mode = str(form.get("mode") or "html")
    mailing_id = mailings.create_mailing(
        title=str(form.get("title") or ""),
        letter_date=str(form.get("letter_date") or ""),
        mode=mode,
        letter_html=str(form.get("letter_html") or ""),
        user_name=user["name"],
    )
    template = form.get("template_pdf")
    annex = form.get("annex_pdf")
    if template is not None and getattr(template, "filename", None):
        data = await template.read()
        if data:
            mailings.save_upload(mailing_id, "template", template.filename, data)
    if annex is not None and getattr(annex, "filename", None):
        data = await annex.read()
        if data:
            mailings.save_upload(mailing_id, "annex", annex.filename, data)
    return RedirectResponse(f"/mailings/{mailing_id}", status_code=303)


@app.post("/mailings/{mailing_id}/delete")
def mailing_delete(request: Request, mailing_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    mailings.delete_mailing(mailing_id, user.get("name") or "")
    return RedirectResponse("/mailings?flash=deleted", status_code=303)


@app.get("/mailings/{mailing_id}")
def mailing_detail(
    request: Request,
    mailing_id: int,
    q: str = "",
    kind: str = "school",
    place: str = "",
    team: str = "",
    flash: str = "",
):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    mailing = mailings.get_mailing(mailing_id)
    if mailing is None:
        return RedirectResponse("/mailings", status_code=303)
    letters = mailings.letters_for(mailing_id)
    taken = {row["org_id"] for row in letters}
    data = book_dataset(kind, q, place, team, exclude_ids=taken, dedupe_centers=True)
    kind = data["kind"]
    q = data["q"]
    place = data["place"]
    team = data["team"]
    base = f"/mailings/{mailing_id}"
    href = book_href_builder(kind, q, place, team, base=base)
    legend = []
    for region in sorted(data["counts"], key=lambda name: (name == "", name.casefold())):
        key = EMPTY_PLACE if region == "" else region
        legend.append(
            {
                "tone": "missing" if region == "" else str(tone_for(region)),
                "label": "Без региона" if region == "" else region,
                "count": data["counts"][region],
                "on": place == key,
                "href": href(next_place=key, drop_place=place == key),
            }
        )
    team_filters = []
    if kind == db.KIND_SCHOOL:
        team_filters = [
            {"key": "", "label": "Все", "on": team == ""},
            {"key": "yes", "label": "Есть", "on": team == "yes"},
            {"key": "no", "label": "Нет", "on": team == "no"},
        ]
    ready = sum(1 for row in letters if row.get("pdf_name"))
    if not (mailing.get("email_subject") or "").strip():
        mailing["email_subject"] = outbound.DEFAULT_EMAIL_SUBJECT
    if not (mailing.get("email_body") or "").strip():
        mailing["email_body"] = outbound.DEFAULT_EMAIL_BODY
    return page(
        request,
        "mailing.html",
        title=mailing["title"],
        nav="mailings",
        mailing=mailing,
        letters=letters,
        candidates=data["rows"],
        kind=kind,
        q=q,
        place=place,
        team=team,
        legend=legend,
        team_filters=team_filters,
        count_label=data["count_label"],
        school_href=href(db.KIND_SCHOOL),
        center_href=href(db.KIND_CENTER),
        clear_q_href=href(drop_q=True),
        flash=flash,
        placeholders=pdf_letters.PLACEHOLDERS,
        placeholder_help=pdf_letters.PLACEHOLDER_HELP,
        status_label=mailings.STATUS_LABEL,
        ready_count=ready,
        has_template_pdf=mailings.template_path(mailing_id).exists(),
        has_annex_pdf=mailings.annex_path(mailing_id).exists(),
        mail_accounts=outbound.list_accounts(),
    )


@app.post("/mailings/{mailing_id}/settings")
async def mailing_settings(request: Request, mailing_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    before = mailings.get_mailing(mailing_id)
    if before is None:
        return RedirectResponse("/mailings", status_code=303)
    form = await request.form()
    mode = str(form.get("mode") or "html")
    title = str(form.get("title") or "").strip() or "Без названия"
    letter_date = str(form.get("letter_date") or "").strip()
    mode = mode if mode in {"html", "pdf"} else "html"
    mailings.update_mailing(
        mailing_id,
        title=title,
        letter_date=letter_date,
        mode=mode,
        letter_html=str(form.get("letter_html") or ""),
    )
    uploads = []
    template = form.get("template_pdf")
    annex = form.get("annex_pdf")
    if template is not None and getattr(template, "filename", None):
        data = await template.read()
        if data:
            mailings.save_upload(mailing_id, "template", template.filename, data)
            uploads.append("шаблон PDF")
    if annex is not None and getattr(annex, "filename", None):
        data = await annex.read()
        if data:
            mailings.save_upload(mailing_id, "annex", annex.filename, data)
            uploads.append("приложение PDF")
    bits = []
    if title != (before.get("title") or ""):
        bits.append(f"название → «{title}»")
    if letter_date != (before.get("letter_date") or ""):
        bits.append(f"дата → {letter_date or '—'}")
    if mode != (before.get("mode") or ""):
        bits.append(f"режим → {mode}")
    if (str(form.get("letter_html") or "") != (before.get("letter_html") or "")):
        bits.append("текст письма")
    bits.extend(uploads)
    if bits:
        db.log_activity(
            by_user=user.get("name") or "",
            kind="mailing_settings",
            detail=f"«{title}»: " + "; ".join(bits),
            href=f"/mailings/{mailing_id}",
        )
    return RedirectResponse(f"/mailings/{mailing_id}?flash=saved", status_code=303)


@app.post("/mailings/{mailing_id}/add")
async def mailing_add(request: Request, mailing_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    org_ids = []
    for raw in form.getlist("org_id"):
        try:
            org_ids.append(int(raw))
        except ValueError:
            continue
    added = mailings.add_recipients(mailing_id, org_ids, user["name"])
    # Сразу собираем PDF с подстановкой для новых писем
    gen = {"ok": 0, "fail": 0}
    if added:
        gen = mailings.generate_letters(mailing_id, user["name"])
    return RedirectResponse(
        f"/mailings/{mailing_id}?flash=added-{added}-gen-{gen['ok']}-{gen['fail']}",
        status_code=303,
    )


@app.post("/mailings/{mailing_id}/generate")
def mailing_generate(request: Request, mailing_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    result = mailings.generate_letters(mailing_id, user["name"])
    return RedirectResponse(
        f"/mailings/{mailing_id}?flash=gen-{result['ok']}-{result['fail']}",
        status_code=303,
    )


@app.post("/mailings/{mailing_id}/email-settings")
async def mailing_email_settings(request: Request, mailing_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if mailings.get_mailing(mailing_id) is None:
        return RedirectResponse("/mailings", status_code=303)
    form = await request.form()
    raw_acc = str(form.get("email_account_id") or "0").strip()
    try:
        account_id = int(raw_acc)
    except ValueError:
        account_id = 0
    mailings.update_mailing(
        mailing_id,
        email_account_id=account_id,
        email_subject=str(form.get("email_subject") or "").strip(),
        email_body=str(form.get("email_body") or ""),
        email_attach=1 if form.get("email_attach") else 0,
    )
    db.log_activity(
        by_user=user.get("name") or "",
        kind="mailing_settings",
        detail=f"Настройки email-отправки",
        href=f"/mailings/{mailing_id}",
        ref_id=mailing_id,
    )
    return RedirectResponse(f"/mailings/{mailing_id}?flash=email-saved", status_code=303)


@app.post("/mailings/{mailing_id}/send")
def mailing_send_all(request: Request, mailing_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    result = outbound.send_mailing(mailing_id, user.get("name") or "")
    if result.get("error") and not result.get("ok") and not result.get("fail"):
        return RedirectResponse(
            f"/mailings/{mailing_id}?flash=send-err",
            status_code=303,
        )
    return RedirectResponse(
        f"/mailings/{mailing_id}?flash=send-{result['ok']}-{result['fail']}-{result['skip']}",
        status_code=303,
    )


@app.get("/mailings/{mailing_id}/zip")
def mailing_zip(request: Request, mailing_id: int):
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    path = mailings.zip_letters(mailing_id)
    if path is None or not path.exists():
        return RedirectResponse(f"/mailings/{mailing_id}?flash=nozip", status_code=303)
    mailing = mailings.get_mailing(mailing_id)
    name = f"{(mailing or {}).get('title') or 'mailing'}.zip"
    return FileResponse(path, filename=name, media_type="application/zip")


@app.post("/mailings/{mailing_id}/letters/{letter_id}/delete")
def mailing_letter_delete(request: Request, mailing_id: int, letter_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    letter = mailings.get_letter(letter_id)
    if letter and letter["mailing_id"] == mailing_id:
        mailings.remove_letter(letter_id, user.get("name") or "")
    return RedirectResponse(f"/mailings/{mailing_id}", status_code=303)


@app.get("/mailings/{mailing_id}/letters/{letter_id}")
def mailing_letter(request: Request, mailing_id: int, letter_id: int, flash: str = ""):
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    mailing = mailings.get_mailing(mailing_id)
    letter = mailings.get_letter(letter_id)
    if not mailing or not letter or letter["mailing_id"] != mailing_id:
        return RedirectResponse("/mailings", status_code=303)
    if not (mailing.get("email_subject") or "").strip():
        mailing["email_subject"] = outbound.DEFAULT_EMAIL_SUBJECT
    if not (mailing.get("email_body") or "").strip():
        mailing["email_body"] = outbound.DEFAULT_EMAIL_BODY
    events = mailings.events_for(letter_id)
    pdf = mailings.letter_pdf_path(mailing_id, letter_id)
    draft = outbound.compose_for_letter(mailing, letter)
    account = None
    aid = int(mailing.get("email_account_id") or 0)
    if aid:
        account = outbound.get_account(aid)
    return page(
        request,
        "letter.html",
        title=letter["org_name"] or "Письмо",
        nav="mailings",
        mailing=mailing,
        letter=letter,
        events=events,
        has_pdf=pdf.exists(),
        draft=draft,
        mail_account=account,
        mail_accounts=outbound.list_accounts(),
        flash=flash,
    )


@app.post("/mailings/{mailing_id}/letters/{letter_id}/send")
def mailing_letter_send(request: Request, mailing_id: int, letter_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    letter = mailings.get_letter(letter_id)
    if not letter or letter["mailing_id"] != mailing_id:
        return RedirectResponse("/mailings", status_code=303)
    result = outbound.send_letter(letter_id, user.get("name") or "")
    if result.get("ok"):
        return RedirectResponse(
            f"/mailings/{mailing_id}/letters/{letter_id}?flash=sent",
            status_code=303,
        )
    return RedirectResponse(
        f"/mailings/{mailing_id}/letters/{letter_id}?flash=send-fail",
        status_code=303,
    )


@app.get("/mailings/{mailing_id}/letters/{letter_id}/pdf")
def mailing_letter_pdf(request: Request, mailing_id: int, letter_id: int):
    if current_user(request) is None:
        return RedirectResponse("/login", status_code=303)
    letter = mailings.get_letter(letter_id)
    if not letter or letter["mailing_id"] != mailing_id:
        return RedirectResponse("/mailings", status_code=303)
    path = mailings.letter_pdf_path(mailing_id, letter_id)
    if not path.exists():
        return RedirectResponse(f"/mailings/{mailing_id}/letters/{letter_id}", status_code=303)
    # inline — иначе браузер скачивает файл и iframe «Просмотр» остаётся пустым
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=path.name,
        content_disposition_type="inline",
    )


@app.post("/mailings/{mailing_id}/letters/{letter_id}/sent")
def mailing_letter_sent(request: Request, mailing_id: int, letter_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    letter = mailings.get_letter(letter_id)
    if letter and letter["mailing_id"] == mailing_id:
        mailings.mark_sent(letter_id, user["name"])
    return RedirectResponse(f"/mailings/{mailing_id}/letters/{letter_id}", status_code=303)


@app.post("/mailings/{mailing_id}/letters/{letter_id}/reply")
async def mailing_letter_reply(request: Request, mailing_id: int, letter_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    letter = mailings.get_letter(letter_id)
    if letter and letter["mailing_id"] == mailing_id:
        mailings.mark_reply(letter_id, str(form.get("detail") or ""), user["name"])
    return RedirectResponse(f"/mailings/{mailing_id}/letters/{letter_id}", status_code=303)


@app.post("/mailings/{mailing_id}/letters/{letter_id}/note")
async def mailing_letter_note(request: Request, mailing_id: int, letter_id: int):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    letter = mailings.get_letter(letter_id)
    if letter and letter["mailing_id"] == mailing_id:
        mailings.add_note(letter_id, str(form.get("detail") or ""), user["name"])
    return RedirectResponse(f"/mailings/{mailing_id}/letters/{letter_id}", status_code=303)
