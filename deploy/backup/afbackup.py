#!/usr/bin/python3 -I
"""
ApplyForge encrypted backup and restore (Milestone 8.4d, final security corrections).

Installed on the server as /usr/local/sbin/applyforge-backup (deploy/README.md,
section 12). Python standard library only. External tools, found only on a fixed
PATH and run with a minimal, explicit environment: age, pg_dump, pg_restore, createdb,
psql, systemctl, runuser. Linux is the supported platform; `verify` also runs on other
POSIX systems with age.

    applyforge-backup create          [--config /etc/applyforge/backup.conf]
    applyforge-backup verify          --archive FILE --identity FILE [--expected-sha256 HEX]
                                      [--config FILE] [--work-dir DIR] [--lock-file FILE]
    applyforge-backup restore         --archive FILE --identity FILE --expected-sha256 HEX
                                      --new-database NAME --new-media-dir DIR
                                      --confirm-database NAME --confirm-media-dir DIR
                                      [--config FILE] [--media-owner USER] [--allow-unindexed]
    applyforge-backup recover-service [--config FILE] [--discard-marker]

Trust model. age encrypts and integrity-protects an archive, but anyone holding the
PUBLIC recipient can create a valid archive, and the off-host share controls what it
returns on reads. The trusted SHA-256 of each archive is therefore computed from the
bytes age emits (streamed through this tool), recorded durably in a root-only local
index and the journal BEFORE the archive is published, and never derived from the
share. `restore` requires that hash (--expected-sha256), checks it against a private
local copy before decrypting, and - when a local index exists - requires the hash to be
in it. The dump is loaded by a dedicated, non-superuser restore role (its own OS
account, peer authentication) that owns nothing in production, has no memberships and
no dangerous attributes; its effective PostgreSQL identity is verified before use.

Exit status: 0 success; 1 failure; 2 refused (nothing was changed); 3 CRITICAL: the
service was running before the backup and could not be started again; 4 the archive
was created and recorded, but retention failed.
"""

import argparse
import contextlib
import ctypes
import datetime as dt
import errno
import hashlib
import io
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time

try:
    import fcntl
except ImportError:  # not POSIX
    fcntl = None

FORMAT = "applyforge-backup"
FORMAT_VERSION = 1
AGE_HEADER = b"age-encryption.org/v1\n"
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ARCHIVE_RE = re.compile(r"^applyforge-backup-(\d{8}T\d{6}Z)\.tar\.age$")
PARTIAL_RE = re.compile(r"^\.applyforge-backup-\d{8}T\d{6}Z\.tar\.age\.partial$")
WORK_DIR_RE = re.compile(r"^(create|restore|verify)-[a-z0-9_]{8}$")   # tempfile.mkdtemp names
MARKER = ".applyforge-backup-destination"
SERVICE_MARKER = "service-stopped-by-backup.json"
INDEX_NAME = "index.tsv"
INDEX_STATUSES = ("recorded", "failed")
ACTIVE_LOCK = ".active"
MANIFEST = "manifest.json"
DUMP = "database.dump"
MEDIA_PREFIX = "media/"

IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9@._-]+\.service$")
RECIPIENT_RE = re.compile(r"^age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}$")   # X25519 only
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FS_TYPE_RE = re.compile(r"^[a-z0-9._+-]{1,32}$")
TRANSITIONAL_STATES = {"activating", "deactivating", "reloading", "refreshing"}
RESERVED_DATABASES = {"postgres", "template0", "template1"}
PRIVILEGED_ACCOUNTS = {"root", "postgres"}
FORBIDDEN_DATA_PARENTS = ("/etc", "/root", "/home", "/srv/applyforge")
DANGEROUS_PREDEFINED_ROLES = ("pg_execute_server_program", "pg_read_server_files", "pg_write_server_files",
                              "pg_read_all_data", "pg_write_all_data")

MAX_TRUSTED_FILE_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 500_000
MAX_ARCHIVE_BYTES = 1 << 40               # 1 TiB
MAX_TAR_HEADER_BYTES = 1024 * 1024        # PAX / GNU long-name headers
SPACE_MARGIN_BYTES = 64 * 1024 * 1024
SPACE_MARGIN_RATIO = 0.10
STALE_GRACE_SECONDS = 15 * 60
FUTURE_TOLERANCE = dt.timedelta(days=1)
RESTART_ATTEMPTS = 3
RESTART_RETRY_SECONDS = 2
CHUNK = 1024 * 1024
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

# Module-level so the tests can point them at fixtures; never read from the environment.
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/opt/homebrew/bin"
TRUSTED_UID = 0
ENV_PASSTHROUGH_PREFIXES = ()   # empty in production: no inherited variable reaches a subprocess

EXIT_OK, EXIT_FAILED, EXIT_REFUSED, EXIT_SERVICE, EXIT_RETENTION = 0, 1, 2, 3, 4

DEFAULT_CONFIG = "/etc/applyforge/backup.conf"
DEFAULT_LOCK_FILE = "/run/lock/applyforge-backup.lock"
CONFIG_DEFAULTS = {
    "BACKUP_RETENTION_DAYS": "30",
    "BACKUP_MIN_KEEP": "7",
    "BACKUP_WORK_DIR": "/var/lib/applyforge-backup-work",
    "BACKUP_STATE_DIR": "/var/lib/applyforge-backup",
    "BACKUP_LOCK_FILE": DEFAULT_LOCK_FILE,
    "BACKUP_CAPTURE_TIMEOUT_SECONDS": "1800",
    "BACKUP_PG_LOCK_WAIT_SECONDS": "120",
    "BACKUP_ALLOWED_FS_TYPES": "",
    "DB_OWNER": "applyforge",
    "DB_OWNER_OS_USER": "",
    "RESTORE_ROLE": "applyforge_restore",
    "RESTORE_OS_USER": "",
    "PG_RUN_AS": "postgres",
    "SERVICE_NAME": "applyforge.service",
    "RELEASE_LINK": "/srv/applyforge/current",
}
CONFIG_REQUIRED_KEYS = ("BACKUP_DESTINATION", "BACKUP_RECIPIENTS_FILE", "MEDIA_ROOT", "DB_NAME")
CONFIG_KEYS = set(CONFIG_DEFAULTS) | set(CONFIG_REQUIRED_KEYS)
CONFIG_PATH_KEYS = ("BACKUP_DESTINATION", "BACKUP_RECIPIENTS_FILE", "MEDIA_ROOT", "BACKUP_WORK_DIR",
                    "BACKUP_STATE_DIR", "BACKUP_LOCK_FILE", "RELEASE_LINK")
CONFIG_INT_RANGES = {
    "BACKUP_RETENTION_DAYS": (1, 3650),
    "BACKUP_MIN_KEEP": (1, 1000),
    "BACKUP_CAPTURE_TIMEOUT_SECONDS": (1, 86400),
    "BACKUP_PG_LOCK_WAIT_SECONDS": (1, 3600),
}


class Refused(Exception):
    """A precondition is not met. Nothing was changed."""
    exit_code = EXIT_REFUSED


class Failed(Exception):
    """An operation failed. Anything this run created has been cleaned up or is reported."""
    exit_code = EXIT_FAILED


class ServiceRestartFailed(Exception):
    """The service was running before the backup and could not be started again."""
    exit_code = EXIT_SERVICE


class RetentionFailed(Exception):
    """The archive was created and recorded; only retention failed."""
    exit_code = EXIT_RETENTION


class Terminated(Exception):
    """SIGTERM/SIGHUP/SIGINT received; raised so that cleanup and the service restart run."""
    exit_code = EXIT_FAILED


def log(message):
    """Never raises: a closed or broken terminal must not interrupt cleanup or recovery."""
    try:
        print(f"applyforge-backup: {message}", file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def utcnow():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def clean_env():
    """The only environment any subprocess sees: fixed PATH and locale, nothing inherited
    (no PG*, AGE*, LD_*, ...)."""
    env = {"PATH": SAFE_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC", "HOME": "/"}
    if ENV_PASSTHROUGH_PREFIXES:
        env.update({k: v for k, v in os.environ.items() if k.startswith(ENV_PASSTHROUGH_PREFIXES)})
    return env


def run(args, new_session=False, **kwargs):
    """Run a command without a shell, with clean_env(). new_session keeps terminal
    signals (Ctrl-C, hang-up) away from service-management commands."""
    try:
        return subprocess.run(args, check=False, env=clean_env(), start_new_session=new_session, **kwargs)
    except OSError as exc:
        raise Failed(f"could not run {args[0]} ({exc.strerror})") from None


def last_line(data):
    """The most useful line of a tool's error output. age ends every error with a generic
    "report unexpected or unhelpful errors at ..." hint, which is skipped."""
    lines = [line for line in (data or b"").decode("utf-8", "replace").strip().splitlines() if line.strip()]
    useful = [line for line in lines if "report unexpected or unhelpful errors" not in line]
    lines = useful or lines
    return lines[-1][:300] if lines else "no error output"


def is_within(path, parent):
    path, parent = os.path.normpath(path), os.path.normpath(parent)
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def overlaps(a, b):
    a, b = os.path.realpath(a), os.path.realpath(b)
    return is_within(a, b) or is_within(b, a)


def sha256_hex(value):
    value = (value or "").strip().lower()
    return value if SHA256_RE.match(value) else None


# --- Signals ------------------------------------------------------------------------------

HANDLED_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def _raise_terminated(signum, frame):
    raise Terminated(f"terminated by {signal.Signals(signum).name}")


class DeferredSignals:
    """While active, termination signals are recorded instead of interrupting (used
    around service recovery, which must run to completion)."""

    def __enter__(self):
        self.received, self.previous = None, {}
        for signum in HANDLED_SIGNALS:
            self.previous[signum] = signal.signal(signum, self._record)
        return self

    def _record(self, signum, frame):
        self.received = self.received or signal.Signals(signum).name

    def __exit__(self, *exc):
        for signum, handler in self.previous.items():
            signal.signal(signum, handler)
        return False


# --- Platform, privileges and trusted files ------------------------------------------------

def require_posix():
    if os.name != "posix" or fcntl is None:
        raise Refused("this tool supports Linux (and, for verify, other POSIX systems); not this platform")


def require_root():
    if os.geteuid() != 0:
        raise Refused("must run as root (sudo); nothing was changed")


def check_trusted_ancestors(path, what):
    """Every directory above a trusted file must be owned by root and not writable by
    group or others (sticky directories such as /tmp excepted), so the file cannot be
    substituted after it has been checked."""
    directory = os.path.dirname(os.path.realpath(path))
    while True:
        st = os.lstat(directory)
        if not stat.S_ISDIR(st.st_mode):
            raise Refused(f"{what}: {directory} is not a directory")
        if st.st_uid not in (0, TRUSTED_UID):
            raise Refused(f"{what}: directory {directory} must be owned by root")
        if st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX:
            raise Refused(f"{what}: directory {directory} must not be writable by group or others")
        parent = os.path.dirname(directory)
        if parent == directory:
            return
        directory = parent


def read_trusted_file(path, what):
    """Open without following symlinks, validate the open descriptor (regular, root-owned,
    not group/world-writable) and its parent directories, and read from that descriptor."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise Refused(f"{what} {path} must be a regular file (not a symbolic link)") from None
        raise Refused(f"cannot read {what} {path} ({exc.strerror})") from None
    with os.fdopen(fd, "rb") as handle:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"{what} {path} must be a regular file (not a symbolic link)")
        if st.st_uid != TRUSTED_UID:
            raise Refused(f"{what} {path} must be owned by root")
        if st.st_mode & 0o022:
            raise Refused(f"{what} {path} must not be writable by group or others (chmod 644 or 600)")
        check_trusted_ancestors(path, what)
        data = handle.read(MAX_TRUSTED_FILE_BYTES + 1)
    if len(data) > MAX_TRUSTED_FILE_BYTES:
        raise Refused(f"{what} {path} is unreasonably large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused(f"{what} {path} is not valid UTF-8") from None


def check_private_dir(path, what):
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise Refused(f"{what} {path} is not a directory (or is a symbolic link)")
    if st.st_uid != TRUSTED_UID or st.st_mode & 0o077:
        raise Refused(f"{what} {path} must be owned by root and accessible to root only (chmod 700)")


def check_private_file(path, what):
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"{what} {path} is not a regular file (or is a symbolic link)")
    if st.st_uid != TRUSTED_UID or st.st_mode & 0o077:
        raise Refused(f"{what} {path} must be owned by root and readable by root only (chmod 600); "
                      "it may have been tampered with")


def os_user_exists(name):
    try:
        import pwd
        pwd.getpwnam(name)
        return True
    except (ImportError, KeyError):
        return False


# --- Configuration -------------------------------------------------------------------------

class Config:
    def __init__(self, values):
        self.destination = values.get("BACKUP_DESTINATION", "")
        self.recipients_file = values.get("BACKUP_RECIPIENTS_FILE", "")
        self.media_root = values.get("MEDIA_ROOT", "")
        self.db_name = values.get("DB_NAME", "")
        self.db_owner = values["DB_OWNER"]
        self.db_owner_os_user = values["DB_OWNER_OS_USER"] or values["DB_OWNER"]
        self.restore_role = values["RESTORE_ROLE"]
        self.restore_os_user = values["RESTORE_OS_USER"] or values["RESTORE_ROLE"]
        self.pg_run_as = values["PG_RUN_AS"]
        self.service_name = values["SERVICE_NAME"]
        self.work_dir = values["BACKUP_WORK_DIR"]
        self.state_dir = values["BACKUP_STATE_DIR"]
        self.lock_file = values["BACKUP_LOCK_FILE"]
        self.release_link = values["RELEASE_LINK"]
        self.retention_days = int(values["BACKUP_RETENTION_DAYS"])
        self.min_keep = int(values["BACKUP_MIN_KEEP"])
        self.capture_timeout = int(values["BACKUP_CAPTURE_TIMEOUT_SECONDS"])
        self.pg_lock_wait = int(values["BACKUP_PG_LOCK_WAIT_SECONDS"])
        self.allowed_fs_types = [t.strip() for t in values["BACKUP_ALLOWED_FS_TYPES"].split(",") if t.strip()]
        self.index_file = os.path.join(self.state_dir, INDEX_NAME)
        self.marker_file = os.path.join(self.state_dir, SERVICE_MARKER)


def load_config(path, required):
    text = read_trusted_file(path, "configuration file")
    values, seen = dict(CONFIG_DEFAULTS), set()
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in CONFIG_KEYS:
            raise Refused(f"{path} line {number}: unknown or malformed setting")
        if key in seen:
            raise Refused(f"{path} line {number}: duplicate setting {key}")
        seen.add(key)
        values[key] = value
    missing = [key for key in required if not values.get(key)]
    if missing:
        raise Refused(f"{path}: missing required setting(s): {', '.join(missing)}")
    for key in CONFIG_PATH_KEYS:
        value = values.get(key)
        if value and (not os.path.isabs(value) or os.path.normpath(value) != value):
            raise Refused(f"{key} must be an absolute, normalised path")
    for key, (low, high) in CONFIG_INT_RANGES.items():
        if not re.fullmatch(r"[0-9]{1,6}", values[key]) or not low <= int(values[key]) <= high:
            raise Refused(f"{key} must be a whole number from {low} to {high}")
    if values.get("DB_NAME") and not IDENT_RE.match(values["DB_NAME"]):
        raise Refused("DB_NAME is not a valid database name")
    if not IDENT_RE.match(values["DB_OWNER"]) or values["DB_OWNER"] in PRIVILEGED_ACCOUNTS:
        raise Refused("DB_OWNER must be the non-superuser application role (not postgres)")
    owner_os = values["DB_OWNER_OS_USER"] or values["DB_OWNER"]
    if not USER_RE.match(owner_os) or owner_os in PRIVILEGED_ACCOUNTS:
        raise Refused("DB_OWNER_OS_USER must be the application's OS account (not root or postgres)")
    restore_role = values["RESTORE_ROLE"]
    if not IDENT_RE.match(restore_role) or restore_role in PRIVILEGED_ACCOUNTS or restore_role == values["DB_OWNER"]:
        raise Refused("RESTORE_ROLE must be a dedicated non-superuser role, different from DB_OWNER (not postgres)")
    restore_os = values["RESTORE_OS_USER"] or restore_role
    if not USER_RE.match(restore_os) or restore_os in PRIVILEGED_ACCOUNTS or restore_os == owner_os:
        raise Refused("RESTORE_OS_USER must be a dedicated OS account, different from the application's "
                      "(not root or postgres)")
    if values["PG_RUN_AS"] and not USER_RE.match(values["PG_RUN_AS"]):
        raise Refused("PG_RUN_AS is not a valid user name")
    if not SERVICE_RE.match(values["SERVICE_NAME"]):
        raise Refused("SERVICE_NAME must be a systemd service unit name")
    for fs_type in values["BACKUP_ALLOWED_FS_TYPES"].split(","):
        if fs_type.strip() and not FS_TYPE_RE.match(fs_type.strip()):
            raise Refused("BACKUP_ALLOWED_FS_TYPES must be a comma-separated list of filesystem types")

    areas = {key: values[key] for key in ("BACKUP_DESTINATION", "BACKUP_WORK_DIR", "MEDIA_ROOT",
                                          "BACKUP_STATE_DIR") if values.get(key)}
    names = sorted(areas)
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            if overlaps(areas[first], areas[second]):
                raise Refused(f"{first} and {second} must be separate directories (one is inside the other)")
    for key in ("BACKUP_DESTINATION", "MEDIA_ROOT"):
        if values.get(key) and is_within(os.path.realpath(values["BACKUP_LOCK_FILE"]), os.path.realpath(values[key])):
            raise Refused(f"BACKUP_LOCK_FILE must not be inside {key}")
    if values.get("MEDIA_ROOT"):
        real_media = os.path.realpath(values["MEDIA_ROOT"])
        for parent in FORBIDDEN_DATA_PARENTS:
            if is_within(real_media, parent):
                raise Refused(f"MEDIA_ROOT must not be under {parent}")
    return Config(values)


def require_tools(names):
    missing = [name for name in names if shutil.which(name, path=SAFE_PATH) is None]
    if missing:
        raise Refused("required tool(s) not found on the fixed search path: " + ", ".join(missing)
                      + " - install them first (deploy/README.md section 12)")


def pg_command(cfg, args):
    """Administrative PostgreSQL commands run as the PostgreSQL OS user (peer authentication)."""
    return (["runuser", "-u", cfg.pg_run_as, "--"] if cfg.pg_run_as else []) + args


# --- Files, directories and locking --------------------------------------------------------

def check_readable_file(path, what):
    if not path or not os.path.isfile(path):
        raise Refused(f"{what} {path} does not exist or is not a regular file")
    try:
        with open(path, "rb"):
            pass
    except OSError as exc:
        raise Refused(f"cannot read {what} {path} ({exc.strerror}) - insufficient permissions?") from None


def prepare_work_dir(path):
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        st = os.lstat(path)
    except OSError as exc:
        raise Refused(f"cannot create work directory {path} ({exc.strerror})") from None
    if not stat.S_ISDIR(st.st_mode):
        raise Refused(f"work directory {path} is not a directory (or is a symbolic link)")
    if st.st_uid != os.getuid():
        raise Refused(f"work directory {path} is not owned by the current user")
    if st.st_mode & 0o077:
        raise Refused(f"work directory {path} must not be accessible to group or others (chmod 700)")


def prepare_state_dir(cfg):
    try:
        os.makedirs(cfg.state_dir, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise Refused(f"cannot create state directory {cfg.state_dir} ({exc.strerror})") from None
    check_private_dir(cfg.state_dir, "state directory")
    for path, what in ((cfg.index_file, "backup index"), (cfg.marker_file, "service marker")):
        if os.path.lexists(path):
            check_private_file(path, what)


@contextlib.contextmanager
def exclusive_lock(path, required=True):
    """The shared lock for create, verify, restore and recover-service (outside any
    PrivateTmp namespace). Read-only fallback still takes an exclusive flock. Yields True
    when the lock is held."""
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        if required:
            raise Refused(f"lock directory {directory} does not exist")
        log(f"note: {directory} does not exist; continuing without the shared lock")
        yield False
        return
    flags = os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | flags, 0o600)
    except OSError as exc:
        if exc.errno not in (errno.EROFS, errno.EACCES):
            raise Refused(f"cannot open lock file {path} ({exc.strerror})") from None
        try:
            fd = os.open(path, os.O_RDONLY | flags)
        except OSError as exc2:
            raise Refused(f"cannot open lock file {path} ({exc2.strerror}); install the tmpfiles.d rule") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f"lock file {path} is not a regular file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(f"another backup, verify or restore is running (lock {path} is held)") from None
        yield True
    finally:
        os.close(fd)


@contextlib.contextmanager
def private_temp_dir(parent, prefix):
    """A 0700 run directory holding an exclusive per-run lock (.active) for its whole life,
    so stale-directory cleanup can never remove it while it is in use."""
    path = tempfile.mkdtemp(prefix=prefix, dir=parent)
    fd = None
    try:
        if fcntl is not None:
            fd = os.open(os.path.join(path, ACTIVE_LOCK),
                         os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        if fd is not None:
            os.close(fd)


def run_dir_is_active(path):
    """True if another process holds the run directory's .active lock (or it cannot be checked)."""
    try:
        fd = os.open(os.path.join(path, ACTIVE_LOCK), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def remove_stale_dirs(parent, pattern, what, check_active=False):
    """Remove directories left by interrupted runs (call while holding the shared lock).
    Only real directories owned by this user, with the tool's own name pattern, not in
    use (per-run lock) and older than STALE_GRACE_SECONDS are removed."""
    removed = []
    now = time.time()
    try:
        entries = list(os.scandir(parent))
    except OSError:
        return removed
    for entry in entries:
        if not pattern.match(entry.name):
            continue
        try:
            st = os.lstat(entry.path)
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            log(f"note: {entry.path} looks like a {what} but is not a directory owned by this user; left alone")
            continue
        if check_active and run_dir_is_active(entry.path):
            log(f"note: {what} {entry.path} is in use by another run; left alone")
            continue
        if now - st.st_mtime < STALE_GRACE_SECONDS:
            log(f"note: recent {what} {entry.path} left in place (younger than {STALE_GRACE_SECONDS} s)")
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        removed.append(entry.name)
        log(f"removed stale {what} {entry.path} (left by an interrupted run)")
    return removed


def cleanup_stale_work_dirs(work_dir):
    return remove_stale_dirs(work_dir, WORK_DIR_RE, "work directory", check_active=True)


def cleanup_stale_staging(parent, basename):
    pattern = re.compile(r"^\." + re.escape(basename) + r"\.restoring-[a-z0-9_]{8}$")
    return remove_stale_dirs(parent, pattern, "restore staging directory")


class HashingReader:
    def __init__(self, handle):
        self.handle, self.count, self.digest = handle, 0, hashlib.sha256()

    def read(self, size=-1):
        data = self.handle.read(size)
        self.count += len(data)
        self.digest.update(data)
        return data

    def hexdigest(self):
        return self.digest.hexdigest()


def open_regular_nofollow(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    handle = os.fdopen(fd, "rb")
    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
        handle.close()
        raise Failed(f"{path} is not a regular file")
    return handle


def sha256_file(path):
    with open_regular_nofollow(path) as handle:
        reader = HashingReader(handle)
        while reader.read(CHUNK):
            pass
    return reader.hexdigest(), reader.count


def fsync_directory(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def disk_free(path):
    return shutil.disk_usage(path).free


def require_space(path, needed, what):
    required = needed + max(SPACE_MARGIN_BYTES, int(needed * SPACE_MARGIN_RATIO))
    free = disk_free(path)
    if free < required:
        raise Failed(f"insufficient disk space in {path} to {what}: about {required} bytes needed "
                     f"(including a safety margin), {free} available")


# --- Destination and recipients ---------------------------------------------------------------

def is_mount_point(path):
    return os.path.ismount(path)


def mount_fs_type(path):
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    found = None
    for line in lines:
        left, _, right = line.partition(" - ")
        fields = left.split()
        if len(fields) < 5 or not right:
            continue
        mount_point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
        if mount_point == path:
            found = right.split()[0]
    return found


def check_destination(cfg):
    dest = cfg.destination
    if os.path.islink(dest) or not os.path.isdir(dest):
        raise Refused(f"backup destination {dest} does not exist or is a symbolic link")
    if not is_mount_point(dest):
        raise Refused(f"backup destination {dest} is not a mount point: the off-host storage is "
                      "not mounted. Refusing to write backups to the local disk.")
    if cfg.allowed_fs_types:
        fs_type = mount_fs_type(dest)
        if fs_type not in cfg.allowed_fs_types:
            raise Refused(f"backup destination {dest} has filesystem type {fs_type!r}, which is not in "
                          f"BACKUP_ALLOWED_FS_TYPES ({', '.join(cfg.allowed_fs_types)})")
    try:
        marker = os.lstat(os.path.join(dest, MARKER))
    except OSError:
        marker = None
    if marker is None or not stat.S_ISREG(marker.st_mode):
        raise Refused(f"backup destination {dest} has no {MARKER} marker file (a regular file): the wrong "
                      "filesystem is mounted, or the destination was never prepared")
    if not os.access(dest, os.W_OK | os.X_OK):
        raise Refused(f"backup destination {dest} is not writable - insufficient permissions")


def check_recipients(path):
    text = read_trusted_file(path, "recipients file")
    if "AGE-SECRET-KEY-" in text.upper():
        raise Refused(f"{path} contains an age PRIVATE key. Only public recipients (age1...) "
                      "belong on the server; remove the private key from this machine.")
    recipients = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not recipients:
        raise Refused(f"{path} lists no age recipients")
    for recipient in recipients:
        if not RECIPIENT_RE.match(recipient):
            raise Refused(f"{path} contains an entry that is not an age X25519 public recipient (age1...); "
                          "SSH, plugin and post-quantum recipients are not supported")
    return recipients


def recipients_fingerprint(recipients):
    return hashlib.sha256("\n".join(sorted(set(recipients))).encode("ascii")).hexdigest()


# --- Media: descriptor-relative, race-resistant traversal ----------------------------------------

DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


def check_media_root(path):
    if os.path.islink(path) or not os.path.isdir(path):
        raise Refused(f"MEDIA_ROOT {path} does not exist or is a symbolic link")
    if not os.access(path, os.R_OK | os.X_OK):
        raise Refused(f"MEDIA_ROOT {path} is not readable - insufficient permissions")


def check_deadline(deadline):
    if deadline is not None and time.monotonic() > deadline:
        raise Failed("the capture deadline (BACKUP_CAPTURE_TIMEOUT_SECONDS) was exceeded")


def _lstat_at(name, dir_fd):
    return os.stat(name, dir_fd=dir_fd, follow_symlinks=False)


def _incomplete(rel, exc):
    if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENXIO):
        return Failed(f"{rel} under MEDIA_ROOT changed during the backup (replaced by a link or another "
                      "file type); refusing")
    return Failed(f"cannot read {rel} under MEDIA_ROOT ({exc.strerror}); the backup would be incomplete")


def scan_media(root, deadline=None):
    """Walk MEDIA_ROOT with directory descriptors only (never by path), never following
    symlinks or crossing filesystems; open every directory and file relative to its
    parent descriptor with O_NOFOLLOW and check it is the object just listed. Returns
    (files, skipped, root_identity); each file records parts, size, SHA-256, dev and ino."""
    try:
        root_fd = os.open(root, DIR_FLAGS)
    except OSError as exc:
        raise Failed(f"cannot open MEDIA_ROOT {root} ({exc.strerror})") from None
    files, skipped = [], []
    try:
        root_st = os.fstat(root_fd)
        _scan_dir(root_fd, [], root_st.st_dev, files, skipped, deadline)
        return files, sorted(skipped), (root_st.st_dev, root_st.st_ino)
    finally:
        os.close(root_fd)


def _scan_dir(dir_fd, parts, root_dev, files, skipped, deadline):
    check_deadline(deadline)
    here = "/".join(parts) or "."
    try:
        names = sorted(os.listdir(dir_fd))
    except OSError as exc:
        raise _incomplete(here, exc) from None
    for name in names:
        rel_parts = parts + [name]
        rel = "/".join(rel_parts)
        try:
            st = _lstat_at(name, dir_fd)
        except OSError as exc:
            raise _incomplete(rel, exc) from None
        if stat.S_ISLNK(st.st_mode) or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
            skipped.append(rel)
            continue
        if st.st_dev != root_dev:
            raise Failed(f"{rel} is on a different filesystem than MEDIA_ROOT; refusing to cross filesystems")
        if stat.S_ISDIR(st.st_mode):
            try:
                child = os.open(name, DIR_FLAGS, dir_fd=dir_fd)
            except OSError as exc:
                raise _incomplete(rel, exc) from None
            try:
                cst = os.fstat(child)
                if (cst.st_dev, cst.st_ino) != (st.st_dev, st.st_ino):
                    raise Failed(f"{rel} under MEDIA_ROOT changed during the scan (directory swap?); refusing")
                _scan_dir(child, rel_parts, root_dev, files, skipped, deadline)
            finally:
                os.close(child)
            continue
        try:
            fd = os.open(name, FILE_FLAGS, dir_fd=dir_fd)
        except OSError as exc:
            raise Failed(f"cannot read media file {rel} ({exc.strerror}); the backup would be incomplete"
                         if exc.errno not in (errno.ELOOP, errno.ENXIO) else
                         f"media file {rel} changed during the scan; refusing") from None
        with os.fdopen(fd, "rb") as handle:
            fst = os.fstat(handle.fileno())
            if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
                raise Failed(f"media file {rel} changed during the scan; refusing")
            reader = HashingReader(handle)
            try:
                while reader.read(CHUNK):
                    pass
            except OSError as exc:
                raise _incomplete(rel, exc) from None
        files.append({"path": rel, "parts": rel_parts, "size": reader.count, "sha256": reader.hexdigest(),
                      "dev": st.st_dev, "ino": st.st_ino})


def open_media_file(root_fd, entry, root_dev):
    """Re-open a scanned file relative to the MEDIA_ROOT descriptor, one O_NOFOLLOW
    directory at a time."""
    current, opened = root_fd, []
    try:
        for part in entry["parts"][:-1]:
            nxt = os.open(part, DIR_FLAGS, dir_fd=current)
            opened.append(nxt)
            if os.fstat(nxt).st_dev != root_dev:
                raise Failed(f"{entry['path']}: a directory moved to another filesystem during the backup")
            current = nxt
        return os.open(entry["parts"][-1], FILE_FLAGS, dir_fd=current)
    except OSError as exc:
        raise Failed(f"{MEDIA_PREFIX}{entry['path']} changed while the backup was running ({exc.strerror})") from None
    finally:
        for fd in opened:
            os.close(fd)


# --- Service maintenance window and recovery marker -----------------------------------------------

def service_properties(name, *props):
    result = run(["systemctl", "show", *[f"--property={p}" for p in props], name],
                 capture_output=True, new_session=True)
    if result.returncode != 0:
        raise Refused(f"cannot determine the state of {name}")
    values = {}
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    if any(not values.get(p) and p == "ActiveState" for p in props) or any(p not in values for p in props):
        raise Refused(f"cannot determine the state of {name}")
    return values


def service_state(name):
    return service_properties(name, "ActiveState")["ActiveState"]


def service_times(name):
    values = service_properties(name, "ActiveState", "ActiveEnterTimestampMonotonic", "InactiveEnterTimestampMonotonic")
    try:
        return (values["ActiveState"], int(values["ActiveEnterTimestampMonotonic"] or 0),
                int(values["InactiveEnterTimestampMonotonic"] or 0))
    except ValueError:
        raise Refused(f"cannot determine the state of {name}") from None


def stop_service(name):
    log(f"stopping {name} for the backup maintenance window")
    result = run(["systemctl", "stop", name], capture_output=True, new_session=True)
    if result.returncode != 0:
        raise Failed(f"could not stop {name}: {last_line(result.stderr)}")
    state = service_state(name)
    if state not in ("inactive", "failed"):
        raise Failed(f"{name} is still {state} after stopping it")


def start_service(name):
    log(f"starting {name} again")
    result = run(["systemctl", "start", name], capture_output=True, new_session=True)
    if result.returncode != 0:
        raise ServiceRestartFailed(f"systemctl start {name} failed: {last_line(result.stderr)}")
    state = service_state(name)
    if state != "active":
        raise ServiceRestartFailed(f"{name} is {state} after starting it")


def start_service_with_retry(name):
    """Bounded retries; any ordinary exception counts as a failed attempt."""
    last = None
    for attempt in range(1, RESTART_ATTEMPTS + 1):
        try:
            start_service(name)
            return
        except Exception as exc:  # noqa: BLE001 - every failure mode must lead to a retry or exit 3
            last = exc
            log(f"start attempt {attempt}/{RESTART_ATTEMPTS} for {name} failed: {exc}")
            if attempt < RESTART_ATTEMPTS:
                time.sleep(RESTART_RETRY_SECONDS)
    raise ServiceRestartFailed(f"{type(last).__name__}: {last}")


def monotonic_usec():
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000   # the clock systemd's *Monotonic values use


def current_boot_id():
    try:
        with open(BOOT_ID_PATH, encoding="ascii") as handle:
            return handle.read().strip()
    except OSError:
        return ""


def write_service_marker(cfg, **fields):
    """Durable record that THIS run stopped the service (atomic replace, fsync)."""
    data = {"version": 1, "service": cfg.service_name, "boot_id": current_boot_id(),
            "invocation_id": os.environ.get("INVOCATION_ID", ""), "pid": os.getpid(),
            "written_utc": utcnow().strftime(ISO_FORMAT)}
    data.update(fields)
    payload = json.dumps(data).encode("utf-8")
    temporary = cfg.marker_file + ".tmp"
    with contextlib.suppress(FileNotFoundError):
        os.unlink(temporary)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, cfg.marker_file)
    fsync_directory(cfg.state_dir)


def clear_service_marker(cfg):
    with contextlib.suppress(FileNotFoundError):
        os.unlink(cfg.marker_file)
    fsync_directory(cfg.state_dir)


def read_service_marker(cfg):
    check_private_file(cfg.marker_file, "service marker")
    try:
        with open(cfg.marker_file, encoding="utf-8") as handle:
            marker = json.load(handle)
    except (OSError, ValueError, RecursionError):
        return None
    return marker if isinstance(marker, dict) else None


def assess_marker(cfg, marker):
    """('obsolete' | 'superseded' | 'ambiguous' | 'ours', reason). Only 'ours' - same boot,
    and the service has neither been started nor stopped since this tool stopped it - may
    lead to a start, and only when the caller is allowed to start it."""
    if marker is None or marker.get("service") != cfg.service_name:
        return "ambiguous", "the marker is unreadable or names another service"
    try:
        state, active_enter, inactive_enter = service_times(cfg.service_name)
    except Refused as exc:
        return "ambiguous", str(exc)
    if state == "active":
        return "obsolete", f"{cfg.service_name} is active again"
    if state in TRANSITIONAL_STATES:
        return "ambiguous", f"{cfg.service_name} is {state}"
    boot = marker.get("boot_id")
    if not boot or boot != current_boot_id():
        return "ambiguous", "the system has rebooted since the backup stopped the service"
    requested, recorded = marker.get("stop_requested_usec"), marker.get("inactive_enter_usec")
    if not isinstance(requested, int):
        return "ambiguous", "the marker does not record when the stop was requested"
    if active_enter >= requested:
        return "superseded", (f"{cfg.service_name} was started again after the backup stopped it (and is now "
                              f"{state}): a later deliberate action")
    phase = marker.get("phase")
    if phase in ("stopped", "restart-failed") and isinstance(recorded, int):
        if inactive_enter == recorded:
            return "ours", "stopped by the backup and untouched since"
        return "superseded", f"{cfg.service_name} was stopped again after the backup's stop: a later deliberate action"
    if phase == "stopping" and inactive_enter >= requested:
        return "ours", "stopped by the backup (interrupted while stopping) and untouched since"
    return "ambiguous", "the marker's state cannot be matched to the service's history"


def recover_from_marker(cfg, mode):
    """mode: 'create' (never starts), 'automatic' (ExecStopPost of the SAME invocation) or
    'manual' (an operator ran recover-service). Returns True if a marker was handled."""
    if not os.path.lexists(cfg.marker_file):
        return False
    marker = read_service_marker(cfg)
    verdict, reason = assess_marker(cfg, marker)
    name = cfg.service_name
    if verdict == "obsolete":
        clear_service_marker(cfg)
        log(f"stale service marker removed: {reason}")
        return True
    if verdict == "superseded":
        clear_service_marker(cfg)
        log(f"not starting {name}: {reason}. The service marker was removed; the service stays as it is.")
        return True
    if verdict == "ambiguous":
        raise Refused(f"{name} is not running and a backup service marker exists, but {reason}. Not starting it. "
                      "Investigate, start the service yourself if appropriate, then run: "
                      "applyforge-backup recover-service --discard-marker")
    if mode == "create":
        raise Refused(f"{name} was stopped by an earlier backup that did not start it again. It is NOT started "
                      "automatically. Check it, then run: applyforge-backup recover-service")
    if mode == "automatic":
        invocation = marker.get("invocation_id")
        if not invocation or invocation != os.environ.get("INVOCATION_ID"):
            raise Refused(f"the service marker belongs to another run; not starting {name} automatically "
                          "(run applyforge-backup recover-service manually after checking)")
    log(f"{name}: {reason}; starting it again")
    with DeferredSignals():
        start_service_with_retry(name)
        clear_service_marker(cfg)
    log(f"{name} is active; service marker cleared")
    return True


# --- Create -------------------------------------------------------------------------------------------

def dump_database(cfg, path, deadline):
    log(f"dumping database {cfg.db_name} (pg_dump custom format)")
    remaining = max(1.0, deadline - time.monotonic())
    try:
        with open(path, "xb") as out:
            result = run(pg_command(cfg, ["pg_dump", "--format=custom", "--no-password",
                                          f"--lock-wait-timeout={cfg.pg_lock_wait * 1000}",
                                          f"--dbname={cfg.db_name}"]),
                         stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.PIPE, timeout=remaining)
    except subprocess.TimeoutExpired:
        raise Failed("pg_dump did not finish before the capture deadline (BACKUP_CAPTURE_TIMEOUT_SECONDS)") from None
    if result.returncode != 0:
        raise Failed(f"pg_dump failed (exit {result.returncode}): {last_line(result.stderr)}")
    digest, size = sha256_file(path)
    if size == 0:
        raise Failed("pg_dump produced an empty dump")
    return {"path": DUMP, "size": size, "sha256": digest}


def tool_version(tool):
    result = run([tool, "--version"], capture_output=True)
    return result.stdout.decode("utf-8", "replace").strip()[:200] if result.returncode == 0 else "unknown"


def current_release(link):
    try:
        return os.path.basename(os.path.realpath(link)) if os.path.islink(link) else None
    except OSError:
        return None


def _add_member(tar, name, handle, entry):
    st = os.fstat(handle.fileno())
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = entry["size"], 0o600, int(st.st_mtime)
    reader = HashingReader(handle)
    tar.addfile(info, reader)
    if reader.count != entry["size"] or reader.hexdigest() != entry["sha256"]:
        raise Failed(f"{name} changed while the backup was running")


def write_tar(tar_path, manifest, dump_path, dump_entry, media_root, media_identity, media_files, deadline=None):
    """manifest.json, the dump, then every media file re-opened descriptor-relative and
    checked against the scan (same inode, size and SHA-256)."""
    data = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    try:
        with tarfile.open(tar_path, "x", format=tarfile.PAX_FORMAT) as tar:
            info = tarfile.TarInfo(MANIFEST)
            info.size, info.mode, info.mtime = len(data), 0o600, int(utcnow().timestamp())
            tar.addfile(info, io.BytesIO(data))
            with open_regular_nofollow(dump_path) as handle:
                if os.fstat(handle.fileno()).st_size != dump_entry["size"]:
                    raise Failed(f"{DUMP} changed while the backup was running")
                _add_member(tar, DUMP, handle, dump_entry)
            root_fd = os.open(media_root, DIR_FLAGS)
            try:
                root_st = os.fstat(root_fd)
                if (root_st.st_dev, root_st.st_ino) != media_identity:
                    raise Failed("MEDIA_ROOT was replaced while the backup was running")
                for entry in media_files:
                    check_deadline(deadline)
                    member = MEDIA_PREFIX + entry["path"]
                    with os.fdopen(open_media_file(root_fd, entry, root_st.st_dev), "rb") as handle:
                        st = os.fstat(handle.fileno())
                        if (not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (entry["dev"], entry["ino"])
                                or st.st_size != entry["size"]):
                            raise Failed(f"{member} changed while the backup was running")
                        _add_member(tar, member, handle, entry)
            finally:
                os.close(root_fd)
    except (OSError, tarfile.TarError) as exc:
        raise Failed(f"could not write the backup archive ({exc})") from None


def stream_age_encrypt(cfg, tar_path, out_fd, stderr_path):
    """Run age with stdout piped into this process; hash every chunk as age emits it and
    write it to the partial file's descriptor. Returns the TRUSTED (sha256, size)."""
    with open(stderr_path, "xb") as err:
        try:
            proc = subprocess.Popen(["age", "--encrypt", "--recipients-file", cfg.recipients_file, tar_path],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err, env=clean_env())
        except OSError as exc:
            raise Failed(f"could not run age ({exc.strerror})") from None
    digest, size, head = hashlib.sha256(), 0, b""
    try:
        while True:
            chunk = proc.stdout.read(CHUNK)
            if not chunk:
                break
            if len(head) < len(AGE_HEADER):
                head += chunk[:len(AGE_HEADER) - len(head)]
            digest.update(chunk)
            size += len(chunk)
            view = memoryview(chunk)
            while view:
                view = view[os.write(out_fd, view):]
        returncode = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
    if returncode != 0:
        with open(stderr_path, "rb") as err:
            raise Failed(f"age encryption failed (exit {returncode}): {last_line(err.read(65536))}")
    if head != AGE_HEADER:
        raise Failed("age output does not start with an age header")
    return digest.hexdigest(), size


def remove_if_ours(path, identity):
    try:
        st = os.lstat(path)
    except OSError:
        return
    if stat.S_ISREG(st.st_mode) and (st.st_dev, st.st_ino) == identity:
        with contextlib.suppress(OSError):
            os.unlink(path)


def hash_published_candidate(path, identity):
    """Re-read the file on the destination (tamper/corruption DETECTION only - never the
    source of the trusted hash)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as handle:
        st = os.fstat(handle.fileno())
        if (st.st_dev, st.st_ino) != identity:
            raise Failed("the partial file was replaced while encrypting; refusing to publish it")
        reader = HashingReader(handle)
        while reader.read(CHUNK):
            pass
    return reader.hexdigest(), reader.count


def rename_noreplace(source, target):
    """renameat2(RENAME_NOREPLACE): atomic, and fails with EEXIST instead of replacing."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is not available")
    at_fdcwd, rename_noreplace_flag = -100, 1
    if renameat2(at_fdcwd, os.fsencode(source), at_fdcwd, os.fsencode(target), rename_noreplace_flag) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def publish(partial, final, identity):
    """Give the finished file its final name, never replacing an existing archive: a hard
    link, or renameat2(RENAME_NOREPLACE) where hard links are unsupported. Otherwise fail
    closed - there is no check-then-rename fallback."""
    try:
        os.link(partial, final, follow_symlinks=False)
    except FileExistsError:
        raise Failed(f"{os.path.basename(final)} appeared in the destination; not overwriting it") from None
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOTSUP, errno.EMLINK, errno.ENOSYS):
            raise
        try:
            rename_noreplace(partial, final)
        except FileExistsError:
            raise Failed(f"{os.path.basename(final)} appeared in the destination; not overwriting it") from None
        except OSError as exc2:
            if exc2.errno in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP, errno.ENOTSUP):
                raise Failed("the destination filesystem supports neither hard links nor RENAME_NOREPLACE, so an "
                             "archive cannot be published without risking overwriting another one; refusing "
                             "(deploy/README.md section 12.3)") from None
            raise
        return
    remove_if_ours(partial, identity)


def encrypt_to_destination(cfg, tar_path, name, work, record):
    """Encrypt, verify, record the trusted hash durably, then publish.

    1. Create ".<name>.partial" with O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW, mode 0600.
    2. Stream age's stdout through this process into that descriptor, hashing it (the
       trusted hash). fchmod, fsync, close.
    3. Re-read the partial (same inode) and require the same hash - a share that alters
       or corrupts data is detected; the re-read never becomes the trusted hash.
    4. Re-check the destination; append the hash to the root-only index ("recorded") and
       the journal; only then publish without overwriting; check the published file is
       this run's inode. On any failure after recording, a "failed" record follows, and
       only this run's own partial file is removed."""
    dest = cfg.destination
    final = os.path.join(dest, name)
    partial = os.path.join(dest, f".{name}.partial")
    if os.path.lexists(final):
        raise Failed(f"{name} already exists in the backup destination; not overwriting it")
    try:
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        raise Failed(f"{os.path.basename(partial)} already exists in the destination (left by an interrupted "
                     "run, or planted); not touching it") from None
    except OSError as exc:
        raise Failed(f"cannot create {partial} ({exc.strerror})") from None
    st = os.fstat(fd)
    identity = (st.st_dev, st.st_ino)
    closed = recorded = published = done = False
    digest = size = None
    log(f"encrypting the archive with age into {dest}")
    try:
        digest, size = stream_age_encrypt(cfg, tar_path, fd, os.path.join(work, "age.stderr"))
        os.fchmod(fd, 0o600)
        os.fsync(fd)
        os.close(fd)
        closed = True
        reread, reread_size = hash_published_candidate(partial, identity)
        if (reread, reread_size) != (digest, size):
            raise Failed("the ciphertext read back from the destination differs from what age produced: the "
                         "share altered or corrupted it; not publishing")
        try:
            check_destination(cfg)
        except Refused as exc:
            raise Failed(f"destination check before publication failed: {exc}") from None
        record(digest, size, "recorded")
        recorded = True
        log(f"archive {name} sha256={digest} size={size} recorded in {cfg.index_file}")
        publish(partial, final, identity)
        published = True
        fsync_directory(dest)
        final_st = os.lstat(final)
        if not stat.S_ISREG(final_st.st_mode) or (final_st.st_dev, final_st.st_ino) != identity:
            raise Failed(f"the published {name} is not the file this run wrote (the share replaced it); "
                         "its index record is marked failed")
        done = True
    except OSError as exc:
        raise Failed(f"could not write the encrypted backup ({exc.strerror or exc})") from None
    finally:
        if not closed:
            with contextlib.suppress(OSError):
                os.close(fd)
        if not published:
            remove_if_ours(partial, identity)
        if recorded and not done:
            try:
                record(digest, size, "failed")
            except Exception as exc:  # noqa: BLE001
                log(f"warning: could not mark {name} as failed in the index ({exc})")
    return final, digest, size


def index_line_fields(created, name, digest, size, fingerprint, host, status):
    return [created, name, digest, str(size), fingerprint, host, status]


def append_index(cfg, fields):
    """Append one record to the root-only local index (never the backup share)."""
    if len(fields) != 7 or fields[6] not in INDEX_STATUSES or any("\t" in f or "\n" in f for f in fields):
        raise Failed("internal error: malformed index record")
    fd = os.open(cfg.index_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != TRUSTED_UID or st.st_mode & 0o077:
            raise Failed(f"backup index {cfg.index_file} has unsafe ownership or permissions")
        os.write(fd, ("\t".join(fields) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_directory(cfg.state_dir)


def read_index(cfg):
    """Trusted records from the local index: {archive name: sha256} for archives whose
    latest record is 'recorded'. None if there is no index. Unsafe permissions or any
    malformed line refuse (fail closed)."""
    if not os.path.lexists(cfg.index_file):
        return None
    check_private_dir(cfg.state_dir, "state directory")
    check_private_file(cfg.index_file, "backup index")
    with open_regular_nofollow(cfg.index_file) as handle:
        try:
            lines = handle.read().decode("utf-8").splitlines()
        except UnicodeDecodeError:
            raise Refused(f"backup index {cfg.index_file} is not valid UTF-8; refusing to trust it") from None
    latest = {}
    for number, line in enumerate(lines, 1):
        parts = line.split("\t")
        valid = (len(parts) == 7 and ARCHIVE_RE.match(parts[1]) and SHA256_RE.match(parts[2])
                 and parts[3].isdigit() and SHA256_RE.match(parts[4]) and parts[6] in INDEX_STATUSES)
        if valid:
            try:
                dt.datetime.strptime(parts[0], ISO_FORMAT)
            except ValueError:
                valid = False
        if not valid:
            raise Refused(f"backup index {cfg.index_file} is malformed at line {number}; refusing to trust it "
                          "(inspect it manually)")
        latest[parts[1]] = (parts[2], parts[6])
    return {name: digest for name, (digest, status) in latest.items() if status == "recorded"}


def apply_retention(destination, retention_days, now, keep_name=None, min_keep=7, trusted=None):
    """Delete complete archives older than the retention period. The `min_keep` newest are
    always kept - preferring archives in the trusted local index; archives dated more than
    a day in the future never take a protected slot (and are never age-deleted). Only
    regular files named exactly applyforge-backup-<timestamp>.tar.age that start with an
    age header qualify."""
    trusted = trusted or {}
    cutoff = now - dt.timedelta(days=retention_days)
    future_limit = now + FUTURE_TOLERANCE
    candidates, partials = [], 0
    for entry in os.scandir(destination):
        if PARTIAL_RE.match(entry.name):
            partials += 1
            continue
        match = ARCHIVE_RE.match(entry.name)
        if not match or not entry.is_file(follow_symlinks=False):
            continue
        try:
            stamp = dt.datetime.strptime(match.group(1), TIMESTAMP_FORMAT).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        try:
            with open_regular_nofollow(entry.path) as handle:
                if handle.read(len(AGE_HEADER)) != AGE_HEADER:
                    log(f"retention: {entry.name} has no age header; not a candidate")
                    continue
        except (OSError, Failed):
            continue
        candidates.append((stamp, entry.name))
    if partials:
        log(f"note: {partials} incomplete '.partial' file(s) in {destination} are not backups and are left for review")
    future = [c for c in candidates if c[0] > future_limit]
    if future:
        log(f"warning: {len(future)} archive name(s) dated in the future (e.g. {future[0][1]}); they take no "
            "protected retention slot and are left for review")
    eligible = [c for c in candidates if c[0] <= future_limit]
    indexed = sorted((c for c in eligible if c[1] in trusted), reverse=True)
    others = sorted((c for c in eligible if c[1] not in trusted), reverse=True)
    protected = {name for _, name in indexed[:min_keep]}
    if len(protected) < min_keep:
        protected |= {name for _, name in others[:min_keep - len(protected)]}
    protected.add(keep_name)
    deleted = []
    for stamp, name in sorted(eligible):
        if stamp >= cutoff or name in protected:
            continue
        os.unlink(os.path.join(destination, name))
        deleted.append(name)
        log(f"retention: deleted {name} (older than {retention_days} days; {min_keep} newest always kept)")
    return deleted


def cmd_create(args):
    require_posix()
    require_root()
    cfg = load_config(args.config, CONFIG_REQUIRED_KEYS)
    require_tools(["age", "pg_dump", "systemctl"] + (["runuser"] if cfg.pg_run_as else []))
    check_destination(cfg)
    fingerprint = recipients_fingerprint(check_recipients(cfg.recipients_file))
    check_media_root(cfg.media_root)
    prepare_state_dir(cfg)
    prepare_work_dir(cfg.work_dir)

    with exclusive_lock(cfg.lock_file):
        trusted = read_index(cfg) or {}
        recover_from_marker(cfg, "create")
        cleanup_stale_work_dirs(cfg.work_dir)
        now = utcnow()
        name = f"applyforge-backup-{now.strftime(TIMESTAMP_FORMAT)}.tar.age"
        host = socket.gethostname()
        with private_temp_dir(cfg.work_dir, "create-") as work:
            state = service_state(cfg.service_name)
            if state in TRANSITIONAL_STATES:
                raise Refused(f"{cfg.service_name} is {state}; try again when it is settled")
            was_active = state == "active"
            tar_path = os.path.join(work, "backup.tar")
            deadline = time.monotonic() + cfg.capture_timeout

            capture_error, stop_attempted, requested = None, False, None
            try:
                if was_active:
                    requested = monotonic_usec()
                    write_service_marker(cfg, phase="stopping", stop_requested_usec=requested)
                    stop_attempted = True
                    stop_service(cfg.service_name)
                    _, _, inactive = service_times(cfg.service_name)
                    write_service_marker(cfg, phase="stopped", stop_requested_usec=requested,
                                         inactive_enter_usec=inactive)
                else:
                    log(f"{cfg.service_name} is {state}; it will not be started afterwards")
                dump_path = os.path.join(work, DUMP)
                dump_entry = dump_database(cfg, dump_path, deadline)
                media_files, skipped, media_identity = scan_media(cfg.media_root, deadline)
                if skipped:
                    log(f"WARNING: {len(skipped)} entr{'y' if len(skipped) == 1 else 'ies'} under MEDIA_ROOT are "
                        f"not regular files and were NOT backed up (listed in the manifest): {', '.join(skipped[:10])}")
                manifest = {
                    "format": FORMAT,
                    "format_version": FORMAT_VERSION,
                    "created_utc": now.strftime(ISO_FORMAT),
                    "archive_name": name,
                    "host": host,
                    "application_release": current_release(cfg.release_link),
                    "recipients_fingerprint": fingerprint,
                    "database": {"name": cfg.db_name, "dump_file": DUMP, "dump_format": "pg_dump custom",
                                 "pg_dump_version": tool_version("pg_dump")},
                    "media": {"source": cfg.media_root, "file_count": len(media_files),
                              "total_bytes": sum(f["size"] for f in media_files),
                              "skipped_not_regular_files": skipped},
                    "service": {"name": cfg.service_name, "state_before": state, "stopped_during_capture": was_active},
                    "files": [dump_entry] + [{"path": MEDIA_PREFIX + f["path"], "size": f["size"],
                                              "sha256": f["sha256"]} for f in media_files],
                }
                write_tar(tar_path, manifest, dump_path, dump_entry, cfg.media_root, media_identity,
                          media_files, deadline)
                os.unlink(dump_path)
            except BaseException as exc:  # noqa: BLE001 - restore the service first, then re-raise
                capture_error = exc

            if stop_attempted:
                restart_error = None
                with DeferredSignals() as deferred:
                    try:
                        start_service_with_retry(cfg.service_name)
                    except ServiceRestartFailed as exc:
                        restart_error = exc
                        try:
                            _, _, inactive = service_times(cfg.service_name)
                            write_service_marker(cfg, phase="restart-failed", stop_requested_usec=requested,
                                                 inactive_enter_usec=inactive)
                        except Exception as marker_exc:  # noqa: BLE001
                            log(f"warning: could not update the service marker ({marker_exc})")
                    else:
                        try:
                            clear_service_marker(cfg)
                        except OSError as exc:
                            log(f"warning: could not remove the service marker ({exc.strerror})")
                if restart_error is not None:
                    if capture_error is not None:
                        log(f"the backup itself also failed: {capture_error}")
                    raise ServiceRestartFailed(
                        f"CRITICAL: {cfg.service_name} was running before the backup and could not be started "
                        f"again ({restart_error}). The service marker {cfg.marker_file} is kept. Start the service "
                        f"(systemctl start {cfg.service_name}) or run: applyforge-backup recover-service") from None
                if deferred.received and capture_error is None:
                    capture_error = Terminated(f"terminated by {deferred.received}")
            if capture_error is not None:
                raise capture_error

            def record(digest, size, status):
                append_index(cfg, index_line_fields(now.strftime(ISO_FORMAT), name, digest, size,
                                                    fingerprint, host, status))

            final, digest, size = encrypt_to_destination(cfg, tar_path, name, work, record)

        log(f"backup complete: {final} sha256={digest} size={size} recipients={fingerprint[:16]} "
            f"({len(media_files)} media file(s), database dump {dump_entry['size']} bytes)")
        trusted[name] = digest
        try:
            apply_retention(cfg.destination, cfg.retention_days, now, keep_name=name, min_keep=cfg.min_keep,
                            trusted=trusted)
        except Terminated as exc:
            raise Terminated(f"archive {name} was created and recorded (sha256 {digest}); {exc} during "
                             "retention (nothing else is affected)") from None
        except Exception as exc:  # noqa: BLE001 - reported distinctly from creation failures
            raise RetentionFailed(f"archive {name} was created and recorded (sha256 {digest}), but RETENTION FAILED: "
                                  f"{type(exc).__name__}: {exc}") from None
    return EXIT_OK


# --- Verify -------------------------------------------------------------------------------------------

def validate_member_name(name):
    if name in (MANIFEST, DUMP):
        return
    parts = name.split("/")
    if (not name.startswith(MEDIA_PREFIX) or len(parts) < 2 or "\x00" in name or len(name) > 4096
            or any(part in ("", ".", "..") for part in parts)):
        raise Failed(f"archive member {name!r} has an unexpected or unsafe name")


def check_member_type(member):
    if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or getattr(member, "sparse", None) is not None:
        raise Failed(f"archive member {member.name!r} is not a plain regular file "
                     f"(type {member.type!r}); links, devices, directories and sparse files are not accepted")


def _tar_number(field):
    if field[0] & 0x80:
        return int.from_bytes(field[1:], "big")
    text = field.split(b"\0", 1)[0].strip()
    try:
        return int(text or b"0", 8)
    except ValueError:
        raise Failed("the decrypted data is not a valid backup archive (malformed tar header)") from None


def precheck_tar_headers(path):
    """Walk the raw 512-byte tar headers before tarfile parses anything: every header's
    checksum must be valid and its data must fit in the file; PAX/GNU long headers
    (tarfile reads them into memory) and the member count are bounded."""
    members = 0
    file_size = os.path.getsize(path)
    with open(path, "rb") as handle:
        while True:
            block = handle.read(512)
            if len(block) < 512 or block == b"\0" * 512:
                return
            stored = _tar_number(block[148:156])
            computed = sum(block[:148]) + 8 * 32 + sum(block[156:])
            if stored != computed:
                raise Failed("the decrypted data is not a valid backup archive (tar header checksum mismatch)")
            kind = block[156:157]
            size = _tar_number(block[124:136])
            if size > file_size - handle.tell():
                raise Failed("the decrypted data is not a valid backup archive (tar member extends past the end)")
            if kind in (b"x", b"g", b"L", b"K"):
                if size > MAX_TAR_HEADER_BYTES:
                    raise Failed(f"archive contains an oversized extended header ({size} bytes)")
            else:
                members += 1
                if members > MAX_ARCHIVE_MEMBERS:
                    raise Failed(f"archive has more than {MAX_ARCHIVE_MEMBERS} members")
            handle.seek((size + 511) // 512 * 512, os.SEEK_CUR)


def validate_manifest(manifest):
    def bad(reason):
        return Failed(f"invalid manifest: {reason}")
    if not isinstance(manifest, dict):
        raise bad("not a JSON object")
    if manifest.get("format") != FORMAT:
        raise bad("not an ApplyForge backup")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise bad(f"unsupported format_version {manifest.get('format_version')!r}")
    try:
        dt.datetime.strptime(str(manifest.get("created_utc")), ISO_FORMAT)
    except ValueError:
        raise bad("created_utc missing or malformed") from None
    database = manifest.get("database")
    if (not isinstance(database, dict) or not IDENT_RE.match(str(database.get("name", "")))
            or database.get("dump_file") != DUMP):
        raise bad("database section missing or malformed")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise bad("files list missing")
    if len(files) > MAX_ARCHIVE_MEMBERS:
        raise bad(f"more than {MAX_ARCHIVE_MEMBERS} files listed")
    seen = set()
    for entry in files:
        if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                or not isinstance(entry.get("size"), int) or isinstance(entry.get("size"), bool)
                or entry["size"] < 0 or not SHA256_RE.match(str(entry.get("sha256", "")))):
            raise bad("malformed file entry")
        if entry["path"] == MANIFEST:
            raise bad("manifest lists itself")
        validate_member_name(entry["path"])
        if entry["path"] in seen:
            raise bad(f"duplicate entry {entry['path']!r}")
        seen.add(entry["path"])
    if DUMP not in seen:
        raise bad("no database dump listed")


def extract_and_validate(tar_path, dest):
    precheck_tar_headers(tar_path)
    try:
        with tarfile.open(tar_path, "r:") as tar:
            members, names = [], set()
            for member in tar:
                if len(members) >= MAX_ARCHIVE_MEMBERS:
                    raise Failed(f"archive has more than {MAX_ARCHIVE_MEMBERS} members")
                check_member_type(member)
                validate_member_name(member.name)
                if member.name in names:
                    raise Failed(f"archive member {member.name!r} appears twice")
                names.add(member.name)
                members.append(member)
            if MANIFEST not in names:
                raise Failed("archive has no manifest.json")
            manifest_member = next(m for m in members if m.name == MANIFEST)
            if manifest_member.size > MAX_MANIFEST_BYTES:
                raise Failed("manifest.json is unreasonably large")
            try:
                manifest = json.loads(tar.extractfile(manifest_member).read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise Failed("invalid manifest: not valid JSON") from None
            except RecursionError:
                raise Failed("invalid manifest: nested too deeply") from None
            validate_manifest(manifest)
            listed = {entry["path"]: entry for entry in manifest["files"]}
            unlisted = names - set(listed) - {MANIFEST}
            missing = set(listed) - names
            if unlisted:
                raise Failed(f"archive contains file(s) not listed in the manifest: {sorted(unlisted)[:5]}")
            if missing:
                raise Failed(f"archive is missing file(s) listed in the manifest: {sorted(missing)[:5]}")
            data_members = [m for m in members if m.name != MANIFEST]
            for member in data_members:
                if member.size != listed[member.name]["size"]:
                    raise Failed(f"size of {member.name} in the archive ({member.size}) does not match the "
                                 f"manifest ({listed[member.name]['size']})")
            require_space(dest, sum(m.size for m in data_members), "extract the archive")
            for member in data_members:
                entry = listed[member.name]
                target = os.path.join(dest, *member.name.split("/"))
                if not is_within(target, dest):
                    raise Failed(f"archive member {member.name!r} escapes the extraction directory")
                os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
                reader = HashingReader(tar.extractfile(member))
                with open(target, "xb") as out:
                    while True:
                        chunk = reader.read(CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                if reader.count != entry["size"] or reader.hexdigest() != entry["sha256"]:
                    raise Failed(f"checksum mismatch for {member.name}")
    except (tarfile.TarError, EOFError) as exc:
        raise Failed(f"the decrypted data is not a valid backup archive ({exc})") from None
    except OSError as exc:
        raise Failed(f"could not extract the archive ({exc.strerror})") from None
    return manifest


def copy_archive(source, work):
    """Copy the archive into the private work directory while hashing it; everything later
    uses this local copy, so the share cannot change it after the hash check."""
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as exc:
        reason = "it is a symbolic link" if exc.errno == errno.ELOOP else exc.strerror
        raise Refused(f"cannot open archive {source} ({reason})") from None
    local = os.path.join(work, "archive.age")
    with os.fdopen(fd, "rb") as src:
        st = os.fstat(src.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"archive {source} is not a regular file")
        if st.st_size > MAX_ARCHIVE_BYTES:
            raise Refused(f"archive {source} is larger than the limit of {MAX_ARCHIVE_BYTES} bytes")
        require_space(work, st.st_size, "copy the archive")
        reader = HashingReader(src)
        with open(local, "xb") as out:
            while True:
                chunk = reader.read(CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                if reader.count > MAX_ARCHIVE_BYTES:
                    raise Refused(f"archive {source} grew beyond the limit of {MAX_ARCHIVE_BYTES} bytes")
    return local, reader.hexdigest(), reader.count


def decrypt_and_validate(local_archive, identity, work):
    with open(local_archive, "rb") as handle:
        if handle.read(len(AGE_HEADER)) != AGE_HEADER:
            raise Failed("not an age-encrypted file (no age header): wrong file, or corrupted")
    require_space(work, os.path.getsize(local_archive), "decrypt the archive")
    plain = os.path.join(work, "archive.tar")
    result = run(["age", "--decrypt", "--identity", identity, "--output", plain, local_archive],
                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode != 0:
        with contextlib.suppress(OSError):
            os.unlink(plain)
        raise Failed("decryption failed: wrong identity, or the archive is corrupted or incomplete "
                     f"(age: {last_line(result.stderr)})")
    extracted = os.path.join(work, "extracted")
    os.mkdir(extracted, 0o700)
    manifest = extract_and_validate(plain, extracted)
    os.unlink(plain)
    return manifest, extracted


def describe(manifest):
    media = [e for e in manifest["files"] if e["path"].startswith(MEDIA_PREFIX)]
    dump = next(e for e in manifest["files"] if e["path"] == DUMP)
    return (f"created {manifest['created_utc']} on {manifest.get('host')}, database {manifest['database']['name']} "
            f"({dump['size']} bytes), {len(media)} media file(s), release {manifest.get('application_release')}")


def cmd_verify(args):
    require_posix()
    expected = None
    if args.expected_sha256:
        expected = sha256_hex(args.expected_sha256)
        if expected is None:
            raise Refused("--expected-sha256 must be 64 hexadecimal characters")
    cfg = load_config(args.config, ()) if args.config else None
    lock_file = cfg.lock_file if cfg else args.lock_file
    work_dir = args.work_dir or (cfg.work_dir if cfg else None)
    require_tools(["age"])
    check_readable_file(args.archive, "archive")
    check_readable_file(args.identity, "identity file")
    if work_dir:
        prepare_work_dir(work_dir)
    with exclusive_lock(lock_file, required=bool(cfg)) as locked:
        if locked and work_dir:
            cleanup_stale_work_dirs(work_dir)
        with private_temp_dir(work_dir or tempfile.gettempdir(), "verify-") as work:
            local, digest, _ = copy_archive(args.archive, work)
            if expected and digest != expected:
                raise Refused(f"the archive's SHA-256 ({digest}) does not match --expected-sha256; not decrypting it")
            manifest, _ = decrypt_and_validate(local, args.identity, work)
    print(f"OK: archive verified - {describe(manifest)}")
    print(f"archive sha256: {digest}")
    print(f"recipients fingerprint: {manifest.get('recipients_fingerprint')}")
    if not expected:
        print("note: no --expected-sha256 given. age does not prove who created an archive: compare the hash "
              "above with your independent record before trusting it.")
    return EXIT_OK


# --- Restore ----------------------------------------------------------------------------------------------

def check_media_target(path):
    if not os.path.lexists(path):
        return
    if os.path.islink(path) or not os.path.isdir(path):
        raise Refused(f"{path} exists and is not a directory; refusing to touch it")
    if os.listdir(path):
        raise Refused(f"{path} is not empty; refusing to restore over existing files")


def psql_rows(command_prefix, sql):
    result = run(command_prefix + ["psql", "--no-psqlrc", "--no-password", "--tuples-only", "--no-align",
                                   "--field-separator=|", "--dbname=postgres", f"--command={sql}"],
                 stdin=subprocess.DEVNULL, capture_output=True)
    if result.returncode != 0:
        raise Failed(f"database query failed: {last_line(result.stderr)}")
    return [line.strip() for line in result.stdout.decode("utf-8", "replace").splitlines() if line.strip()]


def psql_query(cfg, sql):
    return psql_rows(pg_command(cfg, []), sql)


def database_exists(cfg, name):
    return name in psql_query(cfg, "SELECT datname FROM pg_catalog.pg_database")


IDENTITY_SQL = (
    "SELECT current_user, session_user, r.rolsuper, r.rolcreaterole, r.rolcreatedb, r.rolbypassrls, "
    "r.rolreplication, (SELECT count(*) FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid), "
    + " OR ".join(f"pg_catalog.pg_has_role(current_user, '{role}', 'MEMBER')" for role in DANGEROUS_PREDEFINED_ROLES)
    + " FROM pg_catalog.pg_roles r WHERE r.rolname = current_user"
)


def check_restore_identity(cfg):
    """Verify the identity and privileges of the connection pg_restore WILL use: connect
    exactly the same way (runuser -u RESTORE_OS_USER, peer authentication) and inspect it.
    Fails closed if the check cannot be completed."""
    try:
        rows = psql_rows(["runuser", "-u", cfg.restore_os_user, "--"], IDENTITY_SQL)
    except Failed as exc:
        raise Refused(f"cannot connect as the restore account {cfg.restore_os_user} (peer authentication): {exc}. "
                      "Create the restore role and OS account first (deploy/README.md section 12.8)") from None
    fields = rows[0].split("|") if len(rows) == 1 else []
    if len(fields) != 9:
        raise Refused("could not verify the restore connection's identity (unexpected answer); refusing")
    current, session, superuser, createrole, createdb, bypassrls, replication, memberships, dangerous = fields
    if current != cfg.restore_role or session != cfg.restore_role:
        raise Refused(f"the restore account connects as PostgreSQL role {current!r} (session {session!r}), not "
                      f"{cfg.restore_role}: an unexpected peer/pg_ident mapping; refusing")
    problems = [label for label, value in (("superuser", superuser), ("CREATEROLE", createrole),
                                           ("CREATEDB", createdb), ("BYPASSRLS", bypassrls),
                                           ("REPLICATION", replication)) if value != "f"]
    if memberships != "0":
        problems.append("member of other roles")
    if dangerous != "f":
        problems.append("member of a server-program/server-file/all-data role")
    if problems:
        raise Refused(f"restore role {cfg.restore_role} must be a plain non-superuser role; it has: "
                      f"{', '.join(problems)}")


def check_restore_role_isolated(cfg):
    """The restore role must own nothing in, and hold no privileges on, the production
    database (pg_shdepend is shared across databases)."""
    sql = ("SELECT (SELECT count(*) FROM pg_catalog.pg_database d WHERE d.datname = '{db}' AND d.datdba = r.oid) + "
           "(SELECT count(*) FROM pg_catalog.pg_shdepend s JOIN pg_catalog.pg_database x ON x.oid = s.dbid "
           "WHERE s.refobjid = r.oid AND s.deptype IN ('o', 'a') AND x.datname = '{db}') "
           "FROM pg_catalog.pg_roles r WHERE r.rolname = '{role}'").format(db=cfg.db_name, role=cfg.restore_role)
    rows = psql_query(cfg, sql)
    if rows != ["0"]:
        raise Refused(f"restore role {cfg.restore_role} owns objects in, or has privileges on, the production "
                      f"database {cfg.db_name} (or could not be checked); it must be isolated from production")


def database_owner(cfg, name):
    rows = psql_query(cfg, "SELECT pg_catalog.pg_get_userbyid(datdba) FROM pg_catalog.pg_database "
                           f"WHERE datname = '{name}'")
    return rows[0] if rows else None


RESTORED_DIR_MODE = 0o750
RESTORED_FILE_MODE = 0o640


def copy_media(extracted, manifest, staging):
    """Copy media into the private staging directory with explicit modes: directories
    0750, files 0640. Modes are set with chmod because the process umask (0077) would
    otherwise reduce the directories os.makedirs creates to 0700."""
    for entry in manifest["files"]:
        if not entry["path"].startswith(MEDIA_PREFIX):
            continue
        target = os.path.join(staging, *entry["path"][len(MEDIA_PREFIX):].split("/"))
        os.makedirs(os.path.dirname(target), mode=RESTORED_DIR_MODE, exist_ok=True)
        shutil.copyfile(os.path.join(extracted, *entry["path"].split("/")), target)
        os.chmod(target, RESTORED_FILE_MODE)
    # Staging is created by this run (mkdtemp, root-only parent) and contains only the
    # directories and regular files written above - never a symlink - so chmod is safe.
    for dirpath, dirnames, _ in os.walk(staging):
        for name in dirnames:
            path = os.path.join(dirpath, name)
            if not stat.S_ISDIR(os.lstat(path).st_mode):
                raise Failed(f"unexpected non-directory {path} in the restore staging area")
            os.chmod(path, RESTORED_DIR_MODE)


def chown_tree(path, user):
    import pwd
    record = pwd.getpwnam(user)
    for dirpath, _, filenames in os.walk(path):
        os.chown(dirpath, record.pw_uid, record.pw_gid)
        for name in filenames:
            os.chown(os.path.join(dirpath, name), record.pw_uid, record.pw_gid, follow_symlinks=False)


def cmd_restore(args):
    require_posix()
    require_root()
    cfg = load_config(args.config, ("DB_NAME", "MEDIA_ROOT"))
    if not args.expected_sha256:
        raise Refused("--expected-sha256 is required: the archive's SHA-256 from an independent, trusted record "
                      "(the server's index or journal, or your offline copy). age does not prove who created an archive.")
    expected = sha256_hex(args.expected_sha256)
    if expected is None:
        raise Refused("--expected-sha256 must be 64 hexadecimal characters")
    new_db, target = args.new_database, args.new_media_dir

    # --- Everything below is checked before any change is made. ---
    if not IDENT_RE.match(new_db) or new_db in RESERVED_DATABASES:
        raise Refused("--new-database must be a plain new database name (lowercase letters, digits, _)")
    if new_db == cfg.db_name:
        raise Refused(f"{new_db} is the production database (DB_NAME); restore only into a NEW database")
    if args.confirm_database != new_db:
        raise Refused("--confirm-database must repeat --new-database exactly")
    if not os_user_exists(cfg.restore_os_user):
        raise Refused(f"OS account {cfg.restore_os_user} (RESTORE_OS_USER) does not exist; pg_restore runs as that "
                      "dedicated account (deploy/README.md section 12.8)")
    if not os.path.isabs(target) or os.path.normpath(target) != target:
        raise Refused("--new-media-dir must be an absolute, normalised path")
    if args.confirm_media_dir != target:
        raise Refused("--confirm-media-dir must repeat --new-media-dir exactly")
    real_target = os.path.realpath(target)
    for area in (cfg.media_root, cfg.destination, cfg.work_dir, cfg.state_dir):
        if area and overlaps(real_target, area):
            raise Refused(f"{target} overlaps {area} (production media, backup destination, work or state "
                          "directory); use a separate new directory")
    for parent_dir in FORBIDDEN_DATA_PARENTS:
        if is_within(real_target, parent_dir):
            raise Refused(f"--new-media-dir must not be under {parent_dir}")
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        raise Refused(f"parent directory {parent} does not exist")
    if not os.access(parent, os.W_OK | os.X_OK):
        raise Refused(f"cannot create {target}: no write permission in {parent}")
    check_media_target(target)
    if args.media_owner:
        if not USER_RE.match(args.media_owner) or not os_user_exists(args.media_owner):
            raise Refused(f"--media-owner {args.media_owner} is not an existing user")
    check_readable_file(args.archive, "archive")
    check_readable_file(args.identity, "identity file")
    require_tools(["age", "psql", "createdb", "pg_restore", "runuser"])
    prepare_work_dir(cfg.work_dir)

    with exclusive_lock(cfg.lock_file):
        index = read_index(cfg) if os.path.isdir(cfg.state_dir) else None
        cleanup_stale_work_dirs(cfg.work_dir)
        cleanup_stale_staging(parent, os.path.basename(target))
        with private_temp_dir(cfg.work_dir, "restore-") as work:
            local, digest, _ = copy_archive(args.archive, work)
            if digest != expected:
                raise Refused(f"the archive's SHA-256 ({digest}) does not match --expected-sha256; refusing to "
                              "decrypt or restore it")
            archive_name = os.path.basename(args.archive)
            if index is not None:
                if archive_name in index and index[archive_name] != digest:
                    raise Refused(f"the server's backup index records a different SHA-256 for {archive_name}; "
                                  "refusing to restore it")
                if digest not in index.values():
                    if not args.allow_unindexed:
                        raise Refused(f"SHA-256 {digest} is not recorded in the local backup index {cfg.index_file}. "
                                      "Use --allow-unindexed only for an archive from another server whose hash comes "
                                      "from an independent offline record (deploy/README.md section 12.6)")
                    log(f"WARNING: restoring an archive that is not in the local index (--allow-unindexed); its "
                        f"trust rests entirely on your independent record of sha256 {digest}")
            manifest, extracted = decrypt_and_validate(local, args.identity, work)
            log(f"archive verified (sha256 {digest}): {describe(manifest)}")
            check_restore_identity(cfg)
            check_restore_role_isolated(cfg)
            if database_exists(cfg, new_db):
                raise Refused(f"database {new_db} already exists; refusing to touch it")
            check_media_target(target)
            media_bytes = sum(e["size"] for e in manifest["files"] if e["path"].startswith(MEDIA_PREFIX))
            require_space(parent, media_bytes, "stage the restored media")

            # Changes start here.
            staging = tempfile.mkdtemp(prefix=f".{os.path.basename(target)}.restoring-", dir=parent)
            left_db = (f"Database {new_db} was created by this restore and is left in place for review; drop it "
                       f"manually once reviewed: sudo -u postgres dropdb {new_db}")
            try:
                copy_media(extracted, manifest, staging)
                log(f"creating database {new_db} (owner: restore role {cfg.restore_role})")
                result = run(pg_command(cfg, ["createdb", "--no-password", "--template=template0",
                                              f"--owner={cfg.restore_role}", new_db]),
                             stdin=subprocess.DEVNULL, capture_output=True)
                if result.returncode != 0:
                    raise Failed(f"createdb failed, nothing was restored: {last_line(result.stderr)}")
                actual_owner = database_owner(cfg, new_db)
                if actual_owner != cfg.restore_role:
                    raise Failed(f"database {new_db} is owned by {actual_owner!r}, not {cfg.restore_role}; nothing "
                                 f"was restored. {left_db}")
                log(f"restoring the dump into {new_db} as the isolated restore account {cfg.restore_os_user}")
                with open(os.path.join(extracted, DUMP), "rb") as dump:
                    # Never as postgres or the production owner: the dump's SQL runs with the
                    # isolated restore role's rights only.
                    result = run(["runuser", "-u", cfg.restore_os_user, "--", "pg_restore", "--no-password",
                                  "--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges",
                                  f"--dbname={new_db}"],
                                 stdin=dump, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                if result.returncode != 0:
                    raise Failed(f"pg_restore failed: {last_line(result.stderr)}. The restore ran in a single "
                                 f"transaction, so the database holds no restored data. {left_db}")
                os.chmod(staging, RESTORED_DIR_MODE)
                if args.media_owner:
                    chown_tree(staging, args.media_owner)
                try:
                    os.rename(staging, target)  # local, root-only parent: replaces only an EMPTY directory
                except OSError as exc:
                    raise Failed(f"could not move restored media to {target} ({exc.strerror}); database {new_db} "
                                 "was restored and is left in place") from None
                staging = None
            finally:
                if staging is not None:
                    shutil.rmtree(staging, ignore_errors=True)

    print(f"OK: restored archive (sha256 {digest}) into NEW database {new_db} (owned by the isolated restore role "
          f"{cfg.restore_role}) and NEW media directory {target}.")
    print("The running application was NOT changed and ownership was NOT transferred. Verify the restored data;")
    print("switching production is a separately authorized step (deploy/README.md section 12.7).")
    return EXIT_OK


def cmd_recover_service(args):
    """ExecStopPost (automatic: only for the marker of the SAME invocation) or an operator
    (manual). Starts the service only if this tool stopped it and nothing touched it since."""
    require_posix()
    require_root()
    cfg = load_config(args.config, ())
    if not os.path.lexists(cfg.marker_file):
        log("no service marker; nothing to recover")
        return EXIT_OK
    with exclusive_lock(cfg.lock_file):
        if args.discard_marker:
            read_service_marker(cfg)  # permission check
            clear_service_marker(cfg)
            log(f"service marker discarded; {cfg.service_name} was not started or stopped")
            return EXIT_OK
        recover_from_marker(cfg, "automatic" if os.environ.get("INVOCATION_ID") else "manual")
    return EXIT_OK


# --- Command line --------------------------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(prog="applyforge-backup", description="ApplyForge encrypted backup and restore.")
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create", help="create an encrypted backup (maintenance window)")
    create.add_argument("--config", default=DEFAULT_CONFIG)
    create.set_defaults(func=cmd_create)

    verify = commands.add_parser("verify", help="decrypt and validate an archive; changes nothing")
    verify.add_argument("--archive", required=True)
    verify.add_argument("--identity", required=True, help="age identity file (keep it offline)")
    verify.add_argument("--expected-sha256", help="archive SHA-256 from an independent record")
    verify.add_argument("--config", help="on the server: use its lock and work directory")
    verify.add_argument("--work-dir", help="private directory for temporary plaintext (default: system temp)")
    verify.add_argument("--lock-file", default=DEFAULT_LOCK_FILE, help="lock when --config is not given")
    verify.set_defaults(func=cmd_verify)

    restore = commands.add_parser("restore", help="restore into a NEW database and a NEW media directory")
    restore.add_argument("--config", default=DEFAULT_CONFIG)
    restore.add_argument("--archive", required=True)
    restore.add_argument("--identity", required=True, help="age identity file (keep it offline)")
    restore.add_argument("--expected-sha256", help="REQUIRED: archive SHA-256 from an independent record")
    restore.add_argument("--new-database", required=True)
    restore.add_argument("--new-media-dir", required=True)
    restore.add_argument("--confirm-database", required=True, help="repeat --new-database")
    restore.add_argument("--confirm-media-dir", required=True, help="repeat --new-media-dir")
    restore.add_argument("--media-owner", help="chown restored media to this user")
    restore.add_argument("--allow-unindexed", action="store_true",
                         help="recovery exception: the hash is not in the local index (archive from another server)")
    restore.set_defaults(func=cmd_restore)

    recover = commands.add_parser("recover-service",
                                  help="start the service if a killed backup left it stopped (never if ambiguous)")
    recover.add_argument("--config", default=DEFAULT_CONFIG)
    recover.add_argument("--discard-marker", action="store_true",
                         help="after investigating: remove the marker without starting or stopping anything")
    recover.set_defaults(func=cmd_recover_service)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    os.environ["PATH"] = SAFE_PATH
    previous_umask = os.umask(0o077)
    previous = {signum: signal.signal(signum, _raise_terminated) for signum in HANDLED_SIGNALS}
    try:
        return args.func(args)
    except Refused as exc:
        log(f"REFUSED: {exc}")
        return exc.exit_code
    except (Failed, ServiceRestartFailed, RetentionFailed, Terminated) as exc:
        log(f"FAILED: {exc}")
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - clean message instead of a traceback
        log(f"FAILED: unexpected error ({type(exc).__name__}: {exc})")
        return EXIT_FAILED
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        os.umask(previous_umask)


if __name__ == "__main__":
    sys.exit(main())
