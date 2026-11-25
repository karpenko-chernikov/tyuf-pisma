import hashlib
import os
import re
import secrets
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DB_PATH = DATA / "pisma.db"
WORKBOOK = DATA / "baza.xlsx"
TELEGRAM_WORKBOOK = Path(
    "/Users/nikita/Library/Containers/ru.keepcoder.Telegram/Data/tmp/База_ТЮФ.xlsx"
)

KIND_SCHOOL = "school"
KIND_CENTER = "center"


def load_env():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def connect():
    DATA.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000).hex()
    return f"{salt}${digest}"


def check_password(password, stored):
    salt, digest = stored.split("$", 1)
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000).hex()
    return secrets.compare_digest(test, digest)


def init():
    load_env()
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY,
              name TEXT NOT NULL UNIQUE,
              password TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orgs (
              id INTEGER PRIMARY KEY,
              kind TEXT NOT NULL,
              region TEXT NOT NULL DEFAULT '',
              city TEXT NOT NULL DEFAULT '',
              name TEXT NOT NULL DEFAULT '',
              letter_name TEXT NOT NULL DEFAULT '',
              coverage TEXT NOT NULL DEFAULT '',
              phone TEXT NOT NULL DEFAULT '',
              address TEXT NOT NULL DEFAULT '',
              note TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS teams (
              id INTEGER PRIMARY KEY,
              org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
              name TEXT NOT NULL DEFAULT '',
              participation TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS people (
              id INTEGER PRIMARY KEY,
              org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
              role TEXT NOT NULL DEFAULT '',
              full_name TEXT NOT NULL DEFAULT '',
              greeting TEXT NOT NULL DEFAULT '',
              email TEXT NOT NULL DEFAULT '',
              phone TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS org_edits (
              id INTEGER PRIMARY KEY,
              org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
              at TEXT NOT NULL,
              by_user TEXT NOT NULL DEFAULT '',
              detail TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_org_edits_org ON org_edits(org_id, id DESC);
            CREATE TABLE IF NOT EXISTS activity_log (
              id INTEGER PRIMARY KEY,
              at TEXT NOT NULL,
              by_user TEXT NOT NULL DEFAULT '',
              kind TEXT NOT NULL DEFAULT '',
              detail TEXT NOT NULL DEFAULT '',
              href TEXT NOT NULL DEFAULT '',
              ref_id INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_activity_log_id ON activity_log(id DESC);
            """
        )
        _migrate_columns(conn)
        from app import mailings as mailings_mod
        from app import outbound as outbound_mod

        mailings_mod.init_tables(conn)
        outbound_mod.init_tables(conn)
        _ensure_users(conn)
        if conn.execute("SELECT COUNT(*) AS n FROM orgs").fetchone()["n"] == 0:
            ensure_workbook()
            if WORKBOOK.exists():
                _import_workbook(conn, WORKBOOK)


def _migrate_columns(conn) -> None:
    org_cols = {row[1] for row in conn.execute("PRAGMA table_info(orgs)")}
    if "deleted_at" not in org_cols:
        conn.execute("ALTER TABLE orgs ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
    log_cols = {row[1] for row in conn.execute("PRAGMA table_info(activity_log)")}
    if "ref_id" not in log_cols:
        conn.execute("ALTER TABLE activity_log ADD COLUMN ref_id INTEGER NOT NULL DEFAULT 0")


# Имена входа. Пароли — только из .env при первом создании учётки (не коммитятся).
DEFAULT_USERS = (
    ("Никита Карпенко-Черников", "NIKITA_PASSWORD"),
    ("Александр Зинкевич", "ZINKEVICH_PASSWORD"),
    ("Артем Голомолзин", "GOLOMOLZIN_PASSWORD"),
)

# старое короткое имя → полное (логин в селекте на /login)
USER_RENAMES = (
    ("Никита", "Никита Карпенко-Черников"),
    ("Александр", "Александр Зинкевич"),
)


def _ensure_users(conn) -> None:
    """Создать недостающих пользователей; переименовать короткие логины."""
    for old, new in USER_RENAMES:
        legacy = conn.execute("SELECT id FROM users WHERE name = ?", (old,)).fetchone()
        if not legacy:
            continue
        taken = conn.execute("SELECT id FROM users WHERE name = ?", (new,)).fetchone()
        if taken:
            conn.execute("DELETE FROM users WHERE id = ?", (legacy["id"],))
        else:
            conn.execute(
                "UPDATE users SET name = ? WHERE id = ?",
                (new, legacy["id"]),
            )

    for name, env_key in DEFAULT_USERS:
        row = conn.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
        if row:
            continue
        password = (os.environ.get(env_key) or os.environ.get("DEFAULT_USER_PASSWORD") or "").strip()
        if not password:
            # одноразовый; задайте пароль в .env до первого запуска или смените в Кабинете
            password = secrets.token_urlsafe(12)
        conn.execute(
            "INSERT INTO users (name, password) VALUES (?, ?)",
            (name, hash_password(password)),
        )


def ensure_workbook():
    if WORKBOOK.exists() or not TELEGRAM_WORKBOOK.exists():
        return
    DATA.mkdir(exist_ok=True)
    shutil.copy(TELEGRAM_WORKBOOK, WORKBOOK)


def user_by_name(name):
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE name = ?", (name,)).fetchone()
    return dict(row) if row else None


def user_by_id(user_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT id, name FROM users ORDER BY id").fetchall()
    return [dict(row) for row in rows]


def change_password(user_id, current, new):
    user = user_by_id(user_id)
    if user is None or not check_password(current, user["password"]):
        return "current"
    if not new:
        return "empty"
    with connect() as conn:
        conn.execute("UPDATE users SET password = ? WHERE id = ?", (hash_password(new), user_id))
    return "ok"


def clean(value):
    if value is None:
        return ""
    return str(value).replace("\xa0", " ").strip()


def _rows(ws):
    raw_rows = list(ws.iter_rows(values_only=True))
    if not raw_rows:
        return []
    header = [clean(cell) for cell in raw_rows[0]]
    index = {name: pos for pos, name in enumerate(header)}
    result = []
    for raw in raw_rows[1:]:
        if not any(cell not in (None, "") for cell in raw):
            continue
        item = {}
        for name, pos in index.items():
            item[name] = clean(raw[pos]) if pos < len(raw) else ""
        result.append(item)
    return result


def _import_workbook(conn, path):
    book = load_workbook(path, data_only=True, read_only=True)
    schools = _rows(book["Школы"]) if "Школы" in book.sheetnames else []
    centers = _rows(book["Центры"]) if "Центры" in book.sheetnames else []
    book.close()
    _import_schools(conn, schools)
    _import_centers(conn, centers)


def _append_unique(existing, extra):
    extra = extra.strip()
    if not extra or extra in existing:
        return existing
    return f"{existing}\n\n{extra}" if existing else extra


def _import_schools(conn, rows):
    found = {}
    for row in rows:
        name = row.get("Школа", "")
        team = row.get("Команда", "")
        key = (row.get("Регион", "").casefold(), row.get("Город", "").casefold(), name.casefold() or f"team:{team.casefold()}")
        org_id = found.get(key)
        if org_id is None:
            org_id = conn.execute(
                """
                INSERT INTO orgs (kind, region, city, name, letter_name, coverage, phone, address, note)
                VALUES (?, ?, ?, ?, ?, '', ?, ?, ?)
                """,
                (
                    KIND_SCHOOL,
                    row.get("Регион", ""),
                    row.get("Город", ""),
                    name,
                    row.get("Название для письма", ""),
                    row.get("Телефон", ""),
                    row.get("Адрес", ""),
                    row.get("Заметка", ""),
                ),
            ).lastrowid
            found[key] = org_id
        else:
            current = conn.execute(
                "SELECT letter_name, phone, note, address FROM orgs WHERE id = ?", (org_id,)
            ).fetchone()
            conn.execute(
                "UPDATE orgs SET letter_name = ?, phone = ?, note = ?, address = ? WHERE id = ?",
                (
                    current["letter_name"] or row.get("Название для письма", ""),
                    _append_unique(current["phone"], row.get("Телефон", "")),
                    _append_unique(current["note"], row.get("Заметка", "")),
                    current["address"] or row.get("Адрес", ""),
                    org_id,
                ),
            )
        _add_person(conn, org_id, "Директор", row.get("ФИО директора", ""), row.get("Обращение директору", ""), row.get("Почта директора", ""), "")
        _add_person(
            conn,
            org_id,
            "Руководитель",
            row.get("ФИО руководителя", ""),
            row.get("Обращение руководителю", ""),
            row.get("Почта руководителя", ""),
            "",
        )
        if team:
            have = {
                item["name"].casefold()
                for item in conn.execute("SELECT name FROM teams WHERE org_id = ?", (org_id,))
            }
            if team.casefold() not in have:
                conn.execute(
                    "INSERT INTO teams (org_id, name, participation) VALUES (?, ?, ?)",
                    (org_id, team, row.get("Участие", "")),
                )


def _import_centers(conn, rows):
    found = {}
    for row in rows:
        name = row.get("Центр", "")
        if not name and not row.get("ФИО"):
            continue
        key = (row.get("Регион", "").casefold(), row.get("Город", "").casefold(), name.casefold())
        org_id = found.get(key)
        if org_id is None:
            org_id = conn.execute(
                """
                INSERT INTO orgs (kind, region, city, name, letter_name, coverage, phone, note)
                VALUES (?, ?, ?, ?, ?, ?, '', ?)
                """,
                (
                    KIND_CENTER,
                    row.get("Регион", ""),
                    row.get("Город", ""),
                    name,
                    row.get("Название для письма", ""),
                    row.get("Охват", ""),
                    row.get("Заметка", ""),
                ),
            ).lastrowid
            found[key] = org_id
        else:
            current = conn.execute(
                "SELECT letter_name, coverage, note FROM orgs WHERE id = ?", (org_id,)
            ).fetchone()
            conn.execute(
                "UPDATE orgs SET letter_name = ?, coverage = ?, note = ? WHERE id = ?",
                (
                    current["letter_name"] or row.get("Название для письма", ""),
                    current["coverage"] or row.get("Охват", ""),
                    _append_unique(current["note"], row.get("Заметка", "")),
                    org_id,
                ),
            )
        _add_person(
            conn,
            org_id,
            row.get("Роль", ""),
            row.get("ФИО", ""),
            row.get("Обращение", ""),
            row.get("Почта", ""),
            row.get("Телефон", ""),
        )


def _add_person(conn, org_id, role, full_name, greeting, email, phone):
    if not (full_name or email or greeting or role):
        return
    key = (role.casefold(), full_name.casefold(), email.casefold())
    have = {
        (item["role"].casefold(), item["full_name"].casefold(), item["email"].casefold())
        for item in conn.execute("SELECT role, full_name, email FROM people WHERE org_id = ?", (org_id,))
    }
    if key in have:
        return
    conn.execute(
        "INSERT INTO people (org_id, role, full_name, greeting, email, phone) VALUES (?, ?, ?, ?, ?, ?)",
        (org_id, role, full_name, greeting, email, phone),
    )


def _graph(kind, *, include_deleted: bool = False):
    with connect() as conn:
        if include_deleted:
            sql = "SELECT * FROM orgs WHERE kind = ? ORDER BY id"
        else:
            sql = "SELECT * FROM orgs WHERE kind = ? AND IFNULL(deleted_at, '') = '' ORDER BY id"
        orgs = [dict(row) for row in conn.execute(sql, (kind,))]
        people = [dict(row) for row in conn.execute("SELECT * FROM people ORDER BY id")]
        teams = [dict(row) for row in conn.execute("SELECT * FROM teams ORDER BY id")]
    by_people = {}
    by_teams = {}
    for person in people:
        by_people.setdefault(person["org_id"], []).append(person)
    for team in teams:
        by_teams.setdefault(team["org_id"], []).append(team)
    for org in orgs:
        org["people"] = by_people.get(org["id"], [])
        org["teams"] = by_teams.get(org["id"], [])
        org["is_deleted"] = bool(clean(org.get("deleted_at")))
    return orgs


def _blob(org):
    parts = [
        org["name"],
        org["letter_name"],
        org["region"],
        org["city"],
        org["coverage"],
        org["phone"],
        org.get("address", ""),
        org["note"],
    ]
    for team in org["teams"]:
        parts.extend([team["name"], team["participation"]])
    for person in org["people"]:
        parts.extend([person["full_name"], person["role"], person["email"], person["phone"], person["greeting"]])
    return " ".join(part for part in parts if part)


def _norm(text):
    text = (text or "").casefold().replace("ё", "е")
    text = text.replace("№", " ").replace("#", " ")
    text = re.sub(r"[«»\"'`.,;:()\[\]{}]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _matches(org, query):
    query = (query or "").strip()
    if not query:
        return True
    blob = _norm(_blob(org))
    tokens = [token for token in _norm(query).split() if token]
    if not tokens:
        return True
    return all(token in blob for token in tokens)


def schools(query):
    rows = []
    for org in _graph(KIND_SCHOOL):
        if not _matches(org, query):
            continue
        director = next((person for person in org["people"] if person["role"] == "Директор"), None)
        coach = next((person for person in org["people"] if person["role"] == "Руководитель"), None)
        rows.append({**org, "director": director, "coach": coach})
    rows.sort(key=lambda org: (
        not org["region"],
        org["region"].casefold(),
        not org["city"],
        org["city"].casefold(),
        (org["name"] or (org["teams"][0]["name"] if org["teams"] else "")).casefold(),
    ))
    return rows


def centers(query):
    rows = []
    for org in _graph(KIND_CENTER):
        if not _matches(org, query):
            continue
        people = org["people"] or [None]
        for person in people:
            rows.append({**org, "person": person})
    rows.sort(
        key=lambda org: (
            not org["region"],
            org["region"].casefold(),
            not org["city"],
            org["city"].casefold(),
            org["name"].casefold(),
            ((org["person"] or {}).get("full_name") or "").casefold(),
        )
    )
    return rows


SCHOOL_EXCEL_HEADERS = [
    "Регион",
    "Город",
    "Школа",
    "Адрес",
    "Название для письма",
    "Телефон",
    "Заметка",
    "ФИО директора",
    "Обращение директору",
    "Почта директора",
    "ФИО руководителя",
    "Обращение руководителю",
    "Почта руководителя",
    "Команда",
    "Участие",
]


def all_regions() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT region FROM orgs
            WHERE region IS NOT NULL AND region <> '' AND IFNULL(deleted_at, '') = ''
            ORDER BY region COLLATE NOCASE
            """
        ).fetchall()
    return [row["region"] for row in rows]


def create_center(
    *,
    region: str = "",
    city: str = "",
    name: str = "",
    address: str = "",
    letter_name: str = "",
    coverage: str = "",
    phone: str = "",
    note: str = "",
    person_name: str = "",
    person_role: str = "",
    person_greeting: str = "",
    person_email: str = "",
    person_phone: str = "",
) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Укажите название центра")
    with connect() as conn:
        org_id = conn.execute(
            """
            INSERT INTO orgs (kind, region, city, name, letter_name, coverage, phone, address, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                KIND_CENTER,
                (region or "").strip(),
                (city or "").strip(),
                name,
                (letter_name or "").strip(),
                (coverage or "").strip(),
                (phone or "").strip(),
                (address or "").strip(),
                (note or "").strip(),
            ),
        ).lastrowid
        _add_person(
            conn,
            org_id,
            (person_role or "").strip() or "Контакт",
            (person_name or "").strip(),
            (person_greeting or "").strip(),
            (person_email or "").strip(),
            (person_phone or "").strip(),
        )
    return org_id


def create_school(
    *,
    region: str = "",
    city: str = "",
    name: str = "",
    address: str = "",
    letter_name: str = "",
    phone: str = "",
    note: str = "",
    director_name: str = "",
    director_greeting: str = "",
    director_email: str = "",
    coach_name: str = "",
    coach_greeting: str = "",
    coach_email: str = "",
    team: str = "",
    participation: str = "",
) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Укажите название школы")
    with connect() as conn:
        org_id = conn.execute(
            """
            INSERT INTO orgs (kind, region, city, name, letter_name, coverage, phone, address, note)
            VALUES (?, ?, ?, ?, ?, '', ?, ?, ?)
            """,
            (
                KIND_SCHOOL,
                (region or "").strip(),
                (city or "").strip(),
                name,
                (letter_name or "").strip(),
                (phone or "").strip(),
                (address or "").strip(),
                (note or "").strip(),
            ),
        ).lastrowid
        _add_person(
            conn,
            org_id,
            "Директор",
            (director_name or "").strip(),
            (director_greeting or "").strip(),
            (director_email or "").strip(),
            "",
        )
        _add_person(
            conn,
            org_id,
            "Руководитель",
            (coach_name or "").strip(),
            (coach_greeting or "").strip(),
            (coach_email or "").strip(),
            "",
        )
        team = (team or "").strip()
        if team:
            conn.execute(
                "INSERT INTO teams (org_id, name, participation) VALUES (?, ?, ?)",
                (org_id, team, (participation or "").strip()),
            )
    return org_id


def import_schools_rows(rows: list[dict]) -> dict:
    """Импорт строк с ключами как в SCHOOL_EXCEL_HEADERS. Возвращает {ok, skip, error}."""
    prepared = []
    for row in rows:
        item = {key: clean(row.get(key, "")) for key in SCHOOL_EXCEL_HEADERS}
        if not item["Школа"] and not item["Команда"]:
            continue
        # не импортировать строку-пример из шаблона
        blob = " ".join(item.values()).casefold()
        if "удалите эту строку" in blob or "удалите перед загрузкой" in blob:
            continue
        prepared.append(item)
    if not prepared:
        return {"ok": 0, "skip": 0, "error": "В файле нет строк с школами"}
    before = len(_graph(KIND_SCHOOL))
    with connect() as conn:
        # address column support on top of classic import
        found = {}
        ok = 0
        for row in prepared:
            name = row.get("Школа", "")
            team = row.get("Команда", "")
            key = (
                row.get("Регион", "").casefold(),
                row.get("Город", "").casefold(),
                name.casefold() or f"team:{team.casefold()}",
            )
            org_id = found.get(key)
            if org_id is None:
                # try existing
                existing = conn.execute(
                    """
                    SELECT id FROM orgs
                    WHERE kind = ? AND lower(region) = ? AND lower(city) = ? AND lower(name) = ?
                      AND IFNULL(deleted_at, '') = ''
                    """,
                    (KIND_SCHOOL, row.get("Регион", "").casefold(), row.get("Город", "").casefold(), name.casefold()),
                ).fetchone()
                if existing and name:
                    org_id = existing["id"]
                    found[key] = org_id
                    cur = conn.execute(
                        "SELECT letter_name, phone, note, address FROM orgs WHERE id = ?",
                        (org_id,),
                    ).fetchone()
                    conn.execute(
                        "UPDATE orgs SET letter_name = ?, phone = ?, note = ?, address = ? WHERE id = ?",
                        (
                            cur["letter_name"] or row.get("Название для письма", ""),
                            _append_unique(cur["phone"], row.get("Телефон", "")),
                            _append_unique(cur["note"], row.get("Заметка", "")),
                            cur["address"] or row.get("Адрес", ""),
                            org_id,
                        ),
                    )
                else:
                    org_id = conn.execute(
                        """
                        INSERT INTO orgs (kind, region, city, name, letter_name, coverage, phone, address, note)
                        VALUES (?, ?, ?, ?, ?, '', ?, ?, ?)
                        """,
                        (
                            KIND_SCHOOL,
                            row.get("Регион", ""),
                            row.get("Город", ""),
                            name,
                            row.get("Название для письма", ""),
                            row.get("Телефон", ""),
                            row.get("Адрес", ""),
                            row.get("Заметка", ""),
                        ),
                    ).lastrowid
                    found[key] = org_id
                    ok += 1
            else:
                cur = conn.execute(
                    "SELECT letter_name, phone, note, address FROM orgs WHERE id = ?",
                    (org_id,),
                ).fetchone()
                conn.execute(
                    "UPDATE orgs SET letter_name = ?, phone = ?, note = ?, address = ? WHERE id = ?",
                    (
                        cur["letter_name"] or row.get("Название для письма", ""),
                        _append_unique(cur["phone"], row.get("Телефон", "")),
                        _append_unique(cur["note"], row.get("Заметка", "")),
                        cur["address"] or row.get("Адрес", ""),
                        org_id,
                    ),
                )
            _add_person(
                conn,
                org_id,
                "Директор",
                row.get("ФИО директора", ""),
                row.get("Обращение директору", ""),
                row.get("Почта директора", ""),
                "",
            )
            _add_person(
                conn,
                org_id,
                "Руководитель",
                row.get("ФИО руководителя", ""),
                row.get("Обращение руководителю", ""),
                row.get("Почта руководителя", ""),
                "",
            )
            if team:
                have = {
                    item["name"].casefold()
                    for item in conn.execute("SELECT name FROM teams WHERE org_id = ?", (org_id,))
                }
                if team.casefold() not in have:
                    conn.execute(
                        "INSERT INTO teams (org_id, name, participation) VALUES (?, ?, ?)",
                        (org_id, team, row.get("Участие", "")),
                    )
    after = len(_graph(KIND_SCHOOL))
    return {"ok": max(ok, after - before), "skip": max(0, len(prepared) - ok), "error": ""}


def import_schools_xlsx(data: bytes) -> dict:
    from io import BytesIO

    book = load_workbook(BytesIO(data), data_only=True, read_only=True)
    sheet_name = "Школы" if "Школы" in book.sheetnames else book.sheetnames[0]
    rows = _rows(book[sheet_name])
    book.close()
    return import_schools_rows(rows)


def schools_template_xlsx() -> bytes:
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    book = Workbook()
    sheet = book.active
    sheet.title = "Школы"
    sheet.append(SCHOOL_EXCEL_HEADERS)
    # пример-подсказка второй строкой (стереть перед импортом)
    sheet.append(
        [
            "Примерная область",
            "Примерск",
            "МАОУ Примерная школа №1 (удалите эту строку)",
            "000000, г. Примерск, ул. Примерная, 1",
            "",
            "+7 (000) 000-00-00",
            "строка-пример: удалите перед загрузкой",
            "Иванов Иван Иванович",
            "Уважаемый Иван Иванович",
            "director@example.ru",
            "",
            "",
            "coach@example.ru",
            "Команда «Пример»",
            "сезон 2027",
        ]
    )
    for idx in range(1, len(SCHOOL_EXCEL_HEADERS) + 1):
        sheet.column_dimensions[get_column_letter(idx)].width = 22
    buf = BytesIO()
    book.save(buf)
    return buf.getvalue()


ORG_EDITABLE = {
    "name",
    "letter_name",
    "region",
    "city",
    "address",
    "phone",
    "note",
    "coverage",
}

ORG_FIELD_LABELS = {
    "name": "Название",
    "letter_name": "Для письма",
    "region": "Регион",
    "city": "Город",
    "address": "Адрес",
    "phone": "Телефон",
    "note": "Заметка",
    "coverage": "Охват",
}

PERSON_EDITABLE = {"full_name", "greeting", "email", "phone", "role"}

PERSON_FIELD_LABELS = {
    "full_name": "ФИО",
    "greeting": "Обращение",
    "email": "Почта",
    "phone": "Телефон",
    "role": "Роль",
}


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _short(value, limit=90):
    text = clean(value)
    if not text:
        return "—"
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _change_line(label: str, old, new) -> str | None:
    before = clean(old)
    after = clean(new)
    if before == after:
        return None
    return f"{label}: {_short(before)} → {_short(after)}"


ACTIVITY_KIND_LABEL = {
    "org_edit": "Правка карточки",
    "org_create": "Создание",
    "org_import": "Импорт",
    "org_delete": "Удаление",
    "org_restore": "Восстановление",
    "mailing_create": "Рассылка создана",
    "mailing_delete": "Рассылка удалена",
    "mailing_settings": "Настройки рассылки",
    "mailing_add": "Получатели",
    "mailing_generate": "Сборка PDF",
    "mailing_send": "Рассылка отправлена",
    "letter_sent": "Отправлено",
    "letter_send": "Письмо ушло",
    "letter_send_fail": "Ошибка отправки",
    "letter_reply": "Ответ",
    "letter_note": "Заметка",
    "letter_delete": "Письмо удалено",
    "mail_account": "Почтовый ящик",
}


def log_activity(
    *,
    by_user: str,
    kind: str,
    detail: str,
    href: str = "",
    ref_id: int = 0,
    at: str | None = None,
    conn=None,
) -> None:
    """Глобальный журнал действий (все пользователи, все сущности)."""
    detail = (detail or "").strip()
    if not detail:
        return
    row = (
        at or _now(),
        clean(by_user),
        clean(kind) or "note",
        detail,
        (href or "").strip(),
        int(ref_id or 0),
    )
    sql = (
        "INSERT INTO activity_log (at, by_user, kind, detail, href, ref_id) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    if conn is not None:
        conn.execute(sql, row)
        return
    with connect() as c:
        c.execute(sql, row)


def list_activity(limit: int = 300, offset: int = 0) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, at, by_user, kind, detail, href, ref_id
            FROM activity_log
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
        deleted_ids = {
            row["id"]
            for row in conn.execute(
                "SELECT id FROM orgs WHERE IFNULL(deleted_at, '') <> ''"
            )
        }
    out = []
    for row in rows:
        item = dict(row)
        item["kind_label"] = ACTIVITY_KIND_LABEL.get(item["kind"], item["kind"] or "—")
        ref = int(item.get("ref_id") or 0)
        item["can_restore"] = item["kind"] == "org_delete" and ref in deleted_ids
        out.append(item)
    return out


def soft_delete_org(org_id: int, by_user: str = "") -> bool:
    org = org_by_id(org_id)
    if not org or org.get("is_deleted"):
        return False
    at = _now()
    who = clean(by_user)
    word = "Школа" if org["kind"] == KIND_SCHOOL else "Центр"
    name = clean(org.get("name")) or f"#{org_id}"
    with connect() as conn:
        conn.execute("UPDATE orgs SET deleted_at = ? WHERE id = ?", (at, org_id))
        conn.execute(
            "INSERT INTO org_edits (org_id, at, by_user, detail) VALUES (?, ?, ?, ?)",
            (org_id, at, who, "Удалено"),
        )
        log_activity(
            by_user=who,
            kind="org_delete",
            detail=f"{word}: {name}",
            href=f"/orgs/{org_id}",
            ref_id=org_id,
            at=at,
            conn=conn,
        )
    return True


def restore_org(org_id: int, by_user: str = "") -> bool:
    org = org_by_id(org_id)
    if not org or not org.get("is_deleted"):
        return False
    at = _now()
    who = clean(by_user)
    word = "Школа" if org["kind"] == KIND_SCHOOL else "Центр"
    name = clean(org.get("name")) or f"#{org_id}"
    with connect() as conn:
        conn.execute("UPDATE orgs SET deleted_at = '' WHERE id = ?", (org_id,))
        conn.execute(
            "INSERT INTO org_edits (org_id, at, by_user, detail) VALUES (?, ?, ?, ?)",
            (org_id, at, who, "Восстановлено"),
        )
        log_activity(
            by_user=who,
            kind="org_restore",
            detail=f"{word}: {name}",
            href=f"/orgs/{org_id}",
            ref_id=org_id,
            at=at,
            conn=conn,
        )
    return True


def append_org_edit(org_id: int, by_user: str, detail: str) -> None:
    detail = (detail or "").strip()
    if not detail:
        return
    with connect() as conn:
        at = _now()
        who = clean(by_user)
        conn.execute(
            "INSERT INTO org_edits (org_id, at, by_user, detail) VALUES (?, ?, ?, ?)",
            (org_id, at, who, detail),
        )
        org = conn.execute(
            "SELECT name, kind FROM orgs WHERE id = ?", (org_id,)
        ).fetchone()
        if org:
            word = "Школа" if org["kind"] == KIND_SCHOOL else "Центр"
            name = clean(org["name"]) or f"#{org_id}"
            log_activity(
                by_user=who,
                kind="org_edit",
                detail=f"{word}: {name}\n{detail}",
                href=f"/orgs/{org_id}",
                ref_id=org_id,
                at=at,
                conn=conn,
            )


def org_edits(org_id: int, limit: int = 80) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, at, by_user, detail
            FROM org_edits
            WHERE org_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (org_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def _person_by_role(org: dict, role: str) -> dict:
    for person in org.get("people") or []:
        if person.get("role") == role:
            return person
    return {}


def _teams_label(org: dict) -> str:
    parts = []
    for team in org.get("teams") or []:
        name = clean(team.get("name"))
        if not name:
            continue
        part = clean(team.get("participation"))
        parts.append(f"{name} ({part})" if part else name)
    return "; ".join(parts)


def log_org_diff(org_id: int, by_user: str, before: dict, after: dict | None = None) -> None:
    """Сравнить два снимка org_by_id и записать человекочитаемые правки."""
    if not before:
        return
    if after is None:
        after = org_by_id(org_id)
    if not after:
        return
    lines: list[str] = []
    for key, label in ORG_FIELD_LABELS.items():
        line = _change_line(label, before.get(key), after.get(key))
        if line:
            lines.append(line)

    kind = after.get("kind") or before.get("kind")
    if kind == KIND_SCHOOL:
        people_slots = [
            (_person_by_role(before, "Директор"), _person_by_role(after, "Директор"), "Директор"),
            (_person_by_role(before, "Руководитель"), _person_by_role(after, "Руководитель"), "Руководитель"),
        ]
    else:
        old_p = (before.get("people") or [None])[0] or {}
        new_p = (after.get("people") or [None])[0] or {}
        people_slots = [(old_p, new_p, "Контакт")]

    for old_p, new_p, prefix in people_slots:
        for key, label in PERSON_FIELD_LABELS.items():
            if key == "role" and kind == KIND_SCHOOL:
                continue
            line = _change_line(f"{prefix} · {label}", old_p.get(key), new_p.get(key))
            if line:
                lines.append(line)

    if kind == KIND_SCHOOL:
        line = _change_line("Команды", _teams_label(before), _teams_label(after))
        if line:
            lines.append(line)

    if lines:
        append_org_edit(org_id, by_user, "\n".join(lines))


def org_by_id(org_id: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM orgs WHERE id = ?", (org_id,)).fetchone()
        if not row:
            return None
        org = dict(row)
        org["people"] = [
            dict(p)
            for p in conn.execute(
                "SELECT * FROM people WHERE org_id = ? ORDER BY id", (org_id,)
            )
        ]
        org["teams"] = [
            dict(t)
            for t in conn.execute(
                "SELECT * FROM teams WHERE org_id = ? ORDER BY id", (org_id,)
            )
        ]
    org["is_deleted"] = bool(clean(org.get("deleted_at")))
    if org["kind"] == KIND_SCHOOL:
        org["director"] = next((p for p in org["people"] if p["role"] == "Директор"), None)
        org["coach"] = next((p for p in org["people"] if p["role"] == "Руководитель"), None)
    return org


def update_org(org_id: int, fields: dict) -> bool:
    parts = []
    values = []
    for key, value in fields.items():
        if key not in ORG_EDITABLE:
            continue
        parts.append(f"{key} = ?")
        values.append(clean(value))
    if not parts:
        return False
    values.append(org_id)
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE orgs SET {', '.join(parts)} WHERE id = ? AND IFNULL(deleted_at, '') = ''",
            values,
        )
        return cur.rowcount > 0


def upsert_person(org_id: int, role: str, fields: dict) -> int:
    role = clean(role) or "Контакт"
    with connect() as conn:
        row = None
        if role:
            row = conn.execute(
                "SELECT * FROM people WHERE org_id = ? AND role = ? ORDER BY id LIMIT 1",
                (org_id, role),
            ).fetchone()
        data = {key: clean(fields.get(key, "")) for key in PERSON_EDITABLE if key in fields}
        if row is None and fields.get("person_id"):
            try:
                pid = int(fields["person_id"])
            except (TypeError, ValueError):
                pid = 0
            if pid:
                row = conn.execute(
                    "SELECT * FROM people WHERE id = ? AND org_id = ?",
                    (pid, org_id),
                ).fetchone()
        if row is None:
            full_name = data.get("full_name", "")
            greeting = data.get("greeting", "")
            email = data.get("email", "")
            phone = data.get("phone", "")
            if not (full_name or greeting or email or phone or role):
                return 0
            cur = conn.execute(
                """
                INSERT INTO people (org_id, role, full_name, greeting, email, phone)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (org_id, role, full_name, greeting, email, phone),
            )
            return cur.lastrowid
        sets = []
        values = []
        for key, value in data.items():
            if key == "role":
                continue
            sets.append(f"{key} = ?")
            values.append(value)
        if "role" in data and data["role"]:
            sets.append("role = ?")
            values.append(data["role"])
        if not sets:
            return row["id"]
        values.append(row["id"])
        conn.execute(f"UPDATE people SET {', '.join(sets)} WHERE id = ?", values)
        return row["id"]


def _guess_greeting(full_name: str) -> str:
    parts = [p for p in (full_name or "").split() if p]
    if len(parts) < 2:
        return ""
    from app import mailings as mailings_mod

    return mailings_mod._personal_greeting(full_name, "")


def set_person_field(org_id: int, role: str, field: str, value: str) -> bool:
    if field not in PERSON_EDITABLE:
        return False
    payload = {field: value}
    # если вписали ФИО, а обращения ещё нет — собрать «Уважаемый/ая …»
    if field == "full_name" and clean(value):
        with connect() as conn:
            row = conn.execute(
                "SELECT greeting FROM people WHERE org_id = ? AND role = ? ORDER BY id LIMIT 1",
                (org_id, role),
            ).fetchone()
        if not row or not (row["greeting"] or "").strip():
            guessed = _guess_greeting(value)
            if guessed:
                payload["greeting"] = guessed.rstrip(",")
    upsert_person(org_id, role, payload)
    return True


def replace_teams(org_id: int, teams: list[dict]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM teams WHERE org_id = ?", (org_id,))
        for team in teams:
            name = clean(team.get("name", ""))
            if not name:
                continue
            conn.execute(
                "INSERT INTO teams (org_id, name, participation) VALUES (?, ?, ?)",
                (org_id, name, clean(team.get("participation", ""))),
            )


def quick_set(org_id: int, field: str, value: str, role: str = "", by_user: str = "") -> bool:
    """Быстрая правка из таблицы: org.* или person через role+field."""
    org = org_by_id(org_id)
    if not org or org.get("is_deleted"):
        return False
    ok = False
    if field in ORG_EDITABLE:
        ok = update_org(org_id, {field: value})
    elif field in PERSON_EDITABLE:
        use_role = clean(role)
        if not use_role:
            if org["kind"] == KIND_SCHOOL:
                use_role = "Директор"
            else:
                people = org.get("people") or []
                use_role = (people[0]["role"] if people else "") or "Контакт"
        ok = set_person_field(org_id, use_role, field, value)
    else:
        # составные ключи из таблицы
        aliases = {
            "director_name": ("Директор", "full_name"),
            "director_email": ("Директор", "email"),
            "director_greeting": ("Директор", "greeting"),
            "coach_name": ("Руководитель", "full_name"),
            "coach_email": ("Руководитель", "email"),
            "coach_greeting": ("Руководитель", "greeting"),
            "person_name": ("", "full_name"),
            "person_email": ("", "email"),
            "person_phone": ("", "phone"),
            "person_role": ("", "role"),
            "person_greeting": ("", "greeting"),
        }
        if field in aliases:
            use_role, person_field = aliases[field]
            if not use_role:
                people = org.get("people") or []
                use_role = (people[0]["role"] if people else "") or "Контакт"
                if field == "person_role" and people:
                    # обновляем роль первой записи
                    with connect() as conn:
                        conn.execute(
                            "UPDATE people SET role = ? WHERE id = ?",
                            (clean(value), people[0]["id"]),
                        )
                    ok = True
            if not ok:
                ok = set_person_field(org_id, use_role, person_field, value)
    if ok:
        log_org_diff(org_id, by_user, org)
    return ok
