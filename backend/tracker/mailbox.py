"""
The user's own mailbox, for the email hand-off (Job Automation).

ApplyForge can save a prepared application email - recipient, subject, body and the
resume attached - into the user's **Drafts** folder, so the user opens it in their own
email app (Gmail, Outlook, Yahoo, Thunderbird...), checks or edits it and presses Send
there. Afterwards ApplyForge can look in the **Sent** folder for it.

Both use IMAP with the login the user already saved for sending (an app password for
Gmail/Outlook/Yahoo), so no second credential is needed. Only two things are ever done
to the mailbox: APPEND one draft, and a read-only search of the Sent folder. Nothing is
sent, deleted or changed.

When the server's email backend is not SMTP (local development, tests), a
``LocalMailbox`` folder stands in for the real mailbox, so nothing leaves the machine:
``<EMAIL_DRAFTS_LOCAL_PATH>/<user id>/Drafts`` and ``.../Sent``.
"""

import imaplib
import logging
import re
import socket
import time
from datetime import timedelta, timezone as dt_timezone
from email import message_from_bytes
from email.parser import BytesHeaderParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from tracker.credentials import CredentialError, decrypt

logger = logging.getLogger("tracker.mailbox")

IMAP_TIMEOUT_SECONDS = 30
# Clock differences between ApplyForge and the mail server.
SENT_MATCH_SLACK = timedelta(minutes=5)
MAX_SENT_CANDIDATES = 30
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# Known providers: IMAP server and the web page that lists the drafts.
_PROVIDERS = (
    # (SMTP host suffix, IMAP host, drafts page; "{email}" is the account address)
    ("gmail.com", "imap.gmail.com", "https://mail.google.com/mail/?authuser={email}#drafts"),
    ("googlemail.com", "imap.gmail.com", "https://mail.google.com/mail/?authuser={email}#drafts"),
    ("outlook.com", "outlook.office365.com", "https://outlook.live.com/mail/0/drafts"),
    ("office365.com", "outlook.office365.com", "https://outlook.office.com/mail/drafts"),
    ("mail.yahoo.com", "imap.mail.yahoo.com", "https://mail.yahoo.com/"),
)


class MailboxError(Exception):
    """The mailbox could not be used; ``message`` is written for the user."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _provider(account):
    host = (account.smtp_host or "").lower()
    for suffix, imap_host, drafts_url in _PROVIDERS:
        if host == suffix or host.endswith("." + suffix):
            return imap_host, drafts_url
    return "", ""


def imap_host_for(account):
    """The IMAP server: the one the user entered, a known provider's, or smtp.X -> imap.X."""
    if account.imap_host:
        return account.imap_host
    known, _ = _provider(account)
    if known:
        return known
    host = (account.smtp_host or "").lower()
    return "imap." + host[5:] if host.startswith("smtp.") else ""


def drafts_page_url(account, gmail_id=""):
    """The provider's web page listing the drafts, or "" (open Drafts in your email app).
    With ``gmail_id`` (the draft's Gmail message id, hex) Gmail opens that draft itself;
    if Gmail ignores the id, the draft is the newest one in the Drafts list."""
    _, url = _provider(account)
    if not url:
        return ""
    url = url.replace("{email}", account.from_email)
    if gmail_id and url.endswith("#drafts"):
        url += f"?compose={gmail_id}"
    return url


_SPACED_APP_PASSWORD = re.compile(r"^[A-Za-z]{4}( [A-Za-z]{4}){3}$")


def app_password(password):
    """Google shows app passwords as "abcd efgh ijkl mnop"; the login needs the 16 letters
    without the spaces. Other passwords are used exactly as entered."""
    value = (password or "").strip()
    return value.replace(" ", "") if _SPACED_APP_PASSWORD.match(value) else password


def login_problem(server_text, account):
    """What to do when the mailbox refuses the login, from the server's own answer (which
    never contains the password)."""
    text = (server_text or "").lower()
    gmail = "gmail" in (account.smtp_host or "").lower() or "gmail" in (account.imap_host or "").lower()
    if "application-specific password" in text or "app password" in text:
        return ("Your mailbox needs an app password, not your normal password. Create one at "
                "myaccount.google.com/apppasswords (2-Step Verification must be on) and paste it in "
                "Account → Email sending." if gmail else
                "Your mailbox needs an app password, not your normal password. Create one in your email "
                "account's security settings and paste it in Account → Email sending.")
    if "web login" in text or "log in via your web browser" in text or "webloginrequired" in text:
        return ("Your mailbox wants you to sign in on its website once before apps can log in. Sign in to "
                "your email in the browser, then try again.")
    if "imap" in text and ("disabled" in text or "not enabled" in text):
        return "IMAP is turned off for your mailbox. Turn it on in your email settings (Forwarding and POP/IMAP)."
    hint = ("Use your full Gmail address as the username and a Google app password "
            "(myaccount.google.com/apppasswords), not your normal password." if gmail else
            "Check the username (usually your full email address) and use an app password if your provider "
            "requires one.")
    detail = re.sub(r"\s+", " ", server_text or "").strip(" b'\"")[:160]
    return f"Your mailbox rejected the login for drafts (IMAP): {detail or 'no reason given'}. {hint}"


def local_mode():
    backend = getattr(settings, "EMAIL_DRAFTS_BACKEND", "") or ""
    if backend:
        return backend == "local"
    return not settings.EMAIL_BACKEND.endswith("smtp.EmailBackend")


def mailbox_for(account):
    """The mailbox to use for ``account``: IMAP when emails really go out, else a local folder."""
    return LocalMailbox(account) if local_mode() else ImapMailbox(account)


def _recipients(header_value):
    return {addr.lower() for _, addr in getaddresses([header_value or ""]) if addr}


def _sent_after(date_header, since):
    try:
        sent = parsedate_to_datetime(date_header)
    except (TypeError, ValueError, IndexError):
        return False
    if sent is None:
        return False
    if timezone.is_naive(sent):
        sent = timezone.make_aware(sent, dt_timezone.utc)
    return sent >= since - SENT_MATCH_SLACK


def _match(headers, *, recipient, since, message_id):
    """Evidence (dict) when these headers are the message we are looking for, else None."""
    if message_id and (headers.get("Message-ID") or "").strip() == message_id:
        how = "message-id"
    elif recipient.lower() in _recipients(headers.get("To")) and _sent_after(headers.get("Date"), since):
        how = "recipient-and-time"
    else:
        return None
    return {"match": how, "to": recipient, "subject": str(headers.get("Subject") or "")[:300],
            "date": str(headers.get("Date") or "")}


class LocalMailbox:
    """A mailbox in local folders (test mode). "Sending" a draft = moving its file from
    Drafts to Sent, which is what a local walkthrough does to play the user's part."""

    def __init__(self, account):
        root = getattr(settings, "EMAIL_DRAFTS_LOCAL_PATH", "") or ""
        if not root:
            base = getattr(settings, "EMAIL_FILE_PATH", "") or str(Path(settings.MEDIA_ROOT).parent / "local-mail")
            root = str(Path(base) / "mailbox")
        self.root = Path(root) / str(account.owner_id)
        self.drafts = self.root / "Drafts"
        self.sent = self.root / "Sent"
        self.description = "the local test mailbox"

    def __enter__(self):
        self.drafts.mkdir(parents=True, exist_ok=True)
        self.sent.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, *exc):
        return False

    def check(self):
        return {"drafts": "Drafts", "sent": "Sent"}

    def save_draft(self, message_bytes):
        name = f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1_000_000:06d}.eml"
        (self.drafts / name).write_bytes(message_bytes)
        return "Drafts"

    def find_sent(self, *, recipient, since, message_id):
        files = sorted(self.sent.glob("*.eml"), key=lambda f: f.stat().st_mtime, reverse=True)
        for path in files[:MAX_SENT_CANDIDATES]:
            found = _match(message_from_bytes(path.read_bytes()), recipient=recipient, since=since,
                           message_id=message_id)
            if found:
                return found
        return None


def _quote(name):
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


_LIST_LINE = re.compile(r'^\((?P<flags>[^)]*)\)\s+(?:"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.+)$')


def _parse_list(lines):
    """[(flags (lower-case set), name)] from an IMAP LIST response."""
    folders = []
    for raw in lines or []:
        if not isinstance(raw, bytes):
            continue  # literals come as tuples; not needed for the folders we look for
        match = _LIST_LINE.match(raw.decode("utf-8", "replace").strip())
        if not match:
            continue
        name = match.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = re.sub(r'\\(.)', r'\1', name[1:-1])
        folders.append(({f.lower() for f in match.group("flags").split()}, name))
    return folders


def _find_folder(folders, flag, names):
    for flags, name in folders:
        if flag in flags:
            return name
    by_name = {name.lower(): name for _, name in folders}
    for candidate in names:
        if candidate.lower() in by_name:
            return by_name[candidate.lower()]
    return ""


class ImapMailbox:
    """The user's real mailbox over IMAP (SSL)."""

    def __init__(self, account):
        self.account = account
        self.host = imap_host_for(account)
        self.port = account.imap_port or 993
        self.description = f"your mailbox ({self.host})"
        self.imap = None
        # Set by save_draft on Gmail: the saved draft's message id (hex), to open it directly.
        self.draft_gmail_id = ""

    def __enter__(self):
        if not self.host:
            raise MailboxError("Set the IMAP server of your email account (Account → Email sending) to save drafts.")
        try:
            password = decrypt(self.account.password_encrypted)
        except CredentialError as exc:
            raise MailboxError(str(exc)) from exc
        try:
            self.imap = imaplib.IMAP4_SSL(self.host, self.port, timeout=IMAP_TIMEOUT_SECONDS)
            self.imap.login(self.account.username.strip(), app_password(password))
        except imaplib.IMAP4.error as exc:
            self._close()
            raise MailboxError(login_problem(str(exc), self.account)) from exc
        except (socket.timeout, TimeoutError) as exc:
            self._close()
            raise MailboxError(f"Your mailbox ({self.host}) did not respond in time.") from exc
        except OSError as exc:
            self._close()
            raise MailboxError(f"Could not connect to your mailbox ({self.host}, {type(exc).__name__}).") from exc
        return self

    def __exit__(self, *exc):
        self._close()
        return False

    def _close(self):
        if self.imap is not None:
            try:
                self.imap.logout()
            except Exception:  # already closed or broken; nothing to do
                pass
            self.imap = None

    def _folders(self):
        status, lines = self.imap.list()
        if status != "OK":
            raise MailboxError("Could not list the folders of your mailbox.")
        return _parse_list(lines)

    def _folder(self, kind):
        folders = self._folders()
        if kind == "drafts":
            name = _find_folder(folders, "\\drafts", ("Drafts", "[Gmail]/Drafts", "INBOX.Drafts", "Draft"))
        else:
            name = _find_folder(folders, "\\sent", ("Sent", "Sent Items", "Sent Mail", "[Gmail]/Sent Mail",
                                                    "INBOX.Sent", "Sent Messages"))
        if not name:
            raise MailboxError(f"Could not find the {kind.capitalize()} folder of your mailbox.")
        return name

    def check(self):
        return {"drafts": self._folder("drafts"), "sent": self._folder("sent")}

    def save_draft(self, message_bytes):
        folder = self._folder("drafts")
        try:
            status, data = self.imap.append(_quote(folder), "(\\Draft \\Seen)",
                                            imaplib.Time2Internaldate(time.time()), message_bytes)
        except imaplib.IMAP4.error as exc:
            raise MailboxError("Your mailbox refused to save the draft.") from exc
        if status != "OK":
            raise MailboxError("Your mailbox refused to save the draft.")
        self.draft_gmail_id = self._gmail_id(folder, data)
        return folder

    def _gmail_id(self, folder, append_data):
        """Gmail only: the saved draft's message id in hex (used in Gmail's own links), from
        the APPENDUID answer and a read-only X-GM-MSGID fetch. "" when not available."""
        try:
            if "X-GM-EXT-1" not in getattr(self.imap, "capabilities", ()):
                return ""
            text = b" ".join(part for part in append_data or [] if isinstance(part, bytes))
            match = re.search(rb"APPENDUID \d+ (\d+)", text)
            if not match:
                return ""
            if self.imap.select(_quote(folder), readonly=True)[0] != "OK":
                return ""
            status, data = self.imap.uid("FETCH", match.group(1).decode(), "(X-GM-MSGID)")
            found = re.search(rb"X-GM-MSGID (\d+)", b" ".join(p for p in data or [] if isinstance(p, bytes)))
            return format(int(found.group(1)), "x") if status == "OK" and found else ""
        except Exception:  # only a nicer link; the draft is saved either way
            return ""

    def _search(self, *criteria):
        status, data = self.imap.search(None, *criteria)
        if status != "OK" or not data or not data[0]:
            return []
        return data[0].split()

    def find_sent(self, *, recipient, since, message_id):
        folder = self._folder("sent")
        status, _ = self.imap.select(_quote(folder), readonly=True)
        if status != "OK":
            raise MailboxError("Could not open the Sent folder of your mailbox.")
        ids = []
        if message_id:
            ids = self._search("HEADER", "Message-ID", _quote(message_id))
        if not ids:
            day = (since - SENT_MATCH_SLACK).astimezone(dt_timezone.utc)
            ids = self._search("SINCE", f"{day.day}-{_MONTHS[day.month - 1]}-{day.year}", "TO", _quote(recipient))
        parser = BytesHeaderParser()
        for message_number in reversed(ids[-MAX_SENT_CANDIDATES:]):
            status, data = self.imap.fetch(message_number, "(BODY.PEEK[HEADER.FIELDS (DATE TO SUBJECT MESSAGE-ID)])")
            if status != "OK":
                continue
            for part in data or []:
                if isinstance(part, tuple) and len(part) > 1:
                    found = _match(parser.parsebytes(part[1]), recipient=recipient, since=since,
                                   message_id=message_id)
                    if found:
                        return found
        return None
