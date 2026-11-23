"""Пройтись по школам и центрам: сайты из note, Москва edu.mos.ru, Bing-поиск."""
from __future__ import annotations

import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import db
from enrich_contacts import (
    extract,
    fetch,
    good_email,
    has_mail,
    pick_email,
    set_mail,
    clean_phone,
)

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
MOS_NUM_RE = re.compile(
    r"(?:школа|лицей|гимназия)\s*№?\s*(\d{2,4})\b|\b№\s*(\d{2,4})\b|школа\s+(\d{2,4})\b",
    re.I,
)


def urls_from_note(note: str) -> list[str]:
    out = []
    for m in URL_RE.findall(note or ""):
        u = m.rstrip(").,;]")
        if "sochisirius.ru" in u:
            continue
        out.append(u)
    # "сайт: http://..."
    for m in re.finditer(r"сайт:\s*(https?://\S+|[\w.-]+\.[a-zа-я.]{2,}\S*)", note or "", re.I):
        u = m.group(1).rstrip(").,;]")
        if not u.startswith("http"):
            u = "http://" + u
        if "sochisirius.ru" not in u:
            out.append(u)
    # dedupe
    seen = set()
    res = []
    for u in out:
        if u not in seen:
            seen.add(u)
            res.append(u)
    return res


def bing_links(query: str, limit: int = 6) -> list[str]:
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "ru"})
    html = fetch(url, timeout=12)
    if not html:
        return []
    links = []
    for href in re.findall(r'<a[^>]+href="(https?://[^"]+)"', html):
        host = urllib.parse.urlparse(href).netloc.casefold()
        if any(
            x in host
            for x in (
                "bing.",
                "microsoft.",
                "msn.",
                "facebook.",
                "vk.com",
                "youtube.",
                "wikipedia.org",
                "duckduckgo.",
            )
        ):
            continue
        if href not in links:
            links.append(href)
        if len(links) >= limit:
            break
    return links


def harvest_pages(urls: list[str]) -> tuple[str, str, str, list[str]]:
    emails: set[str] = set()
    phones: set[str] = set()
    addrs: set[str] = set()
    used = []
    for link in urls[:6]:
        html = fetch(link, timeout=10)
        if not html:
            continue
        e, p, a = extract(html)
        # also try /contacts /kontakty /sveden/common
        if not e:
            for path in ("/contacts", "/kontakty", "/sveden/common", "/about", "/glavnoe/kontakty"):
                base = link.rstrip("/")
                if path in link:
                    continue
                html2 = fetch(base + path, timeout=8)
                if html2:
                    e2, p2, a2 = extract(html2)
                    e |= e2
                    p |= p2
                    a |= a2
        if e or p or a:
            used.append(link)
        emails |= e
        phones |= p
        addrs |= a
        if emails and (phones or addrs):
            break
        time.sleep(0.1)
    return pick_email(emails), next(iter(phones), ""), next(iter(sorted(addrs, key=len)), ""), used


def moscow_edu_mail(name: str) -> str:
    m = MOS_NUM_RE.search(name or "")
    if not m:
        return ""
    num = next(g for g in m.groups() if g)
    # skip weird short/long
    if not (2 <= len(num) <= 4):
        return ""
    return f"{num}@edu.mos.ru"


def apply_hit(conn, org_id: int, email: str, phone: str, address: str) -> list[str]:
    cur = conn.execute("SELECT phone, address FROM orgs WHERE id=?", (org_id,)).fetchone()
    changed = []
    if email and not has_mail(conn, org_id):
        if set_mail(conn, org_id, email):
            changed.append(email)
    if phone and phone not in (cur["phone"] or ""):
        new_phone = db._append_unique(cur["phone"] or "", phone)
        conn.execute("UPDATE orgs SET phone=? WHERE id=?", (new_phone, org_id))
        changed.append(phone)
    if address and not (cur["address"] or "").strip():
        conn.execute("UPDATE orgs SET address=? WHERE id=?", (address, org_id))
        changed.append(address[:55])
    return changed


def enrich_org(org: dict) -> dict:
    name = org["name"] or ""
    city = org["city"] or ""
    region = org["region"] or ""
    kind = org["kind"]
    note_urls = urls_from_note(org.get("note") or "")
    email = phone = address = ""
    used = []
    if note_urls:
        email, phone, address, used = harvest_pages(note_urls)
    if not email or not phone:
        label = "центр" if kind == db.KIND_CENTER else "школа"
        q = f"{name} {city} {label} контакты email"
        if region:
            q += f" {region}"
        links = bing_links(q)
        # prefer edu hosts
        links = sorted(
            set(links + note_urls),
            key=lambda u: (
                0
                if any(t in u.casefold() for t in ("edu.", "obr", "school", "лицей", "gov", "sunc", "mskobr"))
                else 1
            ),
        )
        e2, p2, a2, used2 = harvest_pages(links)
        email = email or e2
        phone = phone or p2
        address = address or a2
        used = used or used2
    # Moscow institutional pattern as last resort for numbered GBOU
    if not email and region == "Москва" and kind == db.KIND_SCHOOL:
        guess = moscow_edu_mail(name)
        if guess:
            email = guess
            used.append("pattern:edu.mos.ru")
    return {
        "id": org["id"],
        "name": name,
        "kind": kind,
        "email": email,
        "phone": phone,
        "address": address,
        "sources": used[:3],
    }


def targets(conn, kind: str, need_addr: bool = False):
    rows = [dict(r) for r in conn.execute("SELECT * FROM orgs WHERE kind=? ORDER BY id", (kind,))]
    out = []
    for org in rows:
        need_mail = not has_mail(conn, org["id"])
        need_phone = not (org.get("phone") or "").strip()
        need_a = not (org.get("address") or "").strip()
        if need_mail or need_phone or (need_addr and need_a):
            out.append(org)
    return out


def main():
    kinds = []
    args = sys.argv[1:]
    limit = None
    with_addr = "--all-addr" in args
    if "--schools" in args or not any(a.startswith("--cent") for a in args):
        if "--centers-only" not in args:
            kinds.append(db.KIND_SCHOOL)
    if "--centers" in args or "--centers-only" in args or not args:
        kinds.append(db.KIND_CENTER)
    if not kinds:
        kinds = [db.KIND_SCHOOL, db.KIND_CENTER]
    for a in args:
        if a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])

    with db.connect() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(orgs)")]
        if "address" not in cols:
            conn.execute("ALTER TABLE orgs ADD COLUMN address TEXT NOT NULL DEFAULT ''")
        all_targets = []
        for kind in kinds:
            all_targets.extend(targets(conn, kind, need_addr=with_addr))
        # prioritize: has note URL, then photo, then moscow, then rest
        def prio(o):
            note = o.get("note") or ""
            return (
                0 if urls_from_note(note) else 1,
                0 if "фото" in note else 1,
                0 if o.get("region") == "Москва" else 1,
                o["id"],
            )

        all_targets.sort(key=prio)
        if limit:
            all_targets = all_targets[:limit]

    print(f"к добору: {len(all_targets)} (kinds={kinds})", flush=True)
    got_mail = got_phone = got_addr = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(enrich_org, org): org for org in all_targets}
        for i, fut in enumerate(as_completed(futs), 1):
            org = futs[fut]
            try:
                hit = fut.result()
            except Exception as exc:
                print(f"[{i}/{len(all_targets)}] ошибка {org['name'][:40]}: {exc}", flush=True)
                continue
            with db.connect() as conn:
                changed = apply_hit(conn, hit["id"], hit["email"], hit["phone"], hit["address"])
                if hit["email"] and hit["email"] in changed:
                    got_mail += 1
                if hit["phone"] and hit["phone"] in changed:
                    got_phone += 1
                if hit["address"] and any(hit["address"][:20] in c for c in changed):
                    got_addr += 1
                if changed:
                    print(
                        f"[{i}/{len(all_targets)}] {hit['kind'][0]} {hit['name'][:42]} <- {'; '.join(changed)}",
                        flush=True,
                    )
                elif i % 15 == 0:
                    print(f"[{i}/{len(all_targets)}] ...", flush=True)

    print(f"итог: почт +{got_mail}, тел +{got_phone}, адр +{got_addr}", flush=True)
    with db.connect() as conn:
        for kind, label in ((db.KIND_SCHOOL, "школ"), (db.KIND_CENTER, "центров")):
            rows = list(conn.execute("SELECT id, phone, address FROM orgs WHERE kind=?", (kind,)))
            no_mail = sum(1 for r in rows if not has_mail(conn, r["id"]))
            no_phone = sum(1 for r in rows if not (r["phone"] or "").strip())
            no_addr = sum(1 for r in rows if not (r["address"] or "").strip())
            print(f"{label}: без почты {no_mail}, без тел {no_phone}, без адреса {no_addr} из {len(rows)}", flush=True)


if __name__ == "__main__":
    main()
