#!/usr/bin/env python3
"""Идемпотентный посев школ/центров из JSON (region/city/name + people + teams)."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402

STOP = {
    "мбоу", "маоу", "моу", "гбоу", "сош", "лицей", "гимназия", "школа", "имени",
    "г", "город", "село", "с", "область", "республика", "район", "средняя",
    "общеобразовательная", "муниципальное", "автономное", "бюджетное", "учреждение",
}


def _norm(s: str) -> str:
    s = (s or "").casefold().replace("ё", "е")
    s = s.replace("№", " ").replace("#", " ")
    s = re.sub(r"[«»\"'`.,;:()\[\]{}—–-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _tokens(s: str) -> set[str]:
    return {t for t in _norm(s).split() if t and t not in STOP and len(t) > 1}


def _same_city(a: str, b: str) -> bool:
    a = _norm(a).replace("г ", "").strip()
    b = _norm(b).replace("г ", "").strip()
    if not a or not b:
        return True
    return a == b or a in b or b in a


def _same_region(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return True
    return a == b or a in b or b in a


def _find(conn, item: dict):
    kind = item.get("kind") or db.KIND_SCHOOL
    region = item.get("region") or ""
    city = item.get("city") or ""
    name_n = _norm(item["name"])
    letter_n = _norm(item.get("letter_name") or "")
    want = _tokens(item["name"] + " " + (item.get("letter_name") or ""))
    emails = {
        (p.get("email") or "").casefold()
        for p in item.get("people") or []
        if (p.get("email") or "").strip()
    }
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM orgs WHERE kind = ? AND IFNULL(deleted_at,'') = ''",
            (kind,),
        )
    ]
    for row in rows:
        if not _same_region(region, row["region"] or ""):
            continue
        if not _same_city(city, row["city"] or ""):
            continue
        rn, rl = _norm(row["name"]), _norm(row["letter_name"] or "")
        if rn == name_n or (letter_n and (rl == letter_n or rn == letter_n)):
            return row["id"]
    for email in emails:
        hit = conn.execute(
            """
            SELECT o.id, o.region, o.city FROM orgs o
            JOIN people p ON p.org_id = o.id
            WHERE o.kind = ? AND IFNULL(o.deleted_at,'') = '' AND lower(p.email) = ?
            """,
            (kind, email),
        ).fetchall()
        for row in hit:
            if _same_region(region, row["region"] or "") and _same_city(city, row["city"] or ""):
                return row["id"]
        if len(hit) == 1:
            return hit[0]["id"]
    best = None
    best_score = 0
    for row in rows:
        if not _same_region(region, row["region"] or ""):
            continue
        if not _same_city(city, row["city"] or ""):
            continue
        have = _tokens(row["name"] + " " + (row["letter_name"] or ""))
        score = len(want & have)
        if score < 1:
            continue
        # require digit overlap for numbered schools
        nums_want = set(re.findall(r"\d+", name_n))
        nums_have = set(re.findall(r"\d+", _norm(row["name"])))
        if nums_want and nums_want != nums_have:
            continue
        if score > best_score:
            best_score = score
            best = row["id"]
    return best


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
            "INSERT INTO people (org_id, role, full_name, greeting, email, phone) VALUES (?,?,?,?,?,?)",
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


def seed_file(path: Path) -> tuple[int, int]:
    items = json.loads(path.read_text(encoding="utf-8"))
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
                        item.get("kind") or db.KIND_SCHOOL,
                        item.get("region") or "",
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
                cur = conn.execute("SELECT note FROM orgs WHERE id = ?", (oid,)).fetchone()
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
                        item.get("region") or "",
                        item.get("city") or "",
                        oid,
                    ),
                )
                note = cur["note"] or ""
                rich = item.get("note") or ""
                if rich and (not note or note.startswith("поиск:") or rich.split(":")[0] not in note):
                    if "Бурятский ТЮФ" in rich and "Бурятский ТЮФ" not in note:
                        conn.execute(
                            "UPDATE orgs SET note = ? WHERE id = ?",
                            ((note + ("\n" if note else "") + rich).strip(), oid),
                        )
                    elif not note or note.startswith("поиск:"):
                        conn.execute("UPDATE orgs SET note = ? WHERE id = ?", (rich, oid))
                updated += 1
                status = "updated"
            for person in item.get("people") or []:
                _ensure_person(conn, oid, person)
            for team in item.get("teams") or []:
                _ensure_team(conn, oid, team)
            print(f"{status} #{oid} {item['name'][:60]} → {[t['name'] for t in item.get('teams') or []]}")
    return created, updated


def main(argv: list[str]) -> int:
    paths = [Path(p) for p in argv[1:]] or [Path(__file__).with_name("tula_tyuf_2026.json")]
    total_c = total_u = 0
    for path in paths:
        print(f"== {path.name} ==")
        c, u = seed_file(path)
        total_c += c
        total_u += u
        print(f"file done created={c} updated={u}")
    print(f"all done created={total_c} updated={total_u}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
