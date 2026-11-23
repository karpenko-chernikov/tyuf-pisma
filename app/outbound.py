"""Почтовые ящики (SMTP) и отправка писем рассылки."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from app import db
from app import mailings

DEFAULT_EMAIL_SUBJECT = "Приглашение на Открытый Всероссийский ТЮФ — {{school}}"

DEFAULT_EMAIL_BODY = """{{greeting}}

Направляем вам письмо-приглашение к участию в Открытом Всероссийском турнире юных физиков.
Документы — во вложении (PDF).

С уважением,
оргкомитет Открытого Всероссийского ТЮФ
rosypt.ru
"""

# host, port, security: starttls | ssl | none
SMTP_PRESETS = {
    "gmail.com": ("smtp.gmail.com", 587, "starttls"),
    "googlemail.com": ("smtp.gmail.com", 587, "starttls"),
    "yandex.ru": ("smtp.yandex.ru", 465, "ssl"),
    "yandex.com": ("smtp.yandex.com", 465, "ssl"),
    "ya.ru": ("smtp.yandex.ru", 465, "ssl"),
    "mail.ru": ("smtp.mail.ru", 465, "ssl"),
    "bk.ru": ("smtp.mail.ru", 465, "ssl"),
    "inbox.ru": ("smtp.mail.ru", 465, "ssl"),
    "list.ru": ("smtp.mail.ru", 465, "ssl"),
    "internet.ru": ("smtp.mail.ru", 465, "ssl"),
    "outlook.com": ("smtp.office365.com", 587, "starttls"),
    "hotmail.com": ("smtp.office365.com", 587, "starttls"),
    "live.com": ("smtp.office365.com", 587, "starttls"),
    "nsu.ru": ("smtp.nsu.ru", 587, "starttls"),
}


def init_tables(conn) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS mail_accounts (
          id INTEGER PRIMARY KEY,
          label TEXT NOT NULL DEFAULT '',
          email TEXT NOT NULL,
          smtp_host TEXT NOT NULL DEFAULT '',
          smtp_port INTEGER NOT NULL DEFAULT 587,
          security TEXT NOT NULL DEFAULT 'starttls',
          username TEXT NOT NULL DEFAULT '',
          password_enc TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT ''
        );
        """
    )


def _secret_key() -> bytes:
    raw = os.environ.get("SECRET_KEY") or "tyuf-pisma-local"
    return hashlib.sha256(raw.encode("utf-8")).digest()


def encrypt_secret(plain: str) -> str:
    raw = (plain or "").encode("utf-8")
    key = _secret_key()
    out = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
    return "v1:" + base64.urlsafe_b64encode(out).decode("ascii")


def decrypt_secret(blob: str) -> str:
    text = blob or ""
    if not text.startswith("v1:"):
        return text
    try:
        raw = base64.urlsafe_b64decode(text[3:].encode("ascii"))
    except Exception:
        return ""
    key = _secret_key()
    out = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
    return out.decode("utf-8", errors="replace")


def suggest_smtp(email: str) -> dict:
    domain = (email or "").rsplit("@", 1)[-1].strip().casefold()
    host, port, security = SMTP_PRESETS.get(domain, ("", 587, "starttls"))
    return {"smtp_host": host, "smtp_port": port, "security": security, "domain": domain}


def apply_fields(text: str, data: dict | None) -> str:
    out = text or ""
    if not data:
        return out
    for key, value in data.items():
        val = "" if value is None else str(value)
        out = out.replace("{{" + key + "}}", val)
        out = out.replace("{{ " + key + " }}", val)
    # убрать незаполненные плейсхолдеры
    out = re.sub(r"\{\{\s*[a-zA-Z0-9_]+\s*\}\}", "", out)
    return out


def list_accounts() -> list[dict]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, label, email, smtp_host, smtp_port, security, username, created_at
            FROM mail_accounts
            ORDER BY id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_account(account_id: int, *, with_password: bool = False) -> dict | None:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM mail_accounts WHERE id = ?", (account_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    if with_password:
        item["password"] = decrypt_secret(item.pop("password_enc", ""))
    else:
        item.pop("password_enc", None)
    return item


def upsert_account(
    *,
    account_id: int | None = None,
    label: str,
    email: str,
    password: str = "",
    smtp_host: str = "",
    smtp_port: int = 587,
    security: str = "starttls",
    username: str = "",
) -> int:
    email = (email or "").strip()
    if not email or "@" not in email:
        raise ValueError("Укажите адрес почты")
    label = (label or "").strip() or email
    security = security if security in {"starttls", "ssl", "none"} else "starttls"
    try:
        smtp_port = int(smtp_port or 587)
    except (TypeError, ValueError):
        smtp_port = 587
    smtp_host = (smtp_host or "").strip()
    if not smtp_host:
        preset = suggest_smtp(email)
        smtp_host = preset["smtp_host"]
        smtp_port = preset["smtp_port"]
        security = preset["security"]
    if not smtp_host:
        raise ValueError("Укажите SMTP-сервер (для этого домена подсказка неизвестна)")
    username = (username or "").strip() or email
    with db.connect() as conn:
        if account_id:
            existing = conn.execute(
                "SELECT password_enc FROM mail_accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if not existing:
                raise ValueError("Ящик не найден")
            enc = encrypt_secret(password) if password else existing["password_enc"]
            if password == "":
                enc = existing["password_enc"]
            conn.execute(
                """
                UPDATE mail_accounts
                SET label=?, email=?, smtp_host=?, smtp_port=?, security=?, username=?, password_enc=?
                WHERE id=?
                """,
                (label, email, smtp_host, smtp_port, security, username, enc, account_id),
            )
            return account_id
        if not password:
            raise ValueError("Укажите пароль или пароль приложения")
        cur = conn.execute(
            """
            INSERT INTO mail_accounts
              (label, email, smtp_host, smtp_port, security, username, password_enc, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                label,
                email,
                smtp_host,
                smtp_port,
                security,
                username,
                encrypt_secret(password),
                db._now(),
            ),
        )
        return cur.lastrowid


def delete_account(account_id: int) -> bool:
    with db.connect() as conn:
        cur = conn.execute("DELETE FROM mail_accounts WHERE id = ?", (account_id,))
        return cur.rowcount > 0


def test_account(account_id: int) -> dict:
    account = get_account(account_id, with_password=True)
    if not account:
        return {"ok": False, "error": "Ящик не найден"}
    try:
        _smtp_login(account)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "error": ""}


def _smtp_login(account: dict):
    host = account["smtp_host"]
    port = int(account["smtp_port"])
    user = account.get("username") or account["email"]
    password = account.get("password") or ""
    security = account.get("security") or "starttls"
    context = ssl.create_default_context()
    if security == "ssl":
        smtp = smtplib.SMTP_SSL(host, port, timeout=45, context=context)
    else:
        smtp = smtplib.SMTP(host, port, timeout=45)
        smtp.ehlo()
        if security == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
    try:
        smtp.login(user, password)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass


def _build_message(
    *,
    account: dict,
    to_addr: str,
    subject: str,
    body: str,
    attachments: list[tuple[str, bytes]],
) -> EmailMessage:
    msg = EmailMessage()
    from_name = (account.get("label") or "").strip()
    from_email = account["email"]
    msg["From"] = formataddr((from_name, from_email)) if from_name and from_name != from_email else from_email
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body or "")
    for filename, data in attachments:
        msg.add_attachment(
            data,
            maintype="application",
            subtype="pdf",
            filename=filename,
        )
    return msg


def _send_via_smtp(account: dict, msg: EmailMessage) -> None:
    host = account["smtp_host"]
    port = int(account["smtp_port"])
    user = account.get("username") or account["email"]
    password = account.get("password") or ""
    security = account.get("security") or "starttls"
    context = ssl.create_default_context()
    if security == "ssl":
        with smtplib.SMTP_SSL(host, port, timeout=60, context=context) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
        return
    with smtplib.SMTP(host, port, timeout=60) as smtp:
        smtp.ehlo()
        if security == "starttls":
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(user, password)
        smtp.send_message(msg)


def compose_for_letter(mailing: dict, letter: dict) -> dict:
    """Тема/текст с подстановкой и вложение для одного письма."""
    org = db.org_by_id(letter["org_id"])
    data = mailings.build_fields(mailing, org) if org else {
        "school": letter.get("org_name") or "",
        "greeting": "Уважаемые коллеги,",
        "email": letter.get("email") or "",
        "city": letter.get("city") or "",
        "director": letter.get("person_name") or "",
        "date": mailing.get("letter_date") or "",
    }
    subject = apply_fields(mailing.get("email_subject") or DEFAULT_EMAIL_SUBJECT, data)
    body = apply_fields(mailing.get("email_body") or DEFAULT_EMAIL_BODY, data)
    attach = int(mailing.get("email_attach") or 0) == 1
    pdf_path = None
    if attach and letter.get("pdf_name"):
        path = mailings.letter_pdf_path(mailing["id"], letter["id"])
        if path.exists():
            pdf_path = path
    return {
        "to": (letter.get("email") or "").strip(),
        "subject": subject.strip(),
        "body": body.strip(),
        "data": data,
        "pdf_path": pdf_path,
        "attach": attach,
    }


def send_letter(letter_id: int, user_name: str = "", *, account_id: int | None = None) -> dict:
    letter = mailings.get_letter(letter_id)
    if not letter:
        return {"ok": False, "error": "Письмо не найдено"}
    mailing = mailings.get_mailing(letter["mailing_id"])
    if not mailing:
        return {"ok": False, "error": "Рассылка не найдена"}
    aid = account_id or int(mailing.get("email_account_id") or 0)
    if not aid:
        return {"ok": False, "error": "Выберите ящик «От кого» в настройках отправки"}
    account = get_account(aid, with_password=True)
    if not account:
        return {"ok": False, "error": "Ящик не найден — добавьте в Кабинете"}
    if not account.get("password"):
        return {"ok": False, "error": "У ящика нет пароля"}
    draft = compose_for_letter(mailing, letter)
    to_addr = draft["to"]
    if not to_addr or "@" not in to_addr:
        return {"ok": False, "error": "У получателя нет почты"}
    attachments: list[tuple[str, bytes]] = []
    if draft["attach"]:
        if not draft["pdf_path"]:
            return {"ok": False, "error": "Нет PDF — сначала соберите письма"}
        path: Path = draft["pdf_path"]
        safe = "".join(
            ch if ch.isalnum() or ch in " ._-" else "_"
            for ch in f"{(letter.get('org_name') or 'letter')[:80]}.pdf"
        )
        attachments.append((safe, path.read_bytes()))
    try:
        msg = _build_message(
            account=account,
            to_addr=to_addr,
            subject=draft["subject"],
            body=draft["body"],
            attachments=attachments,
        )
        _send_via_smtp(account, msg)
    except Exception as exc:
        detail = f"Ошибка отправки: {exc}"
        with db.connect() as conn:
            mailings._add_event(conn, letter_id, "error", detail, user_name)
        db.log_activity(
            by_user=user_name,
            kind="letter_send_fail",
            detail=f"«{letter.get('org_name') or letter_id}»: {exc}",
            href=f"/mailings/{mailing['id']}/letters/{letter_id}",
            ref_id=letter_id,
        )
        return {"ok": False, "error": str(exc)}

    note = f"Отправлено с {account['email']} → {to_addr}"
    mailings.mark_sent(letter_id, user_name, note)
    return {"ok": True, "error": "", "to": to_addr}


def send_mailing(
    mailing_id: int,
    user_name: str = "",
    *,
    only_ready: bool = True,
    skip_sent: bool = True,
) -> dict:
    mailing = mailings.get_mailing(mailing_id)
    if not mailing:
        return {"ok": 0, "fail": 0, "skip": 0, "error": "Рассылка не найдена"}
    if not int(mailing.get("email_account_id") or 0):
        return {"ok": 0, "fail": 0, "skip": 0, "error": "Сначала выберите ящик и сохраните настройки отправки"}
    ok = fail = skip = 0
    errors: list[str] = []
    for row in mailings.letters_for(mailing_id):
        if skip_sent and row.get("status") in {"sent", "replied"}:
            skip += 1
            continue
        if only_ready and not row.get("pdf_name") and int(mailing.get("email_attach") or 0) == 1:
            skip += 1
            continue
        if not (row.get("email") or "").strip():
            skip += 1
            continue
        result = send_letter(row["id"], user_name)
        if result.get("ok"):
            ok += 1
        else:
            fail += 1
            if len(errors) < 5:
                errors.append(f"{row.get('org_name')}: {result.get('error')}")
    db.log_activity(
        by_user=user_name,
        kind="mailing_send",
        detail=f"«{mailing.get('title') or mailing_id}» · ок {ok}, ошибок {fail}, пропуск {skip}",
        href=f"/mailings/{mailing_id}",
        ref_id=mailing_id,
    )
    return {
        "ok": ok,
        "fail": fail,
        "skip": skip,
        "error": "; ".join(errors),
    }
