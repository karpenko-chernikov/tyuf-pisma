"""Прогон по школам без ФИО директора: поиск + разбор страниц. Не выдумывает имена."""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from enrich_directors import (
    DATA,
    UA,
    clean_fio,
    extract_director,
    greeting_for,
    missing_schools,
    set_director_name,
    strip_html,
)
from app import db

OUT = DATA / "directors_hunt.jsonl"
FOUND = DATA / "directors_found.json"


def curl(url: str, timeout: int = 12) -> str:
    try:
        proc = subprocess.run(
            [
                "curl",
                "-sL",
                "--max-time",
                str(timeout),
                "-A",
                UA,
                "--compressed",
                "-H",
                "Accept-Language: ru-RU,ru;q=0.9",
                url,
            ],
            capture_output=True,
            timeout=timeout + 4,
        )
        return proc.stdout.decode("utf-8", "ignore")
    except Exception:
        return ""


def ddg_links(query: str, n: int = 6) -> list[str]:
    html = curl(
        "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query}),
        timeout=15,
    )
    links = []
    for m in re.finditer(r'uddg=([^&"]+)', html):
        url = urllib.parse.unquote(m.group(1))
        if not url.startswith("http"):
            continue
        if any(x in url for x in ("duckduckgo.", "youtube.", "vk.com/wall", "facebook.")):
            continue
        if url not in links:
            links.append(url)
        if len(links) >= n:
            break
    # fallback: plain hrefs
    if not links:
        for m in re.finditer(r'href="(https?://[^"]+)"', html):
            url = m.group(1)
            if "duckduckgo" in url:
                continue
            if url not in links:
                links.append(url)
            if len(links) >= n:
                break
    return links


def bing_links(query: str, n: int = 6) -> list[str]:
    html = curl(
        "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query, "setlang": "ru"}),
        timeout=15,
    )
    links = []
    for m in re.finditer(r'<a[^>]+href="(https?://[^"]+)"', html):
        url = m.group(1)
        if any(
            x in url
            for x in (
                "bing.com",
                "microsoft.com",
                "youtube.",
                "facebook.",
                "login.",
                "javascript:",
            )
        ):
            continue
        if url not in links:
            links.append(url)
        if len(links) >= n:
            break
    return links


def candidate_paths(base: str) -> list[str]:
    base = base.rstrip("/")
    paths = [
        "",
        "/sveden/managers",
        "/sveden/struct",
        "/sveden/common",
        "/svedeniya/rukovodstvo",
        "/sveden/rukovodstvo",
        "/contacts",
        "/kontakty",
        "/about/administration",
        "/administratsiya",
        "/rukovodstvo",
        "/o-nas/rukovodstvo",
    ]
    return [base + p for p in paths]


def extract_from_pages(urls: list[str]) -> tuple[str, str]:
    for url in urls:
        html = curl(url)
        if not html or len(html) < 200:
            continue
        text = strip_html(html)
        fio = extract_director(text)
        if fio:
            return fio, url
        # mos.ru / structured snippets
        for m in re.finditer(
            r"Директор[:\s]+([А-ЯЁ][а-яё\-]+ [А-ЯЁ][а-яё\-]+ [А-ЯЁ][а-яё\-]+)",
            text,
        ):
            fio = clean_fio(m.group(1))
            if fio:
                return fio, url
    return "", ""


def hunt_one(row: dict) -> dict:
    name = row["name"]
    city = row.get("city") or ""
    region = row.get("region") or ""
    q1 = f"директор {name} {city}".strip()
    q2 = f"{name} {city} руководство директор".strip()
    links: list[str] = []
    for q in (q1, q2):
        for fn in (ddg_links, bing_links):
            try:
                for u in fn(q, n=5):
                    if u not in links:
                        links.append(u)
            except Exception:
                pass
        if links:
            break
    # expand likely school homepage paths
    expanded: list[str] = []
    for u in links[:8]:
        expanded.append(u)
        # if looks like school root, add sveden pages
        host = urllib.parse.urlparse(u)
        if host.netloc and not any(
            x in host.netloc for x in ("wikipedia", "rusprofile", "list-org", "spark-", "sbis.")
        ):
            root = f"{host.scheme}://{host.netloc}"
            for p in candidate_paths(root)[:6]:
                if p not in expanded:
                    expanded.append(p)
    fio, src = extract_from_pages(expanded[:18])
    # company registries as last resort
    if not fio:
        for u in links:
            if any(x in u for x in ("rusprofile.ru", "list-org.com", "sbis.ru", "checko.ru")):
                html = curl(u)
                text = strip_html(html)
                for pat in (
                    r"Руководитель\s+([А-ЯЁ][а-яё\-]+ [А-ЯЁ][а-яё\-]+ [А-ЯЁ][а-яё\-]+)",
                    r"Директор\s+([А-ЯЁ][а-яё\-]+ [А-ЯЁ][а-яё\-]+ [А-ЯЁ][а-яё\-]+)",
                    r"Генеральный директор\s+([А-ЯЁ][а-яё\-]+ [А-ЯЁ][а-яё\-]+ [А-ЯЁ][а-яё\-]+)",
                ):
                    m = re.search(pat, text)
                    if m:
                        fio = clean_fio(m.group(1))
                        if fio:
                            src = u
                            break
            if fio:
                break
    return {
        "id": row["id"],
        "name": name,
        "city": city,
        "region": region,
        "director": fio,
        "source": src,
        "links": links[:6],
    }


def main():
    rows = missing_schools()
    print(f"к поиску: {len(rows)}", flush=True)
    DATA.mkdir(exist_ok=True)
    found = []
    # resume
    done_ids = set()
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            done_ids.add(obj.get("id"))
            if obj.get("director"):
                found.append(obj)
    todo = [r for r in rows if r["id"] not in done_ids]
    print(f"уже сделано {len(done_ids)}, осталось {len(todo)}", flush=True)

    with OUT.open("a", encoding="utf-8") as fh:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(hunt_one, row): row for row in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                row = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = {
                        "id": row["id"],
                        "name": row["name"],
                        "director": "",
                        "source": f"error:{e}",
                        "links": [],
                    }
                fh.write(json.dumps(res, ensure_ascii=False) + "\n")
                fh.flush()
                if res.get("director"):
                    found.append(res)
                    print(f"+ {res['id']} {res['director']} ← {res.get('source','')[:60]}", flush=True)
                else:
                    print(f"- {res['id']} {row['name'][:40]}", flush=True)
                if i % 10 == 0:
                    print(f"... {i}/{len(todo)}, найдено за прогон {sum(1 for x in found if x['id'] in {r['id'] for r in todo})}", flush=True)
                time.sleep(0.05)

    # unique by id, keep with director
    by_id = {}
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            if obj.get("director"):
                by_id[obj["id"]] = obj
    FOUND.write_text(json.dumps(list(by_id.values()), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"итого с ФИО в hunt: {len(by_id)} → {FOUND}", flush=True)

    # apply
    applied = 0
    with db.connect() as conn:
        for obj in by_id.values():
            if set_director_name(conn, int(obj["id"]), obj["director"]):
                applied += 1
                # normalize greeting without trailing !
                people = list(
                    conn.execute(
                        "SELECT id, greeting FROM people WHERE org_id=? AND role='Директор'",
                        (obj["id"],),
                    )
                )
                for p in people:
                    g = (p["greeting"] or "").rstrip("!")
                    if g and not g.endswith(","):
                        # store without comma; mailings add comma
                        pass
                    conn.execute(
                        "UPDATE people SET greeting=? WHERE id=?",
                        (g.rstrip(","), p["id"]),
                    )
    left = len(missing_schools())
    print(f"записано новых: {applied}; без ФИО осталось: {left}", flush=True)


if __name__ == "__main__":
    main()
