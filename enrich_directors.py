"""Добор ФИО директоров школ. Не выдумывает имена — только из найденных источников."""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path

from app import db

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

FIO_RE = re.compile(
    r"\b([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:ич|на|кызы|оглы)?)\b"
)
DIR_NEAR_RE = re.compile(
    r"(?:директор(?:\s+(?:школы|лицея|гимназии|организации))?|руководитель(?:\s+образовательной\s+организации)?)"
    r"\s*[:\-–—]?\s*"
    r"([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:ич|на)?)",
    re.I,
)
BAD_FIO = (
    "российская федерация",
    "свердловская область",
    "воронежская область",
    "московская область",
    "проверьте правильность",
    "муниципальное автономное",
    "муниципальное бюджетное",
    "государственное бюджетное",
)


def curl(url: str, timeout: int = 10) -> str:
    try:
        proc = subprocess.run(
            ["curl", "-sL", "--max-time", str(timeout), "-A", UA, "--compressed", url],
            capture_output=True,
            timeout=timeout + 3,
        )
        return proc.stdout.decode("utf-8", "ignore")
    except Exception:
        return ""


def strip_html(html: str) -> str:
    html = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    html = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", html)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;|&#160;|&amp;", " ", html)
    return re.sub(r"\s+", " ", html)


def clean_fio(name: str) -> str:
    name = re.sub(r"\s+", " ", (name or "").strip(" .,:;"))
    if not name or len(name) < 8 or len(name) > 60:
        return ""
    if name.casefold() in BAD_FIO:
        return ""
    if not FIO_RE.fullmatch(name):
        # tolerate slight trailing junk
        m = FIO_RE.search(name)
        if not m:
            return ""
        name = m.group(1)
    parts = name.split()
    if len(parts) != 3:
        return ""
    # reject if looks like place names
    if any(p.casefold().endswith(("ская", "цкий", "цкий")) for p in parts[:1]):
        if parts[0].casefold().endswith(("ова", "ева", "ина", "ская")):
            pass
        elif parts[0].casefold().endswith(("ская",)):
            return ""
    return name


def extract_director(text: str) -> str:
    text = text or ""
    for rx in (DIR_NEAR_RE,):
        for m in rx.finditer(text):
            fio = clean_fio(m.group(1))
            if fio:
                return fio
    # table-like: Директор ... FIO on same line-ish
    for m in re.finditer(r"Директор.{0,40}?([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+)", text, re.I | re.S):
        fio = clean_fio(m.group(1))
        if fio:
            return fio
    return ""


def greeting_for(fio: str) -> str:
    parts = fio.split()
    if len(parts) < 3:
        return ""
    name_pat = f"{parts[1]} {parts[2]}"
    pat = parts[2].casefold()
    female = pat.endswith(("овна", "евна", "ична", "инична"))
    word = "Уважаемая" if female else "Уважаемый"
    return f"{word} {name_pat}!"


def set_director_name(conn, org_id: int, fio: str) -> bool:
    fio = clean_fio(fio)
    if not fio:
        return False
    greet = greeting_for(fio)
    people = list(conn.execute("SELECT * FROM people WHERE org_id=? AND role='Директор'", (org_id,)))
    for p in people:
        if (p["full_name"] or "").strip():
            return False
    if people:
        p = people[0]
        conn.execute(
            "UPDATE people SET full_name=?, greeting=COALESCE(NULLIF(greeting,''), ?) WHERE id=?",
            (fio, greet, p["id"]),
        )
        return True
    conn.execute(
        "INSERT INTO people (org_id, role, full_name, greeting, email, phone) VALUES (?, 'Директор', ?, ?, '', '')",
        (org_id, fio, greet),
    )
    return True


def apply_json(path: Path) -> tuple[int, int]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    applied = skipped = 0
    with db.connect() as conn:
        for row in rows:
            org_id = int(row["id"])
            fio = (row.get("director") or row.get("full_name") or "").strip()
            if not fio:
                skipped += 1
                continue
            # skip if already has name
            have = conn.execute(
                "SELECT full_name FROM people WHERE org_id=? AND role='Директор' AND full_name<>''",
                (org_id,),
            ).fetchone()
            if have:
                skipped += 1
                continue
            if set_director_name(conn, org_id, fio):
                applied += 1
                print("+", org_id, fio, flush=True)
            else:
                skipped += 1
    return applied, skipped


def missing_schools():
    out = []
    for s in db.schools(""):
        d = s.get("director") or {}
        if (d.get("full_name") or "").strip():
            continue
        email = (d.get("email") or "") or ((s.get("coach") or {}).get("email") or "")
        out.append(
            {
                "id": s["id"],
                "region": s["region"],
                "city": s["city"],
                "name": s["name"],
                "email": email,
            }
        )
    return out


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "apply":
        applied, skipped = apply_json(Path(sys.argv[2]))
        print(f"applied {applied}, skipped {skipped}")
    else:
        rows = missing_schools()
        DATA.mkdir(exist_ok=True)
        path = DATA / "missing_directors.json"
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"missing {len(rows)} -> {path}")
