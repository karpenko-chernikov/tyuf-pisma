#!/usr/bin/env python3
"""Идемпотентно добавляет тульские школы/команды из ТЮФ по школам + МТИ."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402

DATA = Path(__file__).with_name("tula_tyuf_2026.json")

STOP = {
    "мбоу",
    "маоу",
    "сош",
    "лицей",
    "гимназия",
    "школа",
    "имени",
    "имени",
    "г",
    "город",
    "область",
    "тульская",
    "муниципальное",
    "бюджетное",
    "общеобразовательное",
    "учреждение",
    "средняя",
}


def _norm(s: str) -> str:
    s = (s or "").casefold().replace("ё", "е")
    s = s.replace("№", " ").replace("#", " ")
    s = re.sub(r"[«»\"'`.,;:()\[\]{}—–-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokens(s: str) -> set[str]:
    return {t for t in _norm(s).split() if t and t not in STOP and not t.isdigit()}


def _same_city(a: str, b: str) -> bool:
    a = (a or "").casefold().replace("ё", "е").replace("г.", "").strip()
    b = (b or "").casefold().replace("ё", "е").replace("г.", "").strip()
    if not a or not b:
        return True
    return a == b or a in b or b in a


def _find(conn, item: dict):
    kind = item["kind"]
    city = item.get("city") or ""
    name_n = _norm(item["name"])
    letter_n = _norm(item.get("letter_name") or "")
    want_tok = _tokens(item["name"] + " " + (item.get("letter_name") or ""))
    emails = {
        (p.get("email") or "").casefold()
        for p in item.get("people") or []
        if (p.get("email") or "").strip()
    }

    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM orgs WHERE kind = ? AND IFNULL(deleted_at, '') = ''",
            (kind,),
        )
    ]

    # 1) exact name / letter in same city
    for row in rows:
        if not _same_city(city, row["city"]):
            continue
        rn = _norm(row["name"])
        rl = _norm(row["letter_name"] or "")
        if rn == name_n or (letter_n and rl == letter_n):
            return row["id"]

    # 2) email
    for email in emails:
        hit = conn.execute(
            """
            SELECT o.id, o.city FROM orgs o
            JOIN people p ON p.org_id = o.id
            WHERE o.kind = ? AND IFNULL(o.deleted_at,'') = ''
              AND lower(p.email) = ?
            """,
            (kind, email),
        ).fetchall()
        for row in hit:
            if _same_city(city, row["city"]):
                return row["id"]
        if len(hit) == 1:
            return hit[0]["id"]

    # 3) distinctive token overlap (need ≥1 strong token, all want tokens ⊆ row)
    strong = want_tok - {"олимп", "то", "цпод"}
    if not strong:
        return None
    best = None
    best_score = 0
    for row in rows:
        if not _same_city(city, row["city"]):
            continue
        have = _tokens(row["name"] + " " + (row["letter_name"] or ""))
        if not strong <= have and not (strong & have):
            continue
        score = len(strong & have)
        # require the rarest tokens for short names
        if "менеджер" in strong and "менеджер" not in have:
            continue
        if "слободскова" in strong or "слободсков" in _norm(item["name"]):
            if "слободсков" not in _norm(row["name"]) and "слободсков" not in _norm(row["letter_name"] or ""):
                continue
        if "созвездие" in strong and "созвездие" not in have:
            continue
        if "квасников" in strong and "квасников" not in have and "17" not in _norm(row["name"]):
            continue
        if "федунец" in strong or "федунца" in _norm(item["name"]):
            if "федунец" not in _norm(row["name"]) and "федунца" not in _norm(row["name"]):
                continue
        # bare "лицей" in Новомосковск: must NOT match школа менеджеров / созвездие
        if strong <= {"новомосковска"} or (not (strong - {"новомосковска", "новомосковск"}) and "лицей" in name_n):
            if "менеджер" in have or "созвездие" in have or "олимп" in have:
                continue
            # prefer exact short title
            if _norm(row["name"]) in {"мбоу лицей", 'мбоу "лицей"', "мбоу лицей"} or rn_is_plain_lyceum(row):
                return row["id"]
        if score > best_score:
            best_score = score
            best = row["id"]
    return best


def rn_is_plain_lyceum(row) -> bool:
    n = _norm(row["name"])
    return n in {"мбоу лицей", "лицей"} or re.fullmatch(r'мбоу\s*лицей', n) is not None


def _ensure_person(conn, org_id: int, person: dict) -> None:
    role = (person.get("role") or "Директор").strip() or "Директор"
    full_name = (person.get("full_name") or "").strip()
    email = (person.get("email") or "").strip()
    greeting = (person.get("greeting") or "").strip()
    phone = (person.get("phone") or "").strip()
    if not (full_name or email):
        return
    row = conn.execute(
        "SELECT * FROM people WHERE org_id = ? AND role = ? ORDER BY id LIMIT 1",
        (org_id, role),
    ).fetchone()
    if not row:
        conn.execute(
            """
            INSERT INTO people (org_id, role, full_name, greeting, email, phone)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (org_id, role, full_name, greeting, email, phone),
        )
        return
    sets, vals = [], []
    if full_name and not (row["full_name"] or "").strip():
        sets.append("full_name = ?"); vals.append(full_name)
    if email and not (row["email"] or "").strip():
        sets.append("email = ?"); vals.append(email)
    if greeting and not (row["greeting"] or "").strip():
        sets.append("greeting = ?"); vals.append(greeting)
    if phone and not (row["phone"] or "").strip():
        sets.append("phone = ?"); vals.append(phone)
    if sets:
        vals.append(row["id"])
        conn.execute(f"UPDATE people SET {', '.join(sets)} WHERE id = ?", vals)


def _ensure_team(conn, org_id: int, team: dict) -> None:
    name = (team.get("name") or "").strip()
    if not name:
        return
    part = (team.get("participation") or "").strip()
    key = name.casefold().replace("ё", "е")
    for t in conn.execute("SELECT id, name, participation FROM teams WHERE org_id = ?", (org_id,)):
        if t["name"].casefold().replace("ё", "е") == key:
            if part and not (t["participation"] or "").strip():
                conn.execute("UPDATE teams SET participation = ? WHERE id = ?", (part, t["id"]))
            return
    conn.execute(
        "INSERT INTO teams (org_id, name, participation) VALUES (?, ?, ?)",
        (org_id, name, part),
    )


def main() -> int:
    items = json.loads(DATA.read_text(encoding="utf-8"))
    created = updated = 0
    with db.connect() as conn:
        for item in items:
            oid = _find(conn, item)
            if oid is None:
                oid = conn.execute(
                    """
                    INSERT INTO orgs (kind, region, city, name, letter_name, coverage, phone, address, note)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["kind"],
                        item.get("region") or "Тульская область",
                        item.get("city") or "",
                        item["name"],
                        item.get("letter_name") or "",
                        item.get("coverage") or "",
                        item.get("phone") or "",
                        item.get("address") or "",
                        item.get("note") or "",
                    ),
                ).lastrowid
                created += 1
                status = "created"
            else:
                cur = conn.execute(
                    "SELECT note FROM orgs WHERE id = ?", (oid,)
                ).fetchone()
                conn.execute(
                    """
                    UPDATE orgs SET
                      letter_name = CASE WHEN IFNULL(letter_name,'')='' THEN ? ELSE letter_name END,
                      phone = CASE WHEN IFNULL(phone,'')='' THEN ? ELSE phone END,
                      address = CASE WHEN IFNULL(address,'')='' THEN ? ELSE address END,
                      region = CASE WHEN IFNULL(region,'')='' THEN ? ELSE region END,
                      city = CASE WHEN IFNULL(city,'')='' THEN ? ELSE city END
                    WHERE id = ?
                    """,
                    (
                        item.get("letter_name") or "",
                        item.get("phone") or "",
                        item.get("address") or "",
                        item.get("region") or "Тульская область",
                        item.get("city") or "",
                        oid,
                    ),
                )
                note = cur["note"] or ""
                if item.get("note") and (
                    not note
                    or note.startswith("поиск:")
                    or ("Команда" not in note and "ТЮФ по школам" not in note)
                ):
                    conn.execute("UPDATE orgs SET note = ? WHERE id = ?", (item["note"], oid))
                updated += 1
                status = "updated"
            for person in item.get("people") or []:
                _ensure_person(conn, oid, person)
            for team in item.get("teams") or []:
                _ensure_team(conn, oid, team)
            print(f"{status} #{oid} {item['name'][:60]} → {[t['name'] for t in item.get('teams') or []]}")
    print(f"done created={created} updated={updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
