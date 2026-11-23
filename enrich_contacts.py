"""Добор почты, телефона и адреса школ из открытых страниц. Не выдумывает контакты."""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import db

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
CTX = ssl.create_default_context()
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-()]*\d{2}[\s\-()]*\d{2}")
ADDR_RE = re.compile(
    r"(?:адрес(?:\s*организации)?|юридический адрес|фактический адрес)\s*[:\-]?\s*([^\n<]{15,160})",
    re.I,
)
POSTAL_RE = re.compile(r"(?<!\d)(\d{6})\s*,?\s*([А-Яа-яA-Za-zёЁ.\- ]{2,40},?\s*(?:ул\.|пр\.|пр-т|пер\.|б-р|ш\.|наб\.)[^<\n]{5,80})")
BAD_MAIL = (
    "example.",
    "sentry",
    "wixpress",
    "schema",
    "godaddy",
    "cloudflare",
    "wordpress",
    "noreply",
    "no-reply",
    "privacy@",
    "abuse@",
    "help@sochisirius",
    "help@sirius",
    ".png",
    ".jpg",
    ".gif",
    ".css",
    ".js",
    "sentry.io",
)
GOOD_HOST = (
    "edu.",
    "gov.ru",
    "mskobr.ru",
    "edu54.ru",
    "edu.mos.ru",
    "obr",
    "school",
    "sch",
    "лицей",
    "гимназ",
    "sunc",
    "sesc",
    "internat",
    "lyceum",
    "gymnas",
)


def fetch(url: str, timeout: float = 8) -> str:
    # curl is more reliable against anti-bot / DDG / school portals than urllib alone
    try:
        import subprocess

        proc = subprocess.run(
            [
                "curl",
                "-sL",
                "--max-time",
                str(int(timeout)),
                "-A",
                UA["User-Agent"],
                "--compressed",
                url,
            ],
            capture_output=True,
            timeout=timeout + 2,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout.decode("utf-8", "ignore")[:400_000]
    except Exception:
        pass
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
            return resp.read(400_000).decode("utf-8", "ignore")
    except Exception:
        return ""


def clean_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits[0] in "78":
        return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}"
    return re.sub(r"\s+", " ", raw).strip()


def good_email(email: str) -> bool:
    el = email.casefold()
    if any(b in el for b in BAD_MAIL):
        return False
    if el.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
        return False
    return True


def extract(html: str) -> tuple[set[str], set[str], set[str]]:
    emails = {e for e in EMAIL_RE.findall(html) if good_email(e)}
    phones = {clean_phone(p) for p in PHONE_RE.findall(html)}
    addrs = set()
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    plain = re.sub(r"<[^>]+>", "\n", text)
    plain = re.sub(r"[ \t]+", " ", plain)
    for m in ADDR_RE.finditer(plain):
        val = re.sub(r"\s+", " ", m.group(1)).strip(" .;")
        if 15 <= len(val) <= 160:
            addrs.add(val)
    for m in POSTAL_RE.finditer(plain):
        val = re.sub(r"\s+", " ", f"{m.group(1)}, {m.group(2)}").strip(" .;")
        if 15 <= len(val) <= 160:
            addrs.add(val)
    return emails, phones, addrs


def ddg_links(query: str, limit: int = 6) -> list[str]:
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    html = fetch(url, timeout=10)
    if not html:
        return []
    links = []
    for href in re.findall(r'uddg=([^&"]+)', html):
        try:
            link = urllib.parse.unquote(href)
        except Exception:
            continue
        if not link.startswith("http"):
            continue
        host = urllib.parse.urlparse(link).netloc.casefold()
        if any(x in host for x in ("duckduckgo.", "facebook.", "vk.com", "youtube.", "wikipedia.org")):
            continue
        links.append(link)
        if len(links) >= limit:
            break
    if not links:
        for href in re.findall(r'href="(https?://[^"]+)"', html):
            host = urllib.parse.urlparse(href).netloc.casefold()
            if "duckduckgo" in host:
                continue
            links.append(href)
            if len(links) >= limit:
                break
    return links


def score_link(link: str) -> int:
    host = urllib.parse.urlparse(link).netloc.casefold() + link.casefold()
    score = 0
    for token in GOOD_HOST:
        if token in host:
            score += 2
    if any(x in host for x in ("kontur", "list-org", "orgpage", "2gis", "yandex", "avito")):
        score -= 1
    return score


def pick_email(emails: set[str]) -> str:
    pref = ("info@", "mail@", "school@", "office@", "priem@", "secret@", "admin@", "dir@", "sesc@", "internat@")
    ranked = sorted(
        emails,
        key=lambda e: (
            0 if any(e.casefold().startswith(p) or p.strip("@") in e.casefold() for p in pref) else 1,
            0 if e.casefold().endswith((".ru", ".su", ".by")) else 1,
            len(e),
        ),
    )
    return ranked[0] if ranked else ""


def enrich_one(org: dict) -> dict:
    name = org["name"] or ""
    city = org["city"] or ""
    region = org["region"] or ""
    query = f'{name} {city} школа контакты email сайт'
    if region:
        query += f" {region}"
    links = ddg_links(query)
    links = sorted(set(links), key=score_link, reverse=True)[:5]
    emails: set[str] = set()
    phones: set[str] = set()
    addrs: set[str] = set()
    used = []
    for link in links:
        html = fetch(link)
        if not html:
            continue
        e, p, a = extract(html)
        # require some relevance: school name token or city on page
        blob = re.sub(r"<[^>]+>", " ", html).casefold()
        tokens = [t for t in re.findall(r"[а-яёa-z0-9]{4,}", name.casefold()) if t not in {"школа", "лицей", "гимназия", "маоу", "мбоу", "гбоу", "мкоу"}]
        if tokens and not any(t in blob for t in tokens[:3]) and city.casefold() not in blob:
            continue
        if e or p or a:
            used.append(link)
        emails |= e
        phones |= p
        addrs |= a
        if emails and (phones or addrs):
            break
        time.sleep(0.15)
    return {
        "id": org["id"],
        "email": pick_email(emails),
        "phone": next(iter(phones), ""),
        "address": next(iter(sorted(addrs, key=len)), ""),
        "sources": used[:3],
        "name": name,
    }


def has_mail(conn, org_id: int) -> bool:
    return any(
        (row["email"] or "").strip()
        for row in conn.execute("SELECT email FROM people WHERE org_id=?", (org_id,))
    )


def set_mail(conn, org_id: int, email: str) -> bool:
    people = list(conn.execute("SELECT * FROM people WHERE org_id=? AND role='Директор'", (org_id,)))
    if any((p["email"] or "").casefold() == email.casefold() for p in people):
        return False
    for p in people:
        if not (p["email"] or "").strip():
            conn.execute("UPDATE people SET email=? WHERE id=?", (email, p["id"]))
            return True
    if not people:
        conn.execute(
            "INSERT INTO people (org_id, role, full_name, greeting, email, phone) VALUES (?, 'Директор', '', '', ?, '')",
            (org_id, email),
        )
        return True
    # already has another director email — still store as coach/contact if empty coach
    coaches = list(conn.execute("SELECT * FROM people WHERE org_id=? AND role='Руководитель'", (org_id,)))
    for p in coaches:
        if not (p["email"] or "").strip():
            conn.execute("UPDATE people SET email=? WHERE id=?", (email, p["id"]))
            return True
    return False


def main():
    import sys

    # Priority: missing email first; address alone is lower priority (new empty column).
    only_mail_phone = "--all-addr" not in sys.argv
    limit = None
    for arg in sys.argv[1:]:
        if arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1])

    with db.connect() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(orgs)")]
        if "address" not in cols:
            conn.execute("ALTER TABLE orgs ADD COLUMN address TEXT NOT NULL DEFAULT ''")
        schools = [dict(r) for r in conn.execute("SELECT * FROM orgs WHERE kind=? ORDER BY id", (db.KIND_SCHOOL,))]
        targets = []
        for org in schools:
            need_mail = not has_mail(conn, org["id"])
            need_phone = not (org.get("phone") or "").strip()
            need_addr = not (org.get("address") or "").strip()
            if only_mail_phone:
                if need_mail or need_phone:
                    targets.append(org)
            elif need_mail or need_phone or need_addr:
                targets.append(org)
        # Prefer schools already tagged from photos / recent research.
        targets.sort(key=lambda o: (0 if "фото" in (o.get("note") or "") else 1, o["id"]))
        if limit:
            targets = targets[:limit]
    print("к добору: {} школ{}".format(len(targets), " (почта/телефон)" if only_mail_phone else ""), flush=True)
    got_mail = got_phone = got_addr = 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(enrich_one, org): org for org in targets}
        for i, fut in enumerate(as_completed(futs), 1):
            org = futs[fut]
            try:
                hit = fut.result()
            except Exception as exc:
                print("[{}/{}] ошибка {}: {}".format(i, len(targets), org["name"][:40], exc), flush=True)
                continue
            with db.connect() as conn:
                cur = conn.execute("SELECT phone, address FROM orgs WHERE id=?", (hit["id"],)).fetchone()
                phone = cur["phone"] or ""
                address = cur["address"] or ""
                changed = []
                if hit["email"] and not has_mail(conn, hit["id"]):
                    if set_mail(conn, hit["id"], hit["email"]):
                        got_mail += 1
                        changed.append(hit["email"])
                if hit["phone"] and hit["phone"] not in phone:
                    phone = db._append_unique(phone, hit["phone"])
                    conn.execute("UPDATE orgs SET phone=? WHERE id=?", (phone, hit["id"]))
                    got_phone += 1
                    changed.append(hit["phone"])
                if hit["address"] and not address:
                    conn.execute("UPDATE orgs SET address=? WHERE id=?", (hit["address"], hit["id"]))
                    got_addr += 1
                    changed.append(hit["address"][:60])
                if changed:
                    print("[{}/{}] {} <- {}".format(i, len(targets), hit["name"][:48], "; ".join(changed)), flush=True)
                elif i % 10 == 0:
                    print("[{}/{}] ...".format(i, len(targets)), flush=True)
    print("итог добора: почт +{}, телефонов +{}, адресов +{}".format(got_mail, got_phone, got_addr), flush=True)
    ss = db.schools("")
    no_mail = sum(
        1
        for s in ss
        if not ((s.get("director") or {}).get("email") or (s.get("coach") or {}).get("email"))
    )
    no_phone = sum(1 for s in ss if not (s.get("phone") or "").strip())
    no_addr = sum(1 for s in ss if not (s.get("address") or "").strip())
    print(f"осталось без почты {no_mail}, без телефона {no_phone}, без адреса {no_addr} из {len(ss)}", flush=True)


if __name__ == "__main__":
    main()
