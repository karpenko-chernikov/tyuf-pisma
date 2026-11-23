"""Рассылки: кампания → письма → история событий."""

from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path

from app import db
from app import pdf_letters

MAILINGS_DIR = db.DATA / "mailings"

STATUS_LABEL = {
    "new": "новое",
    "ready": "готово",
    "sent": "отправлено",
    "replied": "есть ответ",
    "error": "ошибка",
}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _dir(mailing_id: int) -> Path:
    path = MAILINGS_DIR / str(mailing_id)
    path.mkdir(parents=True, exist_ok=True)
    (path / "letters").mkdir(exist_ok=True)
    return path


def init_tables(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mailings (
          id INTEGER PRIMARY KEY,
          title TEXT NOT NULL,
          letter_date TEXT NOT NULL DEFAULT '',
          out_prefix TEXT NOT NULL DEFAULT '',
          mode TEXT NOT NULL DEFAULT 'html',
          letter_html TEXT NOT NULL DEFAULT '',
          template_name TEXT NOT NULL DEFAULT '',
          annex_name TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          created_by TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS mailing_letters (
          id INTEGER PRIMARY KEY,
          mailing_id INTEGER NOT NULL REFERENCES mailings(id) ON DELETE CASCADE,
          org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
          out_no TEXT NOT NULL DEFAULT '',
          org_name TEXT NOT NULL DEFAULT '',
          org_kind TEXT NOT NULL DEFAULT '',
          city TEXT NOT NULL DEFAULT '',
          region TEXT NOT NULL DEFAULT '',
          email TEXT NOT NULL DEFAULT '',
          person_name TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'new',
          pdf_name TEXT NOT NULL DEFAULT '',
          error TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          UNIQUE(mailing_id, org_id)
        );
        CREATE TABLE IF NOT EXISTS mailing_events (
          id INTEGER PRIMARY KEY,
          letter_id INTEGER NOT NULL REFERENCES mailing_letters(id) ON DELETE CASCADE,
          at TEXT NOT NULL,
          kind TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '',
          by_user TEXT NOT NULL DEFAULT ''
        );
        """
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(mailings)")}
    alters = {
        "email_account_id": "ALTER TABLE mailings ADD COLUMN email_account_id INTEGER NOT NULL DEFAULT 0",
        "email_subject": "ALTER TABLE mailings ADD COLUMN email_subject TEXT NOT NULL DEFAULT ''",
        "email_body": "ALTER TABLE mailings ADD COLUMN email_body TEXT NOT NULL DEFAULT ''",
        "email_attach": "ALTER TABLE mailings ADD COLUMN email_attach INTEGER NOT NULL DEFAULT 1",
    }
    for name, sql in alters.items():
        if name not in cols:
            conn.execute(sql)


def list_mailings():
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT m.*,
              (SELECT COUNT(*) FROM mailing_letters l WHERE l.mailing_id = m.id) AS letters,
              (SELECT COUNT(*) FROM mailing_letters l WHERE l.mailing_id = m.id AND l.status = 'ready') AS ready,
              (SELECT COUNT(*) FROM mailing_letters l WHERE l.mailing_id = m.id AND l.status = 'sent') AS sent,
              (SELECT COUNT(*) FROM mailing_letters l WHERE l.mailing_id = m.id AND l.status = 'replied') AS replied
            FROM mailings m
            ORDER BY m.id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_mailing(mailing_id: int):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM mailings WHERE id = ?", (mailing_id,)).fetchone()
    return dict(row) if row else None


def create_mailing(*, title: str, letter_date: str, mode: str, letter_html: str, user_name: str):
    from app import outbound

    title = (title or "").strip() or "Без названия"
    mode = mode if mode in {"html", "pdf"} else "html"
    html = (letter_html or "").strip() or pdf_letters.DEFAULT_LETTER_HTML
    with db.connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO mailings (
              title, letter_date, mode, letter_html, created_at, created_by,
              email_subject, email_body, email_attach
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                title,
                letter_date.strip(),
                mode,
                html,
                _now(),
                user_name or "",
                outbound.DEFAULT_EMAIL_SUBJECT,
                outbound.DEFAULT_EMAIL_BODY,
            ),
        )
        mailing_id = cur.lastrowid
        db.log_activity(
            by_user=user_name or "",
            kind="mailing_create",
            detail=f"«{title}» · режим {mode}",
            href=f"/mailings/{mailing_id}",
            conn=conn,
        )
    _dir(mailing_id)
    return mailing_id


def delete_mailing(mailing_id: int, user_name: str = "") -> bool:
    mailing = get_mailing(mailing_id)
    if mailing is None:
        return False
    title = mailing.get("title") or f"#{mailing_id}"
    with db.connect() as conn:
        letter_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM mailing_letters WHERE mailing_id = ?", (mailing_id,)
            )
        ]
        if letter_ids:
            placeholders = ",".join("?" * len(letter_ids))
            conn.execute(
                f"DELETE FROM mailing_events WHERE letter_id IN ({placeholders})",
                letter_ids,
            )
        conn.execute("DELETE FROM mailing_letters WHERE mailing_id = ?", (mailing_id,))
        conn.execute("DELETE FROM mailings WHERE id = ?", (mailing_id,))
        db.log_activity(
            by_user=user_name,
            kind="mailing_delete",
            detail=f"«{title}» · писем было {len(letter_ids)}",
            href="/mailings",
            conn=conn,
        )
    folder = MAILINGS_DIR / str(mailing_id)
    if folder.exists():
        import shutil

        shutil.rmtree(folder, ignore_errors=True)
    return True


def save_upload(mailing_id: int, kind: str, filename: str, data: bytes):
    folder = _dir(mailing_id)
    safe = "template.pdf" if kind == "template" else "annex.pdf"
    path = folder / safe
    path.write_bytes(data)
    with db.connect() as conn:
        if kind == "template":
            # Не переключаем mode автоматически: PDF-подстановка не умеет
            # переносить строки, из-за этого текст налезает друг на друга.
            conn.execute(
                "UPDATE mailings SET template_name = ? WHERE id = ?",
                (filename or safe, mailing_id),
            )
        else:
            conn.execute(
                "UPDATE mailings SET annex_name = ? WHERE id = ?",
                (filename or safe, mailing_id),
            )


def update_mailing(mailing_id: int, **fields):
    allowed = {
        "title",
        "letter_date",
        "mode",
        "letter_html",
        "email_account_id",
        "email_subject",
        "email_body",
        "email_attach",
    }
    parts = []
    values = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        parts.append(f"{key} = ?")
        values.append(value)
    if not parts:
        return
    values.append(mailing_id)
    with db.connect() as conn:
        conn.execute(f"UPDATE mailings SET {', '.join(parts)} WHERE id = ?", values)


def template_path(mailing_id: int) -> Path:
    return _dir(mailing_id) / "template.pdf"


def annex_path(mailing_id: int) -> Path:
    return _dir(mailing_id) / "annex.pdf"


def letter_pdf_path(mailing_id: int, letter_id: int) -> Path:
    return _dir(mailing_id) / "letters" / f"{letter_id}.pdf"


def letters_for(mailing_id: int):
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM mailing_letters
            WHERE mailing_id = ?
            ORDER BY region COLLATE NOCASE, city COLLATE NOCASE, org_name COLLATE NOCASE, id
            """,
            (mailing_id,),
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["status_label"] = STATUS_LABEL.get(item["status"], item["status"])
        out.append(item)
    return out


def get_letter(letter_id: int):
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM mailing_letters WHERE id = ?", (letter_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["status_label"] = STATUS_LABEL.get(item["status"], item["status"])
    return item


def events_for(letter_id: int):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM mailing_events WHERE letter_id = ? ORDER BY id",
            (letter_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def history_for_org(org_id: int) -> list[dict]:
    """Все письма по организации во всех рассылках + последнее событие."""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT l.*,
                   m.title AS mailing_title,
                   m.letter_date AS mailing_date,
                   (
                     SELECT e.at || ' · ' || e.kind || CASE WHEN e.detail != '' THEN ': ' || e.detail ELSE '' END
                     FROM mailing_events e
                     WHERE e.letter_id = l.id
                     ORDER BY e.id DESC
                     LIMIT 1
                   ) AS last_event
            FROM mailing_letters l
            JOIN mailings m ON m.id = l.mailing_id
            WHERE l.org_id = ?
            ORDER BY l.id DESC
            """,
            (org_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _add_event(conn, letter_id: int, kind: str, detail: str = "", by_user: str = ""):
    conn.execute(
        "INSERT INTO mailing_events (letter_id, at, kind, detail, by_user) VALUES (?, ?, ?, ?, ?)",
        (letter_id, _now(), kind, detail, by_user),
    )


def _contact_for_org(org: dict) -> tuple[str, str, str]:
    people = org.get("people") or []
    if org.get("kind") == db.KIND_SCHOOL:
        director = next((p for p in people if p.get("role") == "Директор"), None)
        coach = next((p for p in people if p.get("role") == "Руководитель"), None)
        person = director or coach or (people[0] if people else None)
    else:
        person = people[0] if people else None
    if not person:
        return "", "", ""
    return person.get("full_name") or "", person.get("email") or "", person.get("greeting") or ""


def _short_addressee(full_name: str) -> str:
    parts = [p for p in (full_name or "").replace("ё", "е").split() if p]
    if len(parts) >= 3:
        return f"{parts[1][0]}. {parts[2][0]}. {parts[0]}"
    if len(parts) == 2:
        return f"{parts[1][0]}. {parts[0]}"
    return full_name or ""


def _name_patronymic(full_name: str) -> str:
    parts = [p for p in (full_name or "").split() if p]
    if len(parts) >= 3:
        return f"{parts[1]} {parts[2]}"
    if len(parts) == 2:
        return parts[1]
    return full_name or ""


def _is_female_name(full_name: str, stored_greeting: str = "") -> bool:
    g = (stored_greeting or "").strip().casefold()
    if g.startswith("уважаемая"):
        return True
    if g.startswith("уважаемый"):
        return False
    parts = [p for p in (full_name or "").replace("ё", "е").split() if p]
    if len(parts) >= 3:
        patronymic = parts[2].casefold()
        if patronymic.endswith(("овна", "евна", "ична", "инична")):
            return True
        if patronymic.endswith(("ович", "евич", "ич")):
            return False
    if parts:
        first = parts[1].casefold() if len(parts) >= 2 else parts[0].casefold()
        if first.endswith(("а", "я")) and not first.endswith(("илья", "никита", "кузьма", "лука", "фома")):
            return True
    return False


def _personal_greeting(full_name: str, stored_greeting: str = "") -> str:
    stored = (stored_greeting or "").strip()
    if stored:
        # в письме после обращения обычно запятая
        if stored.endswith("!"):
            stored = stored[:-1].rstrip() + ","
        elif not stored.endswith(","):
            stored = stored.rstrip(".") + ","
        return stored
    address = _name_patronymic(full_name).strip()
    if not address:
        return "Уважаемые коллеги!"
    word = "Уважаемая" if _is_female_name(full_name, stored_greeting) else "Уважаемый"
    return f"{word} {address},"


def build_fields(mailing: dict, org: dict) -> dict:
    person_name, email, stored_greeting = _contact_for_org(org)
    # в теле письма — «для письма», иначе полное название
    school = (org.get("letter_name") or "").strip() or (org.get("name") or "")
    if org.get("kind") == db.KIND_SCHOOL:
        role_line = f"Директору {school}" if school else "Директору"
    else:
        role = (org.get("people") or [{}])[0].get("role") if org.get("people") else ""
        role_line = f"{role} {school}".strip() if role else school
    # в шапке — инициалы директора, не letter_name школы
    addressee = _short_addressee(person_name)
    greeting = _personal_greeting(person_name, stored_greeting)
    city = org.get("city") or ""
    if city and not city.lower().startswith("г"):
        city = f"г. {city}"
    teams = ", ".join(t.get("name") or "" for t in (org.get("teams") or []) if t.get("name"))
    return {
        "school": school,
        "city": city,
        "address": org.get("address") or "",
        "email": email,
        "phone": org.get("phone") or "",
        "director": person_name,
        "addressee": addressee,
        "role_line": role_line,
        "greeting": greeting,
        "date": mailing.get("letter_date") or "",
        "region": org.get("region") or "",
        "teams": teams,
    }


def add_recipients(mailing_id: int, org_ids: list[int], user_name: str = "") -> int:
    mailing = get_mailing(mailing_id)
    if not mailing:
        return 0
    added = 0
    graph = {org["id"]: org for org in db._graph(db.KIND_SCHOOL) + db._graph(db.KIND_CENTER)}
    with db.connect() as conn:
        have = {
            row["org_id"]
            for row in conn.execute(
                "SELECT org_id FROM mailing_letters WHERE mailing_id = ?", (mailing_id,)
            )
        }
        for org_id in org_ids:
            if org_id in have:
                continue
            org = graph.get(org_id)
            if not org:
                continue
            person_name, email, _ = _contact_for_org(org)
            cur = conn.execute(
                """
                INSERT INTO mailing_letters (
                  mailing_id, org_id, org_name, org_kind, city, region,
                  email, person_name, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
                """,
                (
                    mailing_id,
                    org_id,
                    org.get("name") or "",
                    org.get("kind") or "",
                    org.get("city") or "",
                    org.get("region") or "",
                    email,
                    person_name,
                    _now(),
                ),
            )
            _add_event(conn, cur.lastrowid, "created", "Добавлено в рассылку", user_name)
            added += 1
            have.add(org_id)
    if added:
        db.log_activity(
            by_user=user_name,
            kind="mailing_add",
            detail=f"«{mailing.get('title') or mailing_id}» · +{added}",
            href=f"/mailings/{mailing_id}",
        )
    return added


def remove_letter(letter_id: int, user_name: str = ""):
    letter = get_letter(letter_id)
    if not letter:
        return
    pdf = letter_pdf_path(letter["mailing_id"], letter_id)
    with db.connect() as conn:
        conn.execute("DELETE FROM mailing_letters WHERE id = ?", (letter_id,))
        db.log_activity(
            by_user=user_name,
            kind="letter_delete",
            detail=f"«{letter.get('org_name') or letter_id}» · рассылка #{letter['mailing_id']}",
            href=f"/mailings/{letter['mailing_id']}",
            conn=conn,
        )
    pdf.unlink(missing_ok=True)


def generate_letters(mailing_id: int, user_name: str = "", only_ids: list[int] | None = None) -> dict:
    mailing = get_mailing(mailing_id)
    if not mailing:
        return {"ok": 0, "fail": 0, "error": "Рассылка не найдена"}
    graph = {org["id"]: org for org in db._graph(db.KIND_SCHOOL) + db._graph(db.KIND_CENTER)}
    letters = letters_for(mailing_id)
    if only_ids is not None:
        wanted = set(only_ids)
        letters = [row for row in letters if row["id"] in wanted]
    mode = mailing["mode"]
    tpl = template_path(mailing_id)
    annex = annex_path(mailing_id)
    ok = fail = 0
    for row in letters:
        org = graph.get(row["org_id"])
        if not org:
            fail += 1
            continue
        data = build_fields(mailing, org)
        pdf = letter_pdf_path(mailing_id, row["id"])
        result = pdf_letters.build_letter_pdf(
            dst=pdf,
            data=data,
            mode=mode,
            html_template=mailing.get("letter_html") or "",
            template_pdf=tpl if tpl.exists() else None,
            annex_pdf=annex if annex.exists() else None,
        )
        with db.connect() as conn:
            if result.get("ok"):
                conn.execute(
                    "UPDATE mailing_letters SET status = 'ready', pdf_name = ?, email = ?, person_name = ?, error = '' WHERE id = ?",
                    (pdf.name, data.get("email") or "", data.get("director") or "", row["id"]),
                )
                note = "PDF собран"
                if result.get("missing"):
                    note += "; не найдены поля: " + ", ".join(result["missing"])
                _add_event(conn, row["id"], "generated", note, user_name)
                ok += 1
            else:
                conn.execute(
                    "UPDATE mailing_letters SET status = 'error', error = ? WHERE id = ?",
                    (result.get("error") or "Не удалось собрать PDF", row["id"]),
                )
                _add_event(conn, row["id"], "error", result.get("error") or "", user_name)
                fail += 1
    if ok or fail:
        db.log_activity(
            by_user=user_name,
            kind="mailing_generate",
            detail=f"«{mailing.get('title') or mailing_id}» · готово {ok}, ошибок {fail}",
            href=f"/mailings/{mailing_id}",
        )
    return {"ok": ok, "fail": fail, "error": ""}


def mark_sent(letter_id: int, user_name: str = "", detail: str = ""):
    letter = get_letter(letter_id)
    with db.connect() as conn:
        conn.execute("UPDATE mailing_letters SET status = 'sent' WHERE id = ?", (letter_id,))
        _add_event(conn, letter_id, "sent", detail or "Отмечено как отправленное", user_name)
    if letter:
        db.log_activity(
            by_user=user_name,
            kind="letter_sent",
            detail=f"«{letter.get('org_name') or letter_id}»",
            href=f"/mailings/{letter['mailing_id']}/letters/{letter_id}",
        )


def mark_reply(letter_id: int, detail: str, user_name: str = ""):
    letter = get_letter(letter_id)
    note = (detail or "").strip()
    with db.connect() as conn:
        conn.execute("UPDATE mailing_letters SET status = 'replied' WHERE id = ?", (letter_id,))
        _add_event(conn, letter_id, "replied", note, user_name)
    if letter:
        tail = f": {note}" if note else ""
        db.log_activity(
            by_user=user_name,
            kind="letter_reply",
            detail=f"«{letter.get('org_name') or letter_id}»{tail}",
            href=f"/mailings/{letter['mailing_id']}/letters/{letter_id}",
        )


def add_note(letter_id: int, detail: str, user_name: str = ""):
    letter = get_letter(letter_id)
    note = (detail or "").strip()
    with db.connect() as conn:
        _add_event(conn, letter_id, "note", note, user_name)
    if letter and note:
        db.log_activity(
            by_user=user_name,
            kind="letter_note",
            detail=f"«{letter.get('org_name') or letter_id}»: {note}",
            href=f"/mailings/{letter['mailing_id']}/letters/{letter_id}",
        )


def zip_letters(mailing_id: int) -> Path | None:
    letters = [row for row in letters_for(mailing_id) if row.get("pdf_name")]
    if not letters:
        return None
    folder = _dir(mailing_id)
    zip_path = folder / "letters.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for row in letters:
            pdf = letter_pdf_path(mailing_id, row["id"])
            if not pdf.exists():
                continue
            safe_name = f"{(row['org_name'] or 'org')[:80]}.pdf"
            safe_name = "".join(ch if ch.isalnum() or ch in " ._-" else "_" for ch in safe_name)
            zf.write(pdf, safe_name)
    return zip_path
