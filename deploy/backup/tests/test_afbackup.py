"""
Milestone 8.4d: tests for deploy/backup/afbackup.py (including both security passes).

POSIX only (the tool targets Linux): run on Linux or WSL with

    python3 -m unittest discover -s deploy/backup/tests -v

Safety: every external tool the backup tool calls (age, pg_dump, pg_restore, createdb,
psql, systemctl, runuser) is replaced by a fictional stand-in in a temporary bin
directory, which is the tool's only search path (afbackup.SAFE_PATH is pointed at it)
and is checked to shadow every real tool. No real database, service, mount or key is
touched. All data, recipients and identities are fictional.

The stand-in "age" follows age's command-line contract (--encrypt to stdout or
--output, --decrypt, --recipients-file, --identity) and authenticates its output with
HMAC-SHA256, so a wrong identity, a flipped byte or a truncated file is rejected the
way real age rejects them. It is NOT real cryptography. The stand-in "runuser" really
executes the wrapped stand-in and records the account it runs as; the stand-in
"systemctl" keeps ActiveState and the Active/InactiveEnterTimestampMonotonic values.
RealAgeTests use the real age binary with a throwaway identity and are skipped when age
is not installed.

Test seams patched in every test: require_root, TRUSTED_UID, SAFE_PATH,
ENV_PASSTHROUGH_PREFIXES (only FAKE_* variables reach the stand-ins), RESTART_RETRY_SECONDS,
is_mount_point, os_user_exists and current_boot_id. Tests of those checks undo the patch.
"""

import contextlib
import datetime as dt
import errno
import fcntl
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import afbackup  # noqa: E402

BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
RECIPIENT = "age1" + (BECH32 * 2)[:58]            # fictional, syntactically valid
OTHER_RECIPIENT = "age1" + (BECH32[::-1] * 2)[:58]
TOOLS = ("age", "pg_dump", "pg_restore", "createdb", "psql", "systemctl", "runuser")
REAL_AGE = shutil.which("age")
REAL_AGE_KEYGEN = shutil.which("age-keygen")
ORIGINAL_REQUIRE_ROOT = afbackup.require_root
FIXED_NOW = dt.datetime(2026, 11, 18, 3, 30, 12, tzinfo=dt.timezone.utc)
RESTORE_ROLE = "applyforge_restore"

FAKE_TOOL = r'''
"""Fictional stand-ins for the tools afbackup calls. Test use only."""
import hashlib, hmac, json, os, re, sys, time

NAME = os.path.basename(sys.argv[0])
ARGS = sys.argv[1:]
STATE = os.environ["FAKE_STATE"]
HEADER = b"age-encryption.org/v1\n"


def path(name):
    return os.path.join(STATE, name)


def read(name, default=""):
    try:
        with open(path(name)) as fh:
            return fh.read()
    except FileNotFoundError:
        return default


def write(name, text):
    with open(path(name), "w") as fh:
        fh.write(text)


def service():
    return read("service_state", "active").strip()


def times():
    return json.loads(read("times", '{"active_enter": 1, "inactive_enter": 0}'))


def now_usec():
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC) // 1000


def record():
    leaks = sorted(k for k in os.environ if k.startswith(("PG", "AGE", "LD_")))
    with open(path("calls.log"), "a") as fh:
        fh.write(json.dumps({"tool": NAME, "args": ARGS, "service": service(),
                             "as": os.environ.get("FAKE_RUN_AS", "caller"), "env_leaks": leaks}) + "\n")


def opt(*flags, required=True):
    for i, arg in enumerate(ARGS):
        if arg in flags and i + 1 < len(ARGS):
            return ARGS[i + 1]
    if required:
        fail(f"{NAME}: missing option {flags[0]}")
    return None


def fail(message):
    sys.stderr.write(message + "\n")
    sys.exit(1)


def keystream(key, n):
    out, counter = bytearray(), 0
    while len(out) < n:
        out += hashlib.sha256(key + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def emit(out, data):
    if os.environ.get("FAKE_AGE_TEE") and "--encrypt" in ARGS:
        with open(os.environ["FAKE_AGE_TEE"], "wb") as fh:
            fh.write(data)
    if out is None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        with open(out, "wb") as fh:
            fh.write(data)


def age():
    record()
    out, src = opt("--output", "-o", required=False), ARGS[-1]
    if "--encrypt" in ARGS:
        if os.environ.get("FAKE_AGE_FAIL"):
            emit(out, HEADER + b"partial output")
            fail("age: error: simulated encryption failure")
        with open(opt("--recipients-file", "-R")) as fh:
            recipients = [l.strip() for l in fh if l.strip() and not l.startswith("#")]
        with open(src, "rb") as fh:
            data = fh.read()
        file_key = os.urandom(32)
        stanzas = b"".join(b"-> fake " + xor(file_key, hashlib.sha256(r.encode()).digest()).hex().encode() + b"\n"
                           for r in recipients)
        body = xor(data, keystream(file_key, len(data)))
        mac = hmac.new(file_key, HEADER + stanzas + body, "sha256").hexdigest().encode()
        emit(out, HEADER + stanzas + b"--- " + mac + b"\n" + body)
    elif "--decrypt" in ARGS:
        with open(opt("--identity", "-i")) as fh:
            identities = [l.split()[1] for l in fh if l.startswith("FAKE-AGE-IDENTITY ")]
        with open(src, "rb") as fh:
            blob = fh.read()
        if not blob.startswith(HEADER):
            fail("age: error: not an age file")
        rest, stanzas = blob[len(HEADER):], []
        while rest.startswith(b"-> ") and b"\n" in rest:
            line, rest = rest.split(b"\n", 1)
            stanzas.append(line + b"\n")
        if not rest.startswith(b"--- ") or b"\n" not in rest:
            fail("age: error: malformed header")
        mac_line, body = rest.split(b"\n", 1)
        for identity in identities:
            for stanza in stanzas:
                try:
                    file_key = xor(bytes.fromhex(stanza.split()[2].decode()), hashlib.sha256(identity.encode()).digest())
                except (ValueError, IndexError):
                    continue
                expected = hmac.new(file_key, HEADER + b"".join(stanzas) + body, "sha256").hexdigest().encode()
                if hmac.compare_digest(expected, mac_line[4:]):
                    emit(out, xor(body, keystream(file_key, len(body))))
                    return
        emit(out, b"partial plaintext written before the failure was detected")
        fail("age: error: no identity matched any of the recipients, or the file is damaged")
    else:
        fail("age: unsupported invocation")


def pg_dump():
    if ARGS == ["--version"]:
        print("pg_dump (PostgreSQL) 18.6 (fictional stand-in)")
        return
    record()
    if os.environ.get("FAKE_PG_DUMP_PIDFILE"):
        with open(os.environ["FAKE_PG_DUMP_PIDFILE"], "w") as fh:
            fh.write(str(os.getpid()))
    if os.environ.get("FAKE_PG_DUMP_SLEEP"):
        time.sleep(float(os.environ["FAKE_PG_DUMP_SLEEP"]))
    if os.environ.get("FAKE_PG_DUMP_FAIL"):
        fail("pg_dump: error: simulated failure")
    db = [a.split("=", 1)[1] for a in ARGS if a.startswith("--dbname=")][0]
    sys.stdout.buffer.write(b"PGDMP\x01 fictional dump of " + db.encode() + b"\n")


def systemctl():
    record()
    command, t = ARGS[0], times()
    if command == "show":
        values = {"ActiveState": service(), "ActiveEnterTimestampMonotonic": t["active_enter"],
                  "InactiveEnterTimestampMonotonic": t["inactive_enter"]}
        for arg in ARGS:
            if arg.startswith("--property="):
                prop = arg.split("=", 1)[1]
                print(f"{prop}={values[prop]}")
    elif command == "stop":
        if os.environ.get("FAKE_SYSTEMCTL_STOP_FAIL"):
            fail("Failed to stop applyforge.service (simulated)")
        if service() == "active":
            t["inactive_enter"] = now_usec()
        write("service_state", "inactive")
        write("times", json.dumps(t))
    elif command == "start":
        once = os.environ.get("FAKE_SYSTEMCTL_START_FAIL_ONCE") and not os.path.exists(path("start_failed_once"))
        if os.environ.get("FAKE_SYSTEMCTL_START_FAIL") or once:
            if once:
                write("start_failed_once", "1")
            t["inactive_enter"] = now_usec()          # activating -> failed re-enters an inactive state
            write("service_state", "failed")
            write("times", json.dumps(t))
            fail("Job for applyforge.service failed (simulated)")
        if service() != "active":
            t["active_enter"] = now_usec()
        write("service_state", "active")
        write("times", json.dumps(t))
    else:
        fail("systemctl: unsupported")


def databases():
    return [l for l in read("databases").splitlines() if l]


def psql():
    record()
    command = [a.split("=", 1)[1] for a in ARGS if a.startswith("--command=")][0]
    user = os.environ.get("FAKE_RUN_AS", "caller")
    if "session_user" in command:
        if os.environ.get("FAKE_PSQL_FAIL_AS") == user:
            fail(f'psql: error: FATAL:  Peer authentication failed for user "{user}"')
        print(read("identity_row").strip() or f"{user}|{user}|f|f|f|f|f|0|f")
    elif "pg_shdepend" in command:
        print(read("production_privileges", "0").strip())
    elif "pg_get_userbyid" in command:
        name = re.search(r"datname = '([^']*)'", command).group(1)
        owners = json.loads(read("db_owners", "{}"))
        if name in owners:
            print(owners[name])
    else:
        print("\n".join(databases()))


def createdb():
    record()
    name = ARGS[-1]
    if name in databases():
        fail(f'createdb: error: database "{name}" already exists')
    write("databases", "\n".join(databases() + [name]) + "\n")
    owner = [a.split("=", 1)[1] for a in ARGS if a.startswith("--owner=")][0]
    owners = json.loads(read("db_owners", "{}"))
    owners[name] = os.environ.get("FAKE_CREATEDB_OWNER_OVERRIDE", owner)
    write("db_owners", json.dumps(owners))


def pg_restore():
    record()
    data = sys.stdin.buffer.read()
    if os.environ.get("FAKE_PG_RESTORE_FAIL"):
        fail("pg_restore: error: simulated failure")
    db = [a.split("=", 1)[1] for a in ARGS if a.startswith("--dbname=")][0]
    with open(path(f"restored-{db}.dump"), "wb") as fh:
        fh.write(data)


def runuser():
    record()
    if len(ARGS) < 4 or ARGS[0] != "-u" or ARGS[2] != "--":
        fail("runuser: unsupported invocation")
    env = dict(os.environ, FAKE_RUN_AS=ARGS[1])
    if os.environ.get("FAKE_RUNUSER_FORKS"):
        # Like the real runuser (PAM session): fork, run the command as a child, wait for it.
        # Killing only this process would leave the child running.
        child = os.fork()
        if child == 0:
            os.execvpe(ARGS[3], [ARGS[3]] + ARGS[4:], env)
        _, status = os.waitpid(child, 0)
        sys.exit(os.waitstatus_to_exitcode(status) & 0xFF)
    os.execvpe(ARGS[3], [ARGS[3]] + ARGS[4:], env)


{"age": age, "pg_dump": pg_dump, "systemctl": systemctl, "psql": psql, "createdb": createdb,
 "pg_restore": pg_restore, "runuser": runuser}[NAME]()
'''

MEDIA_FIXTURES = {
    "resumes/fictional_resume.pdf": b"%PDF-1.4\n% fictional test resume - Jane Example\n%%EOF\n",
    "resumes/nested/notes.txt": b"fictional notes\n",
    "empty.bin": b"",
    "résumé – unicode.txt": "fictional unicode name\n".encode(),
}
SECRET = b"SECRET_KEY=fictional-secret-that-must-never-be-archived"
NOT_ROOT = not (hasattr(os, "geteuid") and os.geteuid() == 0)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def sha256_path(path):
    with open(path, "rb") as fh:
        return sha256(fh.read())


def open_fd_count():
    return len(os.listdir("/proc/self/fd"))


def stat_mode(path):
    return os.lstat(path).st_mode & 0o7777


class BrokenStream:
    """A terminal that went away: every write fails."""

    def write(self, data):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    def flush(self):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")


@unittest.skipUnless(os.name == "posix", "the backup tool targets Linux; run these tests on Linux or WSL")
class BackupTestCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="afbackup-test-")
        self.addCleanup(self._cleanup)
        self.bin, self.state = self.mkdir("bin"), self.mkdir("state")
        fake = os.path.join(self.bin, "faketool.py")
        with open(fake, "w") as fh:
            fh.write(f"#!{sys.executable}\n" + FAKE_TOOL)
        os.chmod(fake, 0o755)
        for tool in TOOLS:
            os.symlink("faketool.py", os.path.join(self.bin, tool))
        safe_path = f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin"
        env = mock.patch.dict(os.environ, {"PATH": safe_path, "FAKE_STATE": self.state})
        env.start()
        self.addCleanup(env.stop)
        for key in [k for k in os.environ if (k.startswith("FAKE_") and k != "FAKE_STATE") or k == "INVOCATION_ID"]:
            del os.environ[key]
        for tool in TOOLS:  # safety: only the fictional stand-ins may ever run
            self.assertEqual(shutil.which(tool, path=safe_path), os.path.join(self.bin, tool), f"real {tool} reachable")

        self.dest = self.mkdir("offhost-backups")
        self.touch(os.path.join(self.dest, afbackup.MARKER))
        for name, value in (("SAFE_PATH", safe_path), ("TRUSTED_UID", os.getuid()),
                            ("ENV_PASSTHROUGH_PREFIXES", ("FAKE_",)), ("RESTART_RETRY_SECONDS", 0)):
            patcher = mock.patch.object(afbackup, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, kwargs in (("require_root", {"return_value": None}),
                             ("is_mount_point", {"side_effect": lambda p: p == self.dest}),
                             ("os_user_exists", {"side_effect": lambda n: n in ("applyforge", RESTORE_ROLE)}),
                             ("current_boot_id", {"return_value": "fictional-boot-id"})):
            patcher = mock.patch.object(afbackup, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.media = self.mkdir("var/lib/applyforge/media")
        for rel, data in MEDIA_FIXTURES.items():
            self.touch(os.path.join(self.media, *rel.split("/")), data)
        self.work = os.path.join(self.tmp, "var", "lib", "applyforge-backup-work")
        self.state_dir = os.path.join(self.tmp, "var", "lib", "applyforge-backup")
        self.lock_dir = self.mkdir("run/lock")
        self.lock_file = os.path.join(self.lock_dir, "applyforge-backup.lock")
        self.recipients = self.touch(os.path.join(self.tmp, "etc", "backup-recipients.txt"),
                                     f"# fictional public recipient\n{RECIPIENT}\n".encode())
        self.identity = self.touch(os.path.join(self.tmp, "offline", "identity.txt"),
                                   f"# fictional test identity - not a real age key\nFAKE-AGE-IDENTITY {RECIPIENT}\n".encode())
        self.wrong_identity = self.touch(os.path.join(self.tmp, "offline", "wrong-identity.txt"),
                                         f"FAKE-AGE-IDENTITY {OTHER_RECIPIENT}\n".encode())
        self.set_service("active")
        self.set_databases(["postgres", "applyforge"])
        self.config = self.write_config()

    # --- helpers ------------------------------------------------------------------------

    def _cleanup(self):
        for dirpath, dirnames, _ in os.walk(self.tmp):  # undo chmod done by permission tests
            for name in dirnames:
                with contextlib.suppress(OSError):
                    os.chmod(os.path.join(dirpath, name), 0o700)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mkdir(self, rel):
        path = os.path.join(self.tmp, *rel.split("/"))
        os.makedirs(path, exist_ok=True)
        return path

    def touch(self, path, data=b""):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def write_config(self, path=None, **overrides):
        values = {
            "BACKUP_DESTINATION": self.dest, "BACKUP_RECIPIENTS_FILE": self.recipients,
            "BACKUP_RETENTION_DAYS": "30", "BACKUP_WORK_DIR": self.work, "BACKUP_STATE_DIR": self.state_dir,
            "BACKUP_LOCK_FILE": self.lock_file, "DB_NAME": "applyforge", "DB_OWNER": "applyforge",
            "PG_RUN_AS": "postgres", "MEDIA_ROOT": self.media, "SERVICE_NAME": "applyforge.service",
            "RELEASE_LINK": os.path.join(self.tmp, "srv", "current"),
        }
        values.update(overrides)
        path = path or os.path.join(self.tmp, "backup.conf")
        with open(path, "w") as fh:
            fh.write("# fictional test configuration\n")
            fh.writelines(f"{k}={v}\n" for k, v in values.items() if v is not None)
        os.chmod(path, 0o644)
        return path

    def set_service(self, state):
        with open(os.path.join(self.state, "service_state"), "w") as fh:
            fh.write(state)

    def service(self):
        with open(os.path.join(self.state, "service_state")) as fh:
            return fh.read().strip()

    def systemctl(self, *args):
        subprocess.run([os.path.join(self.bin, "systemctl"), *args, "applyforge.service"], check=True,
                       capture_output=True)

    def set_state_file(self, name, text):
        with open(os.path.join(self.state, name), "w") as fh:
            fh.write(text)

    def set_databases(self, names):
        self.set_state_file("databases", "\n".join(names) + "\n")

    def databases(self):
        with open(os.path.join(self.state, "databases")) as fh:
            return [line for line in fh.read().splitlines() if line]

    def calls(self, tool=None):
        try:
            with open(os.path.join(self.state, "calls.log")) as fh:
                entries = [json.loads(line) for line in fh]
        except FileNotFoundError:
            return []
        return [e for e in entries if tool is None or e["tool"] == tool]

    def clear_calls(self):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(os.path.join(self.state, "calls.log"))

    def order(self):
        return [(c["tool"], c["args"][0]) for c in self.calls() if c["tool"] != "runuser"]

    def run_tool(self, *argv, stderr=None):
        err, out = stderr or io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = afbackup.main(list(argv))
        self.stderr = err.getvalue() if isinstance(err, io.StringIO) else ""
        self.stdout = out.getvalue()
        return code

    def create(self):
        return self.run_tool("create", "--config", self.config)

    def recover(self, *extra):
        return self.run_tool("recover-service", "--config", self.config, *extra)

    def archives(self):
        return sorted(n for n in os.listdir(self.dest) if afbackup.ARCHIVE_RE.match(n))

    def make_backup(self):
        self.assertEqual(self.create(), 0, self.stderr)
        archive = os.path.join(self.dest, self.archives()[-1])
        self.clear_calls()
        return archive

    def plaintext(self, archive, identity=None):
        out = os.path.join(self.tmp, "plain.tar")
        subprocess.run([os.path.join(self.bin, "age"), "--decrypt", "--identity", identity or self.identity,
                        "--output", out, archive], check=True, capture_output=True)
        with open(out, "rb") as fh:
            data = fh.read()
        os.unlink(out)
        return data

    def verify(self, archive, identity=None, expected=None, *extra):
        argv = ["verify", "--archive", archive, "--identity", identity or self.identity,
                "--work-dir", self.work, "--lock-file", self.lock_file, *extra]
        if expected:
            argv += ["--expected-sha256", expected]
        return self.run_tool(*argv)

    AUTO = object()

    def restore(self, archive, db="applyforge_restore_test", media=None, confirm_db=None,
                confirm_media=None, identity=None, expected=AUTO, extra=()):
        media = media or os.path.join(self.tmp, "restored-media")
        argv = ["restore", "--config", self.config, "--archive", archive,
                "--identity", identity or self.identity, "--new-database", db,
                "--new-media-dir", media, "--confirm-database", confirm_db or db,
                "--confirm-media-dir", confirm_media or media, *extra]
        if expected is self.AUTO:
            expected = sha256_path(archive) if os.path.isfile(archive) else "0" * 64
        if expected is not None:
            argv += ["--expected-sha256", expected]
        return self.run_tool(*argv)

    def make_archive(self, members, name="custom.tar.age"):
        """Encrypt a hand-built tar. members: (name, bytes) or (TarInfo, bytes|None)."""
        tar_path = os.path.join(self.tmp, "custom.tar")
        with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
            for member, data in members:
                info = member if isinstance(member, tarfile.TarInfo) else tarfile.TarInfo(member)
                if data is not None:
                    info.size = len(data)
                tar.addfile(info, io.BytesIO(data) if data is not None else None)
        out = os.path.join(self.tmp, name)
        subprocess.run([os.path.join(self.bin, "age"), "--encrypt", "--recipients-file", self.recipients,
                        "--output", out, tar_path], check=True, capture_output=True)
        os.unlink(tar_path)
        return out

    @staticmethod
    def manifest_for(files, **overrides):
        manifest = {"format": afbackup.FORMAT, "format_version": afbackup.FORMAT_VERSION,
                    "created_utc": "2026-11-18T03:30:00Z",
                    "database": {"name": "applyforge", "dump_file": afbackup.DUMP},
                    "files": [{"path": p, "size": len(d), "sha256": sha256(d)} for p, d in files]}
        manifest.update(overrides)
        return json.dumps(manifest).encode()

    def assert_no_leftovers(self):
        if os.path.isdir(self.work):
            self.assertEqual(os.listdir(self.work), [], "temporary files left in the work directory")
        self.assertEqual([n for n in os.listdir(self.dest) if n.endswith(".partial")], [], "partial file left")
        staging = [os.path.join(d, n) for d, dirs, _ in os.walk(self.tmp) for n in dirs if ".restoring-" in n]
        self.assertEqual(staging, [], "restore staging directory left")

    def write_old_archive(self, days_ago, now=None, content=None):
        stamp = ((now or afbackup.utcnow()) - dt.timedelta(days=days_ago)).strftime(afbackup.TIMESTAMP_FORMAT)
        return self.touch(os.path.join(self.dest, f"applyforge-backup-{stamp}.tar.age"),
                          content if content is not None else afbackup.AGE_HEADER + b"fictional old archive")

    @contextlib.contextmanager
    def hold_lock(self, path=None):
        fd = os.open(path or self.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            os.close(fd)

    def index_path(self):
        return os.path.join(self.state_dir, afbackup.INDEX_NAME)

    def index_lines(self):
        with open(self.index_path()) as fh:
            return [line.rstrip("\n").split("\t") for line in fh]

    def marker_path(self):
        return os.path.join(self.state_dir, afbackup.SERVICE_MARKER)

    def marker(self):
        with open(self.marker_path()) as fh:
            return json.load(fh)


# --- Round trip ----------------------------------------------------------------------------

class RoundTripTests(BackupTestCase):

    def test_create_verify_restore_round_trip(self):
        self.assertEqual(self.create(), 0, self.stderr)
        archives = self.archives()
        self.assertEqual(len(archives), 1)
        archive = os.path.join(self.dest, archives[0])
        with open(archive, "rb") as fh:
            data = fh.read()
        self.assertTrue(data.startswith(afbackup.AGE_HEADER))
        self.assertNotIn(b"fictional test resume", data)
        self.assertEqual(os.stat(archive).st_mode & 0o777, 0o600)
        self.assertEqual(self.service(), "active")
        self.assert_no_leftovers()

        digest = sha256_path(archive)
        self.assertEqual(self.verify(archive, expected=digest), 0, self.stderr)
        self.assertIn("OK: archive verified", self.stdout)
        self.assert_no_leftovers()

        target = os.path.join(self.tmp, "restored-media")
        self.assertEqual(self.restore(archive, media=target), 0, self.stderr)
        for rel, data in MEDIA_FIXTURES.items():
            with open(os.path.join(target, *rel.split("/")), "rb") as fh:
                self.assertEqual(fh.read(), data, rel)
        with open(os.path.join(self.state, "restored-applyforge_restore_test.dump"), "rb") as fh:
            self.assertEqual(fh.read(), b"PGDMP\x01 fictional dump of applyforge\n")
        createdb = self.calls("createdb")[0]
        self.assertEqual(createdb["as"], "postgres")
        self.assertIn(f"--owner={RESTORE_ROLE}", createdb["args"])
        self.assertIn("--template=template0", createdb["args"])
        restore_call = self.calls("pg_restore")[0]
        self.assertEqual(restore_call["as"], RESTORE_ROLE)
        for flag in ("--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges",
                     "--dbname=applyforge_restore_test"):
            self.assertIn(flag, restore_call["args"])
        self.assertIn("NOT changed", self.stdout)
        self.assertIn("applyforge", self.databases())
        self.assert_no_leftovers()

    def assert_restored_modes(self, target):
        modes = {}
        for dirpath, dirnames, filenames in os.walk(target):
            modes[os.path.relpath(dirpath, target)] = stat_mode(dirpath)
            for name in filenames:
                modes[os.path.relpath(os.path.join(dirpath, name), target)] = stat_mode(os.path.join(dirpath, name))
        self.assertEqual(modes["."], 0o750, "top-level directory")
        for rel in ("resumes", os.path.join("resumes", "nested")):
            self.assertEqual(modes[rel], 0o750, f"nested directory {rel}")
        for rel in MEDIA_FIXTURES:
            self.assertEqual(modes[os.path.join(*rel.split("/"))], 0o640, f"file {rel}")
        self.assertEqual(sorted(set(modes.values())), [0o640, 0o750])

    def test_restored_media_directories_0750_and_files_0640_despite_umask(self):
        archive = self.make_backup()
        target = os.path.join(self.tmp, "restored-media")
        self.assertEqual(self.restore(archive, media=target), 0, self.stderr)
        self.assert_restored_modes(target)

    def test_restored_media_modes_when_target_already_exists_empty(self):
        archive = self.make_backup()
        target = os.path.join(self.tmp, "restored-media")
        os.mkdir(target, 0o700)
        self.assertEqual(self.restore(archive, media=target), 0, self.stderr)
        self.assert_restored_modes(target)

    def test_manifest_records_contents_checksums_and_recipients(self):
        release = os.path.join(self.tmp, "srv", "releases", "0123abcd")
        os.makedirs(release)
        os.symlink(release, os.path.join(self.tmp, "srv", "current"))
        archive = self.make_backup()
        with tarfile.open(fileobj=io.BytesIO(self.plaintext(archive))) as tar:
            names = tar.getnames()
            manifest = json.loads(tar.extractfile(afbackup.MANIFEST).read())
            dump = tar.extractfile(afbackup.DUMP).read()
        self.assertEqual(names[0], afbackup.MANIFEST)
        self.assertEqual(manifest["application_release"], "0123abcd")
        self.assertEqual(manifest["recipients_fingerprint"], afbackup.recipients_fingerprint([RECIPIENT]))
        entries = {e["path"]: e for e in manifest["files"]}
        self.assertEqual(entries[afbackup.DUMP]["sha256"], sha256(dump))
        for rel, data in MEDIA_FIXTURES.items():
            self.assertEqual(entries["media/" + rel], {"path": "media/" + rel, "size": len(data), "sha256": sha256(data)})

    def test_archive_contains_no_secrets_or_links(self):
        secret_env = self.touch(os.path.join(self.tmp, "etc", "applyforge.env"), SECRET)
        profile = self.mkdir("browser_profile")
        self.touch(os.path.join(profile, "Cookies"), b"fictional-linkedin-cookie")
        os.symlink(secret_env, os.path.join(self.media, "link.env"))
        os.symlink(profile, os.path.join(self.media, "profile-link"))
        archive = self.make_backup()
        plain = self.plaintext(archive)
        self.assertNotIn(b"fictional-secret-that-must-never-be-archived", plain)
        self.assertNotIn(b"fictional-linkedin-cookie", plain)
        with tarfile.open(fileobj=io.BytesIO(plain)) as tar:
            names = set(tar.getnames())
            manifest = json.loads(tar.extractfile(afbackup.MANIFEST).read())
        self.assertEqual(names, {afbackup.MANIFEST, afbackup.DUMP} | {"media/" + r for r in MEDIA_FIXTURES})
        self.assertEqual(manifest["media"]["skipped_not_regular_files"], ["link.env", "profile-link"])


# --- R1/R13: trusted hash from age's output, recorded before publication ------------------------------

class TrustedHashTests(BackupTestCase):

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(afbackup, "utcnow", return_value=FIXED_NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.name = f"applyforge-backup-{FIXED_NOW.strftime(afbackup.TIMESTAMP_FORMAT)}.tar.age"
        self.final = os.path.join(self.dest, self.name)

    def test_index_and_journal_hash_equals_what_age_emitted(self):
        tee = os.path.join(self.tmp, "age-output.bin")
        os.environ["FAKE_AGE_TEE"] = tee
        self.assertEqual(self.create(), 0, self.stderr)
        emitted = sha256_path(tee)
        self.assertEqual(self.index_lines()[0][2], emitted)
        self.assertEqual(self.index_lines()[0][6], "recorded")
        self.assertIn(f"sha256={emitted}", self.stderr)
        self.assertEqual(sha256_path(self.final), emitted)

    def test_share_returning_different_bytes_on_reread_is_rejected(self):
        # A malicious share serves other bytes when the tool reads the file back.
        with mock.patch.object(afbackup, "hash_published_candidate", return_value=("f" * 64, 1234)):
            self.assertEqual(self.create(), 1)
        self.assertIn("differs from what age produced", self.stderr)
        self.assertFalse(os.path.lexists(self.final))
        self.assertFalse(os.path.exists(self.index_path()) and self.index_lines(), "nothing may be recorded")
        self.assertEqual(os.listdir(self.dest), [afbackup.MARKER])

    def test_hash_is_recorded_durably_before_publication(self):
        real_publish = afbackup.publish
        seen = {}

        def publish(partial, final, identity):
            seen["index_at_publish"] = self.index_lines()
            seen["final_existed"] = os.path.lexists(final)
            return real_publish(partial, final, identity)
        with mock.patch.object(afbackup, "publish", side_effect=publish):
            self.assertEqual(self.create(), 0, self.stderr)
        self.assertFalse(seen["final_existed"])
        self.assertEqual([l[1:3] + [l[6]] for l in seen["index_at_publish"]],
                         [[self.name, sha256_path(self.final), "recorded"]])

    def test_failed_publication_marks_record_failed_and_restore_refuses_it(self):
        real_publish = afbackup.publish

        def appear_then_publish(partial, final, identity):
            self.touch(final, b"archive that appeared meanwhile")
            return real_publish(partial, final, identity)
        with mock.patch.object(afbackup, "publish", side_effect=appear_then_publish):
            self.assertEqual(self.create(), 1)
        self.assertEqual([l[6] for l in self.index_lines()], ["recorded", "failed"])
        self.assertEqual(afbackup.read_index(afbackup.load_config(self.config, ())), {})


# --- H1/R2/R3/R12: restore trust, isolated restore role, effective identity -------------------------

class TrustedRestoreTests(BackupTestCase):

    def setUp(self):
        super().setUp()
        self.archive = self.make_backup()
        self.target = os.path.join(self.tmp, "restored-media")

    def assert_no_restore_side_effects(self, allow_age=False):
        tools = (["age"] if not allow_age else []) + ["createdb", "pg_restore"]
        self.assertEqual([c for t in tools for c in self.calls(t)], [])
        self.assertFalse(os.path.lexists(self.target))
        self.assertEqual(self.databases(), ["postgres", "applyforge"])
        self.assert_no_leftovers()

    def test_missing_expected_hash_refused_before_anything(self):
        self.assertEqual(self.restore(self.archive, media=self.target, expected=None), 2)
        self.assertIn("--expected-sha256 is required", self.stderr)
        self.assert_no_restore_side_effects()
        self.assertEqual(self.calls("psql"), [])

    def test_wrong_or_malformed_expected_hash_refused_before_decrypting(self):
        for value, phrase in (("0" * 64, "does not match --expected-sha256"), ("not-a-hash", "64 hexadecimal")):
            with self.subTest(value=value):
                self.assertEqual(self.restore(self.archive, media=self.target, expected=value), 2)
                self.assertIn(phrase, self.stderr)
                self.assert_no_restore_side_effects()
                self.assertEqual(self.calls("psql"), [])

    def test_correct_hash_proceeds_and_is_case_insensitive(self):
        self.assertEqual(self.restore(self.archive, media=self.target, expected=sha256_path(self.archive).upper()), 0,
                         self.stderr)
        self.assertTrue(os.path.isdir(self.target))

    def test_pg_restore_runs_as_isolated_restore_role_only(self):
        self.assertEqual(self.restore(self.archive, media=self.target), 0, self.stderr)
        calls = self.calls("pg_restore")
        self.assertEqual([c["as"] for c in calls], [RESTORE_ROLE])
        self.assertFalse(any(a.startswith(("--role", "--username", "-U")) for a in calls[0]["args"]))
        self.assertNotIn(calls[0]["as"], ("applyforge", "postgres"))
        identity_checks = [c for c in self.calls("psql") if "session_user" in c["args"][-1]]
        self.assertEqual([c["as"] for c in identity_checks], [RESTORE_ROLE])
        self.assertTrue(all(c["as"] == "postgres" for c in self.calls("createdb")))

    def test_restore_role_cannot_be_production_owner_or_privileged(self):
        cases = ({"RESTORE_ROLE": "applyforge"}, {"RESTORE_ROLE": "postgres"},
                 {"RESTORE_OS_USER": "root"}, {"RESTORE_OS_USER": "postgres"}, {"RESTORE_OS_USER": "applyforge"})
        for overrides in cases:
            with self.subTest(overrides=overrides):
                self.config = self.write_config(**overrides)
                self.assertEqual(self.restore(self.archive, media=self.target), 2)
                self.assertIn("RESTORE_", self.stderr)
                self.assert_no_restore_side_effects()

    def test_effective_identity_problems_refused_before_createdb(self):
        user = RESTORE_ROLE
        cases = {
            "superuser": f"{user}|{user}|t|f|f|f|f|0|f",
            "CREATEROLE": f"{user}|{user}|f|t|f|f|f|0|f",
            "CREATEDB": f"{user}|{user}|f|f|t|f|f|0|f",
            "BYPASSRLS": f"{user}|{user}|f|f|f|t|f|0|f",
            "REPLICATION": f"{user}|{user}|f|f|f|f|t|0|f",
            "member of other roles": f"{user}|{user}|f|f|f|f|f|1|f",
            "server-program/server-file": f"{user}|{user}|f|f|f|f|f|0|t",
            "unexpected peer/pg_ident mapping": f"applyforge|applyforge|f|f|f|f|f|0|f",
            "could not verify": "garbage",
        }
        for phrase, row in cases.items():
            with self.subTest(case=phrase):
                self.clear_calls()
                self.set_state_file("identity_row", row)
                self.assertEqual(self.restore(self.archive, media=self.target), 2)
                self.assertIn(phrase, self.stderr)
                self.assert_no_restore_side_effects(allow_age=True)

    def test_identity_check_failure_fails_closed(self):
        os.environ["FAKE_PSQL_FAIL_AS"] = RESTORE_ROLE
        self.assertEqual(self.restore(self.archive, media=self.target), 2)
        self.assertIn("cannot connect as the restore account", self.stderr)
        self.assert_no_restore_side_effects(allow_age=True)

    def test_restore_role_with_production_ownership_or_privileges_refused(self):
        self.set_state_file("production_privileges", "3")
        self.assertEqual(self.restore(self.archive, media=self.target), 2)
        self.assertIn("isolated from production", self.stderr)
        self.assert_no_restore_side_effects(allow_age=True)

    def test_database_with_unexpected_owner_is_not_restored(self):
        os.environ["FAKE_CREATEDB_OWNER_OVERRIDE"] = "applyforge"
        self.assertEqual(self.restore(self.archive, media=self.target), 1)
        self.assertIn("is owned by 'applyforge'", self.stderr)
        self.assertIn("sudo -u postgres dropdb applyforge_restore_test", self.stderr)
        self.assertEqual(self.calls("pg_restore"), [])
        self.assertFalse(os.path.lexists(self.target))
        self.assert_no_leftovers()

    def test_unindexed_archive_requires_explicit_exception(self):
        files = [(afbackup.DUMP, b"PGDMP fictional"), ("media/a.txt", b"fictional")]
        planted = self.make_archive([(afbackup.MANIFEST, self.manifest_for(files))] + files)
        self.clear_calls()
        self.assertEqual(self.restore(planted, media=self.target), 2)
        self.assertIn("is not recorded in the local backup index", self.stderr)
        self.assert_no_restore_side_effects()
        self.assertEqual(self.restore(planted, media=self.target, extra=["--allow-unindexed"]), 0, self.stderr)
        self.assertIn("not in the local index", self.stderr)

    def test_malicious_dump_reaches_only_the_isolated_non_superuser_role(self):
        evil = b"PGDMP fictional\nRESET ROLE; ALTER DATABASE applyforge CONNECTION LIMIT 0; ALTER ROLE applyforge PASSWORD 'x';\n"
        files = [(afbackup.DUMP, evil), ("media/a.txt", b"fictional")]
        planted = self.make_archive([(afbackup.MANIFEST, self.manifest_for(files))] + files)
        self.assertEqual(self.restore(planted, media=self.target, extra=["--allow-unindexed"]), 0, self.stderr)
        call = self.calls("pg_restore")[0]
        self.assertEqual(call["as"], RESTORE_ROLE)   # not the production owner, not postgres
        self.assertIn("--single-transaction", call["args"])

    def test_index_name_mismatch_refused(self):
        lines = self.index_lines()
        lines[-1][2] = "f" * 64
        with open(self.index_path(), "w") as fh:
            fh.writelines("\t".join(l) + "\n" for l in lines)
        self.assertEqual(self.restore(self.archive, media=self.target), 2)
        self.assertIn("index records a different SHA-256", self.stderr)
        self.assertEqual(self.calls("createdb"), [])

    def test_malformed_index_refused(self):
        with open(self.index_path(), "a") as fh:
            fh.write("garbage line\n")
        self.assertEqual(self.restore(self.archive, media=self.target), 2)
        self.assertIn("malformed at line 2", self.stderr)
        self.assert_no_restore_side_effects()

    def test_new_server_without_index_relies_on_expected_hash_only(self):
        os.unlink(self.index_path())
        self.assertEqual(self.restore(self.archive, media=self.target), 0, self.stderr)

    def test_symlinked_archive_refused(self):
        link = os.path.join(self.tmp, "link.tar.age")
        os.symlink(self.archive, link)
        self.assertEqual(self.restore(link, media=self.target, expected=sha256_path(self.archive)), 2)
        self.assertIn("symbolic link", self.stderr)
        self.assert_no_restore_side_effects()


class IndexTests(BackupTestCase):

    def test_index_records_hash_and_is_root_only(self):
        archive = self.make_backup()
        lines = self.index_lines()
        self.assertEqual(len(lines), 1)
        created, name, digest, size, fingerprint, _host, status = lines[0]
        self.assertEqual((name, digest, int(size), status),
                         (os.path.basename(archive), sha256_path(archive), os.path.getsize(archive), "recorded"))
        self.assertEqual(fingerprint, afbackup.recipients_fingerprint([RECIPIENT]))
        self.assertEqual(os.stat(self.state_dir).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.index_path()).st_mode & 0o777, 0o600)
        self.assertFalse(any(afbackup.INDEX_NAME in n for n in os.listdir(self.dest)))

    def test_unsafe_index_or_state_dir_refused_before_service_stop(self):
        self.make_backup()
        for path, mode in ((self.index_path(), 0o640), (self.index_path(), 0o666), (self.state_dir, 0o750)):
            with self.subTest(path=path, mode=oct(mode)):
                os.chmod(path, mode)
                self.clear_calls()
                self.assertEqual(self.create(), 2)
                self.assertIn("root", self.stderr)
                self.assertNotIn(("systemctl", "stop"), self.order())
                os.chmod(path, 0o600 if path == self.index_path() else 0o700)

    def test_malformed_index_refused_before_service_stop(self):
        self.make_backup()
        with open(self.index_path(), "a") as fh:
            fh.write("2026-11-18T03:30:00Z\tnot-an-archive\tabc\t1\tx\thost\trecorded\n")
        self.assertEqual(self.create(), 2)
        self.assertIn("malformed", self.stderr)
        self.assertNotIn(("systemctl", "stop"), self.order())

    def test_symlinked_index_refused(self):
        os.makedirs(self.state_dir, mode=0o700)
        elsewhere = self.touch(os.path.join(self.tmp, "elsewhere.tsv"))
        os.symlink(elsewhere, self.index_path())
        self.assertEqual(self.create(), 2)
        self.assertIn("symbolic link", self.stderr)
        self.assertEqual(self.calls(), [])


# --- H2/R9: publication ---------------------------------------------------------------------------------

class PublicationTests(BackupTestCase):

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(afbackup, "utcnow", return_value=FIXED_NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.name = f"applyforge-backup-{FIXED_NOW.strftime(afbackup.TIMESTAMP_FORMAT)}.tar.age"
        self.final = os.path.join(self.dest, self.name)
        self.partial = os.path.join(self.dest, f".{self.name}.partial")

    def test_planted_partial_symlink_is_never_followed_or_deleted(self):
        victim = self.touch(os.path.join(self.tmp, "victim.txt"), b"precious fictional data")
        os.symlink(victim, self.partial)
        self.assertEqual(self.create(), 1)
        self.assertIn("planted", self.stderr)
        with open(victim, "rb") as fh:
            self.assertEqual(fh.read(), b"precious fictional data")
        self.assertTrue(os.path.islink(self.partial))
        self.assertFalse(os.path.lexists(self.final))
        self.assertEqual(self.service(), "active")

    def test_existing_final_archive_is_never_overwritten(self):
        self.touch(self.final, b"existing fictional archive")
        self.assertEqual(self.create(), 1)
        self.assertIn("not overwriting", self.stderr)
        with open(self.final, "rb") as fh:
            self.assertEqual(fh.read(), b"existing fictional archive")
        self.assertFalse(os.path.lexists(self.partial))

    def test_encryption_failure_removes_own_partial_and_closes_descriptors(self):
        os.environ["FAKE_AGE_FAIL"] = "1"
        before = open_fd_count()
        self.assertEqual(self.create(), 1)
        self.assertIn("age encryption failed", self.stderr)
        self.assertEqual(open_fd_count(), before, "file descriptor leaked")
        self.assertEqual(os.listdir(self.dest), [afbackup.MARKER])
        self.assert_no_leftovers()

    def test_atomic_publication_via_streamed_stdout(self):
        before = open_fd_count()
        self.assertEqual(self.create(), 0, self.stderr)
        self.assertEqual(open_fd_count(), before, "file descriptor leaked")
        encrypt = [c for c in self.calls("age") if "--encrypt" in c["args"]][0]
        self.assertNotIn("--output", encrypt["args"])
        self.assertTrue(os.path.isfile(self.final))
        self.assertFalse(os.path.lexists(self.partial))
        self.assertEqual(os.stat(self.final).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.final).st_nlink, 1)

    def test_partial_swapped_during_encryption_is_not_published_or_deleted(self):
        victim = self.touch(os.path.join(self.tmp, "victim.txt"), b"precious fictional data")
        real_hash = afbackup.hash_published_candidate

        def swap_then_hash(path, identity):
            os.unlink(path)
            os.symlink(victim, path)
            return real_hash(path, identity)
        with mock.patch.object(afbackup, "hash_published_candidate", side_effect=swap_then_hash):
            self.assertEqual(self.create(), 1)
        self.assertTrue(os.path.islink(self.partial), "a planted replacement must never be deleted")
        with open(victim, "rb") as fh:
            self.assertEqual(fh.read(), b"precious fictional data")
        self.assertFalse(os.path.lexists(self.final))

    def test_final_archive_appearing_during_encryption_is_not_overwritten(self):
        real_hash = afbackup.hash_published_candidate

        def appear_then_hash(path, identity):
            result = real_hash(path, identity)
            self.touch(self.final, b"archive that appeared meanwhile")
            return result
        with mock.patch.object(afbackup, "hash_published_candidate", side_effect=appear_then_hash):
            self.assertEqual(self.create(), 1)
        self.assertIn("not overwriting", self.stderr)
        with open(self.final, "rb") as fh:
            self.assertEqual(fh.read(), b"archive that appeared meanwhile")
        self.assertFalse(os.path.lexists(self.partial))

    def test_destination_rechecked_before_publication(self):
        calls = {"n": 0}

        def mounted(path):
            calls["n"] += 1
            return calls["n"] == 1
        with mock.patch.object(afbackup, "is_mount_point", side_effect=mounted):
            self.assertEqual(self.create(), 1)
        self.assertIn("destination check before publication failed", self.stderr)
        self.assertEqual(os.listdir(self.dest), [afbackup.MARKER])
        self.assertEqual(self.service(), "active")

    def test_marker_removed_before_publication_is_detected(self):
        real_hash = afbackup.hash_published_candidate

        def remove_marker_then_hash(path, identity):
            os.unlink(os.path.join(self.dest, afbackup.MARKER))
            return real_hash(path, identity)
        with mock.patch.object(afbackup, "hash_published_candidate", side_effect=remove_marker_then_hash):
            self.assertEqual(self.create(), 1)
        self.assertIn("marker file", self.stderr)
        self.assertEqual(os.listdir(self.dest), [])

    def test_without_hard_links_renameat2_noreplace_is_used(self):
        with mock.patch.object(afbackup.os, "link", side_effect=OSError(errno.EPERM, "Operation not permitted")):
            self.assertEqual(self.create(), 0, self.stderr)
        self.assertTrue(os.path.isfile(self.final))
        self.assertFalse(os.path.lexists(self.partial))

    def test_renameat2_never_overwrites(self):
        real_hash = afbackup.hash_published_candidate

        def appear_then_hash(path, identity):
            result = real_hash(path, identity)
            self.touch(self.final, b"archive that appeared meanwhile")
            return result
        with mock.patch.object(afbackup.os, "link", side_effect=OSError(errno.EPERM, "Operation not permitted")), \
                mock.patch.object(afbackup, "hash_published_candidate", side_effect=appear_then_hash):
            self.assertEqual(self.create(), 1)
        self.assertIn("not overwriting", self.stderr)
        with open(self.final, "rb") as fh:
            self.assertEqual(fh.read(), b"archive that appeared meanwhile")

    def test_no_safe_publication_method_fails_closed(self):
        with mock.patch.object(afbackup.os, "link", side_effect=OSError(errno.EPERM, "Operation not permitted")), \
                mock.patch.object(afbackup, "rename_noreplace", side_effect=OSError(errno.EINVAL, "Invalid argument")):
            self.assertEqual(self.create(), 1)
        self.assertIn("supports neither hard links nor RENAME_NOREPLACE", self.stderr)
        self.assertEqual(os.listdir(self.dest), [afbackup.MARKER])
        self.assertEqual([l[6] for l in self.index_lines()], ["recorded", "failed"])


# --- M1/R8: shared lock and per-run locks ----------------------------------------------------------------------

class LockTests(BackupTestCase):

    def test_create_restore_verify_and_recover_refused_while_locked(self):
        archive = self.make_backup()
        digest = sha256_path(archive)
        with self.hold_lock():
            for label, runner in (("create", self.create),
                                  ("restore", lambda: self.restore(archive)),
                                  ("verify", lambda: self.verify(archive, expected=digest)),
                                  ("verify --config", lambda: self.run_tool(
                                      "verify", "--config", self.config, "--archive", archive,
                                      "--identity", self.identity, "--expected-sha256", digest))):
                with self.subTest(command=label):
                    self.clear_calls()
                    self.assertEqual(runner(), 2)
                    self.assertIn("another backup, verify or restore is running", self.stderr)
                    self.assertEqual(self.calls(), [])
            self.touch(self.marker_path(), b"{}")
            os.chmod(self.marker_path(), 0o600)
            self.assertEqual(self.recover(), 2)
            self.assertIn("another backup", self.stderr)
            os.unlink(self.marker_path())
        os.unlink(archive)
        self.assertEqual(self.create(), 0, self.stderr)

    def test_verify_with_config_uses_the_configured_lock(self):
        archive = self.make_backup()
        other_lock = os.path.join(self.mkdir("other-run-lock"), "x.lock")
        self.config = self.write_config(BACKUP_LOCK_FILE=other_lock)
        with self.hold_lock(other_lock):
            code = self.run_tool("verify", "--config", self.config, "--archive", archive, "--identity",
                                 self.identity, "--expected-sha256", sha256_path(archive))
        self.assertEqual(code, 2)
        self.assertIn(other_lock, self.stderr)

    def test_missing_lock_directory_refused_for_create(self):
        os.rmdir(self.lock_dir)
        self.assertEqual(self.create(), 2)
        self.assertIn("lock directory", self.stderr)
        self.assertEqual(self.calls(), [])

    def test_read_only_lock_fallback_still_excludes(self):
        self.touch(self.lock_file)
        real_open = afbackup.os.open

        def fake_open(path, flags, *args, **kwargs):
            if path == self.lock_file and flags & os.O_CREAT:
                raise OSError(errno.EROFS, "Read-only file system")
            return real_open(path, flags, *args, **kwargs)
        with mock.patch.object(afbackup.os, "open", side_effect=fake_open):
            self.assertEqual(self.create(), 0, self.stderr)
        with self.hold_lock():          # held by another process/run, then the read-only path must still exclude
            with mock.patch.object(afbackup.os, "open", side_effect=fake_open):
                self.assertEqual(self.create(), 2)
            self.assertIn("another backup", self.stderr)

    def test_active_run_directory_is_never_removed_even_when_old(self):
        os.makedirs(self.work, mode=0o700)
        active = os.path.join(self.work, "create-actv1234")
        os.mkdir(active)
        self.touch(os.path.join(active, "backup.tar"), b"in use")
        lock_fd = os.open(os.path.join(active, afbackup.ACTIVE_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        old = time.time() - 7200
        os.utime(active, (old, old))
        try:
            self.assertEqual(self.create(), 0, self.stderr)
            self.assertTrue(os.path.isdir(active))
            self.assertIn("in use by another run", self.stderr)
        finally:
            os.close(lock_fd)
        shutil.rmtree(active)


# --- M2: stale plaintext cleanup ---------------------------------------------------------------------------------

class StaleCleanupTests(BackupTestCase):

    def age_dir(self, path, seconds):
        old = time.time() - seconds
        os.utime(path, (old, old))

    def test_stale_work_dirs_removed_recent_and_unrelated_kept(self):
        os.makedirs(self.work, mode=0o700)
        stale = [os.path.join(self.work, n) for n in ("create-abcdefgh", "restore-12345678", "verify-zz_zz_zz")]
        for path in stale:
            self.touch(os.path.join(path, "backup.tar"), b"fictional plaintext")
            self.touch(os.path.join(path, afbackup.ACTIVE_LOCK))   # lock file left by a crash, not held
            self.age_dir(path, 3600)
        recent = os.path.join(self.work, "create-recent01")
        self.touch(os.path.join(recent, "backup.tar"), b"x")
        keep = [recent]
        for name in ("notes", "create-short", "create-UPPERCAS", "other-abcdefgh"):
            path = os.path.join(self.work, name)
            os.mkdir(path)
            self.age_dir(path, 3600)
            keep.append(path)
        link_target = self.mkdir("not-in-work")
        link = os.path.join(self.work, "create-linklink")
        os.symlink(link_target, link)
        keep.append(link)

        self.assertEqual(self.create(), 0, self.stderr)
        for path in stale:
            self.assertFalse(os.path.lexists(path), path)
        for path in keep:
            self.assertTrue(os.path.lexists(path), path)
        self.assertIn("recent work directory", self.stderr)

    def test_stale_restore_staging_removed_only_for_the_same_target(self):
        archive = self.make_backup()
        target = os.path.join(self.tmp, "restored-media")
        stale = os.path.join(self.tmp, ".restored-media.restoring-abcdefgh")
        other = os.path.join(self.tmp, ".other-media.restoring-abcdefgh")
        for path in (stale, other):
            self.touch(os.path.join(path, "resume.pdf"), b"fictional plaintext resume")
            self.age_dir(path, 3600)
        self.assertEqual(self.restore(archive, media=target), 0, self.stderr)
        self.assertFalse(os.path.lexists(stale))
        self.assertTrue(os.path.isdir(other))
        shutil.rmtree(other)

    def test_tmpfiles_rule_covers_work_state_and_lock(self):
        rule = os.path.join(os.path.dirname(os.path.dirname(HERE)), "systemd", "applyforge-backup.tmpfiles")
        with open(rule) as fh:
            entries = {f[1]: f for f in (l.split() for l in fh if l.strip() and not l.startswith("#"))}
        self.assertEqual(entries["/var/lib/applyforge-backup-work"][:5],
                         ["d", "/var/lib/applyforge-backup-work", "0700", "root", "root"])
        self.assertEqual(entries["/var/lib/applyforge-backup"][2], "0700")
        self.assertEqual(entries["/run/lock/applyforge-backup.lock"][0], "f")


# --- M3/R6: service marker and recovery ---------------------------------------------------------------------

class ServiceMarkerTests(BackupTestCase):

    def kill_during_capture(self):
        """Simulate SIGKILL inside the maintenance window: the restart never happens."""
        with mock.patch.object(afbackup, "dump_database", side_effect=KeyboardInterrupt), \
                mock.patch.object(afbackup, "start_service_with_retry", side_effect=SystemExit("killed")):
            with self.assertRaises(SystemExit):
                self.create()

    def test_marker_lifecycle_on_success(self):
        real_write = afbackup.write_service_marker
        phases = []

        def write(cfg, **fields):
            phases.append(fields["phase"])
            return real_write(cfg, **fields)
        with mock.patch.object(afbackup, "write_service_marker", side_effect=write):
            self.assertEqual(self.create(), 0, self.stderr)
        self.assertEqual(phases, ["stopping", "stopped"])
        self.assertFalse(os.path.lexists(self.marker_path()))
        self.assertEqual(self.service(), "active")

    def test_marker_records_boot_invocation_and_stop_times(self):
        os.environ["INVOCATION_ID"] = "fictional-invocation-1"
        self.kill_during_capture()
        marker = self.marker()
        self.assertEqual((marker["boot_id"], marker["invocation_id"], marker["phase"]),
                         ("fictional-boot-id", "fictional-invocation-1", "stopped"))
        self.assertIsInstance(marker["stop_requested_usec"], int)
        self.assertIsInstance(marker["inactive_enter_usec"], int)

    def test_marker_cleared_after_capture_failure(self):
        os.environ["FAKE_PG_DUMP_FAIL"] = "1"
        self.assertEqual(self.create(), 1)
        self.assertFalse(os.path.lexists(self.marker_path()))
        self.assertEqual(self.service(), "active")

    def test_execstoppost_of_same_invocation_recovers(self):
        os.environ["INVOCATION_ID"] = "fictional-invocation-1"
        self.kill_during_capture()
        self.assertEqual(self.service(), "inactive")
        self.assertEqual(self.recover(), 0, self.stderr)          # same INVOCATION_ID: automatic
        self.assertEqual(self.service(), "active")
        self.assertFalse(os.path.lexists(self.marker_path()))

    def test_execstoppost_of_another_invocation_does_not_start(self):
        os.environ["INVOCATION_ID"] = "fictional-invocation-1"
        self.kill_during_capture()
        os.environ["INVOCATION_ID"] = "fictional-invocation-2"
        self.assertEqual(self.recover(), 2)
        self.assertIn("belongs to another run", self.stderr)
        self.assertEqual(self.service(), "inactive")
        self.assertTrue(os.path.isfile(self.marker_path()))

    def test_manual_recover_after_killed_manual_run(self):
        self.kill_during_capture()
        self.assertEqual(self.recover(), 0, self.stderr)          # no INVOCATION_ID: operator
        self.assertEqual(self.service(), "active")

    def test_create_never_starts_service_because_of_an_old_marker(self):
        self.kill_during_capture()
        self.clear_calls()
        self.assertEqual(self.create(), 2)
        self.assertIn("NOT started automatically", self.stderr)
        self.assertNotIn(("systemctl", "start"), self.order())
        self.assertEqual(self.service(), "inactive")

    def test_restart_failure_then_operator_deliberately_stops_service(self):
        os.environ["INVOCATION_ID"] = "fictional-invocation-1"
        os.environ["FAKE_SYSTEMCTL_START_FAIL"] = "1"
        self.assertEqual(self.create(), 3)
        self.assertEqual(self.marker()["phase"], "restart-failed")
        del os.environ["FAKE_SYSTEMCTL_START_FAIL"]
        # The operator starts it, investigates, and deliberately stops it again.
        self.systemctl("start")
        self.systemctl("stop")
        self.clear_calls()
        self.assertEqual(self.recover(), 0, self.stderr)           # automatic recovery of the same invocation
        self.assertIn("later deliberate action", self.stderr)
        self.assertNotIn(("systemctl", "start"), self.order())
        self.assertEqual(self.service(), "inactive")
        self.assertFalse(os.path.lexists(self.marker_path()))
        self.assertEqual(self.create(), 0, self.stderr)            # backups continue; service stays stopped
        self.assertEqual(self.service(), "inactive")

    def test_restart_failure_recovered_by_same_invocation_execstoppost(self):
        os.environ["INVOCATION_ID"] = "fictional-invocation-1"
        os.environ["FAKE_SYSTEMCTL_START_FAIL"] = "1"
        self.assertEqual(self.create(), 3)
        self.assertIn("CRITICAL", self.stderr)
        del os.environ["FAKE_SYSTEMCTL_START_FAIL"]
        self.assertEqual(self.recover(), 0, self.stderr)
        self.assertEqual(self.service(), "active")

    def test_reboot_makes_marker_ambiguous_and_service_stays_stopped(self):
        self.kill_during_capture()
        with mock.patch.object(afbackup, "current_boot_id", return_value="another-boot"):
            self.assertEqual(self.recover(), 2)
            self.assertIn("rebooted", self.stderr)
            self.assertEqual(self.create(), 2)
        self.assertEqual(self.service(), "inactive")
        self.assertTrue(os.path.isfile(self.marker_path()))
        self.assertEqual(self.recover("--discard-marker"), 0, self.stderr)
        self.assertFalse(os.path.lexists(self.marker_path()))
        self.assertEqual(self.service(), "inactive")

    def test_marker_obsolete_when_service_already_active(self):
        self.kill_during_capture()
        self.systemctl("start")
        self.assertEqual(self.create(), 0, self.stderr)
        self.assertIn("stale service marker removed", self.stderr)

    def test_inactive_service_gets_no_marker_and_recover_does_nothing(self):
        self.set_service("inactive")
        with mock.patch.object(afbackup, "write_service_marker", wraps=afbackup.write_service_marker) as marker:
            self.assertEqual(self.create(), 0, self.stderr)
        marker.assert_not_called()
        self.clear_calls()
        self.assertEqual(self.recover(), 0)
        self.assertIn("nothing to recover", self.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.service(), "inactive")

    def test_capture_deadline_restarts_service_and_cleans_up(self):
        self.config = self.write_config(BACKUP_CAPTURE_TIMEOUT_SECONDS="1")
        os.environ["FAKE_PG_DUMP_SLEEP"] = "5"
        started = time.monotonic()
        self.assertEqual(self.create(), 1)
        self.assertLess(time.monotonic() - started, 4.5)
        self.assertIn("capture deadline", self.stderr)
        self.assertEqual(self.service(), "active")
        self.assertFalse(os.path.lexists(self.marker_path()))
        self.assert_no_leftovers()

    def test_capture_deadline_kills_pg_dump_behind_runuser(self):
        # Regression (8.4e): the real runuser forks; a timeout must kill pg_dump too, not
        # just runuser, or the dump keeps running (and holding locks) after the backup failed.
        self.config = self.write_config(BACKUP_CAPTURE_TIMEOUT_SECONDS="1")
        pidfile = os.path.join(self.tmp, "pg_dump.pid")
        os.environ.update(FAKE_PG_DUMP_SLEEP="30", FAKE_RUNUSER_FORKS="1", FAKE_PG_DUMP_PIDFILE=pidfile)
        started = time.monotonic()
        self.assertEqual(self.create(), 1)
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("capture deadline", self.stderr)
        with open(pidfile) as fh:
            pid = int(fh.read())
        alive = True
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.05)
        if alive:
            os.kill(pid, signal.SIGKILL)
        self.assertFalse(alive, "pg_dump survived the capture deadline")
        self.assertEqual(self.service(), "active")
        self.assert_no_leftovers()

    def test_pg_dump_gets_lock_wait_timeout(self):
        self.config = self.write_config(BACKUP_PG_LOCK_WAIT_SECONDS="45")
        self.assertEqual(self.create(), 0, self.stderr)
        self.assertIn("--lock-wait-timeout=45000", self.calls("pg_dump")[0]["args"])
        self.assertEqual(self.calls("pg_dump")[0]["as"], "postgres")

    def test_unsafe_marker_refused(self):
        os.makedirs(self.state_dir, mode=0o700)
        self.touch(self.marker_path(), b"{}")
        os.chmod(self.marker_path(), 0o644)
        self.assertEqual(self.create(), 2)
        self.assertIn("service marker", self.stderr)


# --- R5: signals, broken output and restart retries ----------------------------------------------------------

class InterruptionTests(BackupTestCase):

    def test_sighup_and_sigint_restart_the_service(self):
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum.name):
                self.set_service("active")

                def interrupted(*args, **kwargs):
                    os.kill(os.getpid(), signum)
                    time.sleep(1)
                with mock.patch.object(afbackup, "dump_database", side_effect=interrupted):
                    self.assertEqual(self.create(), 1)
                self.assertIn(f"terminated by {signum.name}", self.stderr)
                self.assertEqual(self.service(), "active")
                self.assertFalse(os.path.lexists(self.marker_path()))
                self.assert_no_leftovers()

    def test_signal_during_restart_does_not_interrupt_it(self):
        real_start = afbackup.start_service

        def start(name):
            os.kill(os.getpid(), signal.SIGTERM)
            return real_start(name)
        with mock.patch.object(afbackup, "start_service", side_effect=start):
            self.assertEqual(self.create(), 1)
        self.assertEqual(self.service(), "active")
        self.assertIn("terminated by SIGTERM", self.stderr)
        self.assertEqual(self.archives(), [])

    def test_disconnected_terminal_cannot_prevent_recovery(self):
        os.environ["FAKE_PG_DUMP_FAIL"] = "1"
        self.assertEqual(self.run_tool("create", "--config", self.config, stderr=BrokenStream()), 1)
        self.assertEqual(self.service(), "active")
        self.assertFalse(os.path.lexists(self.marker_path()))

    def test_disconnected_terminal_and_sighup_together(self):
        def interrupted(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGHUP)
            time.sleep(1)
        with mock.patch.object(afbackup, "dump_database", side_effect=interrupted):
            self.assertEqual(self.run_tool("create", "--config", self.config, stderr=BrokenStream()), 1)
        self.assertEqual(self.service(), "active")

    def test_restart_retries_after_transient_failure(self):
        os.environ["FAKE_SYSTEMCTL_START_FAIL_ONCE"] = "1"
        self.assertEqual(self.create(), 0, self.stderr)
        self.assertIn("start attempt 1/3", self.stderr)
        self.assertEqual(self.service(), "active")

    def test_restart_retries_after_ordinary_exception(self):
        real_start = afbackup.start_service
        calls = {"n": 0}

        def start(name):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("fictional transient error")
            return real_start(name)
        with mock.patch.object(afbackup, "start_service", side_effect=start):
            self.assertEqual(self.create(), 0, self.stderr)
        self.assertEqual(calls["n"], 2)

    def test_restart_failure_after_all_retries_is_critical(self):
        os.environ["FAKE_SYSTEMCTL_START_FAIL"] = "1"
        self.assertEqual(self.create(), 3)
        self.assertIn("start attempt 3/3", self.stderr)
        self.assertIn("CRITICAL", self.stderr)
        self.assertEqual(self.archives(), [])
        self.assert_no_leftovers()


# --- R7: sanitized subprocess environment --------------------------------------------------------------------

class EnvironmentTests(BackupTestCase):

    def test_hostile_environment_never_reaches_subprocesses(self):
        hostile = {"PGHOST": "evil.example", "PGPORT": "6543", "PGUSER": "postgres", "PGOPTIONS": "-c role=postgres",
                   "PGSERVICE": "evil", "PGPASSFILE": "/tmp/evil", "AGE_FICTIONAL": "x",
                   "LD_LIBRARY_PATH": "/nonexistent-fictional"}
        os.environ.update(hostile)
        self.assertEqual(self.create(), 0, self.stderr)
        archive = os.path.join(self.dest, self.archives()[-1])
        self.assertEqual(self.restore(archive), 0, self.stderr)
        leaks = {c["tool"]: c["env_leaks"] for c in self.calls() if c["env_leaks"]}
        self.assertEqual(leaks, {}, "inherited variables reached a subprocess")
        self.assertEqual(afbackup.clean_env()["PATH"], afbackup.SAFE_PATH)
        self.assertEqual(afbackup.clean_env()["LC_ALL"], "C.UTF-8")


# --- M4/R4: media completeness and traversal races ------------------------------------------------------------

class MediaTraversalTests(BackupTestCase):

    def setUp(self):
        super().setUp()
        self.secret_dir = self.mkdir("etc-applyforge")
        self.touch(os.path.join(self.secret_dir, "notes.txt"), SECRET)
        self.touch(os.path.join(self.secret_dir, "fictional_resume.pdf"), SECRET)

    def assert_no_secret_anywhere(self):
        for directory in (self.dest, self.tmp):
            for dirpath, _, files in os.walk(directory):
                if dirpath.startswith(self.secret_dir):
                    continue
                for name in files:
                    path = os.path.join(dirpath, name)
                    if os.path.islink(path) or not os.path.isfile(path):
                        continue
                    with open(path, "rb") as fh:
                        self.assertNotIn(SECRET, fh.read(), path)

    def swap_dir_for_symlink(self, rel):
        real = os.path.join(self.media, *rel.split("/"))
        os.rename(real, real + ".moved")
        os.symlink(self.secret_dir, real)

    def test_directory_swapped_for_symlink_during_scan(self):
        real_lstat = afbackup._lstat_at

        def lstat(name, dir_fd):
            st = real_lstat(name, dir_fd)
            if name == "nested" and not os.path.islink(os.path.join(self.media, "resumes", "nested")):
                self.swap_dir_for_symlink("resumes/nested")
            return st
        with mock.patch.object(afbackup, "_lstat_at", side_effect=lstat):
            self.assertEqual(self.create(), 1)
        self.assertIn("changed during the backup", self.stderr)
        self.assertEqual(self.archives(), [])
        self.assertEqual(self.service(), "active")
        self.assert_no_secret_anywhere()

    def test_file_swapped_for_symlink_during_scan(self):
        real_lstat = afbackup._lstat_at
        victim = os.path.join(self.media, "resumes", "fictional_resume.pdf")

        def lstat(name, dir_fd):
            st = real_lstat(name, dir_fd)
            if name == "fictional_resume.pdf" and not os.path.islink(victim):
                os.unlink(victim)
                os.symlink(os.path.join(self.secret_dir, "fictional_resume.pdf"), victim)
            return st
        with mock.patch.object(afbackup, "_lstat_at", side_effect=lstat):
            self.assertEqual(self.create(), 1)
        self.assertIn("changed during the scan", self.stderr)
        self.assertEqual(self.archives(), [])
        self.assert_no_secret_anywhere()

    def test_directory_swapped_between_scan_and_capture(self):
        real_scan = afbackup.scan_media

        def scan_then_swap(root, deadline=None):
            result = real_scan(root, deadline)
            self.swap_dir_for_symlink("resumes")
            return result
        with mock.patch.object(afbackup, "scan_media", side_effect=scan_then_swap):
            self.assertEqual(self.create(), 1)
        self.assertIn("changed while the backup was running", self.stderr)
        self.assertEqual(self.archives(), [])
        self.assert_no_secret_anywhere()

    def test_directory_replaced_by_real_copy_between_scan_and_capture(self):
        real_scan = afbackup.scan_media
        resumes = os.path.join(self.media, "resumes")

        def scan_then_replace(root, deadline=None):
            result = real_scan(root, deadline)
            os.rename(resumes, resumes + ".moved")
            shutil.copytree(self.secret_dir, resumes)
            return result
        with mock.patch.object(afbackup, "scan_media", side_effect=scan_then_replace):
            self.assertEqual(self.create(), 1)
        self.assertIn("changed while the backup was running", self.stderr)
        self.assert_no_secret_anywhere_except(resumes)

    def assert_no_secret_anywhere_except(self, allowed_dir):
        self.assertEqual(self.archives(), [])
        for dirpath, _, files in os.walk(self.dest):
            for name in files:
                with open(os.path.join(dirpath, name), "rb") as fh:
                    self.assertNotIn(SECRET, fh.read())

    def test_filesystem_crossing_refused(self):
        real_lstat = afbackup._lstat_at

        def lstat(name, dir_fd):
            st = real_lstat(name, dir_fd)
            if name == "nested":
                values = list(st[:10])
                values[2] = st.st_dev + 1     # st_dev of a mounted filesystem
                return os.stat_result(values)
            return st
        with mock.patch.object(afbackup, "_lstat_at", side_effect=lstat):
            self.assertEqual(self.create(), 1)
        self.assertIn("different filesystem", self.stderr)

    @unittest.skipUnless(NOT_ROOT, "root bypasses permission checks")
    def test_unreadable_directory_fails_backup(self):
        os.chmod(os.path.join(self.media, "resumes", "nested"), 0o000)
        self.assertEqual(self.create(), 1)
        self.assertIn("would be incomplete", self.stderr)
        self.assertEqual(self.archives(), [])
        self.assertEqual(self.service(), "active")
        self.assert_no_leftovers()

    @unittest.skipUnless(NOT_ROOT, "root bypasses permission checks")
    def test_unreadable_file_fails_backup(self):
        os.chmod(os.path.join(self.media, "resumes", "fictional_resume.pdf"), 0o000)
        self.assertEqual(self.create(), 1)
        self.assertIn("cannot read media file resumes/fictional_resume.pdf", self.stderr)

    def test_skipped_entries_reported_explicitly(self):
        os.mkfifo(os.path.join(self.media, "a-fifo"))
        os.symlink("/etc/hostname", os.path.join(self.media, "a-link"))
        archive = self.make_backup()
        self.assertIn("WARNING: 2 entries under MEDIA_ROOT are not regular files", self.stderr)
        with tarfile.open(fileobj=io.BytesIO(self.plaintext(archive))) as tar:
            manifest = json.loads(tar.extractfile(afbackup.MANIFEST).read())
        self.assertEqual(manifest["media"]["skipped_not_regular_files"], ["a-fifo", "a-link"])

    def test_media_changing_during_backup_fails_cleanly(self):
        real_write_tar = afbackup.write_tar

        def mutate_then_write(*args, **kwargs):
            with open(os.path.join(self.media, "empty.bin"), "wb") as fh:
                fh.write(b"changed")
            return real_write_tar(*args, **kwargs)
        with mock.patch.object(afbackup, "write_tar", side_effect=mutate_then_write):
            self.assertEqual(self.create(), 1)
        self.assertIn("changed while the backup was running", self.stderr)
        self.assertEqual(self.archives(), [])
        self.assertEqual(self.service(), "active")
        self.assert_no_leftovers()


# --- M6/R15: manifest, member and resource validation ---------------------------------------------------------

class ValidationTests(BackupTestCase):
    DUMP_BYTES = b"PGDMP fictional"
    MEDIA_BYTES = b"fictional media"

    def files(self):
        return [(afbackup.DUMP, self.DUMP_BYTES), ("media/a.txt", self.MEDIA_BYTES)]

    def assert_rejected(self, archive, phrase):
        self.assertEqual(self.verify(archive), 1, self.stderr)
        self.assertIn(phrase, self.stderr)
        self.assert_no_leftovers()

    def test_valid_hand_built_archive_is_accepted(self):
        archive = self.make_archive([(afbackup.MANIFEST, self.manifest_for(self.files()))] + self.files())
        self.assertEqual(self.verify(archive), 0, self.stderr)
        self.assertIn("compare the hash above with your independent record", self.stdout)

    def test_checksum_mismatch_rejected(self):
        archive = self.make_archive([(afbackup.MANIFEST, self.manifest_for(self.files())),
                                     (afbackup.DUMP, self.DUMP_BYTES), ("media/a.txt", b"fictional MEDIA")])
        self.assert_rejected(archive, "checksum mismatch for media/a.txt")

    def test_size_mismatch_rejected_before_extraction(self):
        archive = self.make_archive([(afbackup.MANIFEST, self.manifest_for(self.files())),
                                     (afbackup.DUMP, self.DUMP_BYTES), ("media/a.txt", b"fictional media, longer")])
        real_space = afbackup.require_space
        purposes = []

        def space(path, needed, what):
            purposes.append(what)
            return real_space(path, needed, what)
        with mock.patch.object(afbackup, "require_space", side_effect=space):
            self.assert_rejected(archive, "does not match the manifest")
        self.assertNotIn("extract the archive", purposes)

    def test_unlisted_missing_and_duplicate_members_rejected(self):
        cases = {
            "not listed in the manifest": [(afbackup.MANIFEST, self.manifest_for(self.files()))] + self.files()
            + [("media/extra.txt", b"x")],
            "missing file(s) listed in the manifest": [(afbackup.MANIFEST, self.manifest_for(self.files())),
                                                       (afbackup.DUMP, self.DUMP_BYTES)],
            "appears twice": [(afbackup.MANIFEST, self.manifest_for(self.files()))] + self.files()
            + [("media/a.txt", self.MEDIA_BYTES)],
            "no manifest.json": self.files(),
        }
        for phrase, members in cases.items():
            with self.subTest(phrase=phrase):
                self.assert_rejected(self.make_archive(members), phrase)

    def test_unsafe_names_rejected(self):
        for bad in ("media/../escape.txt", "/etc/passwd", "other/file.txt", "media//x"):
            with self.subTest(name=bad):
                archive = self.make_archive([(afbackup.MANIFEST, self.manifest_for(self.files())), (bad, b"x")]
                                            + self.files())
                self.assert_rejected(archive, "unexpected or unsafe name")

    def test_link_device_fifo_and_directory_members_rejected(self):
        cases = []
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            link = tarfile.TarInfo("media/link")
            link.type, link.linkname = kind, "media/a.txt"
            cases.append(link)
        for kind in (tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE, tarfile.DIRTYPE):
            special = tarfile.TarInfo("media/special")
            special.type = kind
            cases.append(special)
        for member in cases:
            with self.subTest(type=member.type):
                archive = self.make_archive([(afbackup.MANIFEST, self.manifest_for(self.files())), (member, None)]
                                            + self.files())
                self.assert_rejected(archive, "is not a plain regular file")

    def test_member_type_check_rejects_sparse_and_contiguous(self):
        for kind in (tarfile.GNUTYPE_SPARSE, tarfile.CONTTYPE):
            member = tarfile.TarInfo("media/x")
            member.type = kind
            with self.subTest(type=kind), self.assertRaises(afbackup.Failed):
                afbackup.check_member_type(member)
        sparse_pax = tarfile.TarInfo("media/x")
        sparse_pax.sparse = [(0, 1)]
        with self.assertRaises(afbackup.Failed):
            afbackup.check_member_type(sparse_pax)

    def test_excessive_member_count_rejected(self):
        files = self.files() + [(f"media/f{i}.txt", b"x") for i in range(5)]
        archive = self.make_archive([(afbackup.MANIFEST, self.manifest_for(files))] + files)
        with mock.patch.object(afbackup, "MAX_ARCHIVE_MEMBERS", 4):
            self.assert_rejected(archive, "more than 4 members")

    def test_oversized_extended_header_rejected_before_tarfile_parses_it(self):
        big = tarfile.TarInfo("media/a.txt")
        big.pax_headers = {"comment": "x" * 8192}
        archive = self.make_archive([(afbackup.MANIFEST, self.manifest_for(self.files())),
                                     (afbackup.DUMP, self.DUMP_BYTES), (big, self.MEDIA_BYTES)])
        with mock.patch.object(afbackup, "MAX_TAR_HEADER_BYTES", 1024), \
                mock.patch.object(afbackup.tarfile, "open", side_effect=AssertionError("tarfile reached")):
            self.assert_rejected(archive, "oversized extended header")

    def test_archive_size_limit(self):
        archive = self.make_archive([(afbackup.MANIFEST, self.manifest_for(self.files()))] + self.files())
        with mock.patch.object(afbackup, "MAX_ARCHIVE_BYTES", 100):
            self.assertEqual(self.verify(archive), 2)
        self.assertIn("larger than the limit", self.stderr)

    def test_insufficient_disk_space_rejected_before_extraction(self):
        archive = self.make_archive([(afbackup.MANIFEST, self.manifest_for(self.files()))] + self.files())
        real_free = afbackup.disk_free
        calls = {"n": 0}

        def free(path):
            calls["n"] += 1
            return real_free(path) if calls["n"] <= 2 else 1024
        with mock.patch.object(afbackup, "disk_free", side_effect=free):
            self.assert_rejected(archive, "insufficient disk space")

    def test_deeply_nested_manifest_rejected_cleanly(self):
        archive = self.make_archive([(afbackup.MANIFEST, b"[" * 200000 + b"]" * 200000)] + self.files())
        self.assert_rejected(archive, "invalid manifest")
        self.assertNotIn("Traceback", self.stderr)

    def test_bad_manifest_versions_and_fields_rejected(self):
        cases = {
            "unsupported format_version": {"format_version": 99},
            "not an ApplyForge backup": {"format": "something-else"},
            "created_utc missing or malformed": {"created_utc": "yesterday"},
            "database section missing or malformed": {"database": {"name": "Bad Name", "dump_file": "x"}},
        }
        for phrase, overrides in cases.items():
            with self.subTest(phrase=phrase):
                archive = self.make_archive([(afbackup.MANIFEST, self.manifest_for(self.files(), **overrides))]
                                            + self.files())
                self.assert_rejected(archive, phrase)

    def test_invalid_json_and_non_tar_rejected(self):
        self.assert_rejected(self.make_archive([(afbackup.MANIFEST, b"{not json")] + self.files()), "not valid JSON")
        huge_size = bytearray(4096)
        huge_size[0:10] = b"media/huge"
        huge_size[124:136] = b"\x80" + b"\xff" * 11          # base-256 size: astronomically large
        payloads = {"huge base-256 size (regression)": bytes(huge_size), "text": b"not a tar archive\n" * 300}
        for seed in range(8):                               # deterministic "random" blocks
            payloads[f"seeded random {seed}"] = hashlib.sha256(bytes([seed])).digest() * 128
        for label, payload in payloads.items():
            with self.subTest(payload=label):
                archive = os.path.join(self.tmp, "random.tar.age")
                self.touch(os.path.join(self.tmp, "random.bin"), payload)
                subprocess.run([os.path.join(self.bin, "age"), "--encrypt", "--recipients-file", self.recipients,
                                "--output", archive, os.path.join(self.tmp, "random.bin")], check=True)
                self.assert_rejected(archive, "not a valid backup archive")
                self.assertNotIn("unexpected error", self.stderr)

    def test_tar_header_precheck_accepts_valid_archives_with_extended_headers(self):
        big = tarfile.TarInfo("media/a.txt")
        big.pax_headers = {"comment": "fictional " * 50}
        archive = self.make_archive([(afbackup.MANIFEST, self.manifest_for(self.files())),
                                     (afbackup.DUMP, self.DUMP_BYTES), (big, self.MEDIA_BYTES)])
        self.assertEqual(self.verify(archive), 0, self.stderr)

    def test_verify_with_wrong_expected_hash_refused_before_decrypting(self):
        archive = self.make_archive([(afbackup.MANIFEST, self.manifest_for(self.files()))] + self.files())
        self.clear_calls()
        self.assertEqual(self.verify(archive, expected="e" * 64), 2)
        self.assertEqual(self.calls("age"), [])


# --- Corruption and wrong keys -------------------------------------------------------------------------------

class CorruptionAndKeyTests(BackupTestCase):

    def damaged_copy(self, archive, transform):
        with open(archive, "rb") as fh:
            data = bytearray(fh.read())
        return self.touch(os.path.join(self.tmp, "damaged.tar.age"), transform(data))

    def test_flipped_byte_and_truncation_rejected(self):
        archive = self.make_backup()

        def flip(data):
            data[-10] ^= 0x01
            return bytes(data)
        for transform in (flip, lambda d: bytes(d[: len(d) // 2])):
            with self.subTest(transform=transform):
                self.assertEqual(self.verify(self.damaged_copy(archive, transform)), 1)
                self.assertIn("decryption failed", self.stderr)
                self.assert_no_leftovers()

    def test_unencrypted_file_rejected(self):
        plain = self.touch(os.path.join(self.tmp, "plain.tar"), b"not an age file")
        self.assertEqual(self.verify(plain), 1)
        self.assertIn("no age header", self.stderr)

    def test_wrong_identity_rejected(self):
        archive = self.make_backup()
        self.assertEqual(self.verify(archive, identity=self.wrong_identity), 1)
        self.assertIn("wrong identity", self.stderr)
        self.assert_no_leftovers()

    def test_restore_with_wrong_identity_changes_nothing(self):
        archive = self.make_backup()
        target = os.path.join(self.tmp, "restored-media")
        self.assertEqual(self.restore(archive, media=target, identity=self.wrong_identity), 1)
        self.assertEqual(self.calls("createdb") + self.calls("pg_restore") + self.calls("psql"), [])
        self.assertFalse(os.path.lexists(target))
        self.assert_no_leftovers()


# --- M5/R14/R16: retention -----------------------------------------------------------------------------------

class RetentionTests(BackupTestCase):
    NOW = dt.datetime(2026, 11, 18, 3, 30, tzinfo=dt.timezone.utc)

    def retain(self, days=30, min_keep=1, trusted=None, now=None):
        with contextlib.redirect_stderr(io.StringIO()):
            return afbackup.apply_retention(self.dest, days, now or self.NOW, min_keep=min_keep, trusted=trusted)

    def name_of(self, path):
        return os.path.basename(path)

    def test_deletes_only_old_complete_archives(self):
        old_40 = self.write_old_archive(40, self.NOW)
        old_31 = self.write_old_archive(31, self.NOW)
        recent = self.write_old_archive(29, self.NOW)
        newest = self.write_old_archive(1, self.NOW)
        keep = [recent, newest]
        stamp = (self.NOW - dt.timedelta(days=90)).strftime(afbackup.TIMESTAMP_FORMAT)
        keep += [
            self.touch(os.path.join(self.dest, f".applyforge-backup-{stamp}.tar.age.partial"), afbackup.AGE_HEADER),
            self.touch(os.path.join(self.dest, "notes.txt"), b"unrelated"),
            self.touch(os.path.join(self.dest, f"applyforge-backup-{stamp}.tar.age.sha256"), b"unrelated"),
            self.touch(os.path.join(self.dest, "applyforge-backup-20201399T000000Z.tar.age"), afbackup.AGE_HEADER),
            self.touch(os.path.join(self.dest, "applyforge-backup-20200103T000000Z.tar.age"), b"no age header"),
            os.path.join(self.dest, afbackup.MARKER),
        ]
        old_dir = os.path.join(self.dest, "applyforge-backup-20200102T000000Z.tar.age")
        os.mkdir(old_dir)
        old_link = os.path.join(self.dest, "applyforge-backup-20200105T000000Z.tar.age")
        os.symlink(recent, old_link)
        keep += [old_dir, old_link]
        deleted = self.retain()
        self.assertEqual(sorted(deleted), sorted(self.name_of(p) for p in (old_40, old_31)))
        for path in keep:
            self.assertTrue(os.path.lexists(path), path)

    def test_min_keep_retains_newest_archives_regardless_of_age(self):
        old = [self.write_old_archive(days, self.NOW) for days in range(40, 50)]
        self.assertEqual(len(self.retain(min_keep=7)), 3)
        self.assertEqual(self.archives(), sorted(self.name_of(p) for p in old)[-7:])

    def test_indexed_archives_take_protected_slots_before_planted_ones(self):
        real = [self.write_old_archive(days, self.NOW) for days in range(40, 48)]     # 8 indexed, all old
        planted = [self.write_old_archive(days, self.NOW) for days in range(1, 8)]    # 7 recent, not indexed
        trusted = {self.name_of(p): "a" * 64 for p in real}
        deleted = self.retain(min_keep=7, trusted=trusted)
        self.assertEqual(deleted, [self.name_of(real[-1])])             # only the oldest indexed one
        for path in real[:7] + planted:
            self.assertTrue(os.path.exists(path))

    def test_future_dated_archives_never_take_protected_slots(self):
        real = [self.write_old_archive(days, self.NOW) for days in range(40, 43)]
        future = [self.write_old_archive(-3650 - i, self.NOW) for i in range(7)]   # ten years ahead
        with contextlib.redirect_stderr(io.StringIO()) as err:
            afbackup.apply_retention(self.dest, 30, self.NOW, min_keep=3)
        self.assertIn("dated in the future", err.getvalue())
        for path in real + future:
            self.assertTrue(os.path.exists(path), path)

    def test_clock_jump_cannot_reduce_below_min_keep(self):
        for days in (3, 2, 1):
            self.write_old_archive(days, self.NOW)
        self.retain(min_keep=7, now=self.NOW + dt.timedelta(days=3650))
        self.assertEqual(len(self.archives()), 3)

    def test_defaults_and_configurability(self):
        cfg = afbackup.load_config(self.write_config(BACKUP_RETENTION_DAYS=None), afbackup.CONFIG_REQUIRED_KEYS)
        self.assertEqual((cfg.retention_days, cfg.min_keep), (30, 7))
        self.config = self.write_config(BACKUP_RETENTION_DAYS="7", BACKUP_MIN_KEEP="1")
        ten_days = self.write_old_archive(10)
        three_days = self.write_old_archive(3)
        self.assertEqual(self.create(), 0, self.stderr)
        self.assertFalse(os.path.exists(ten_days))
        self.assertTrue(os.path.exists(three_days))

    def test_failed_backup_never_prunes(self):
        old = self.write_old_archive(100)
        self.config = self.write_config(BACKUP_MIN_KEEP="1")
        os.environ["FAKE_PG_DUMP_FAIL"] = "1"
        self.assertEqual(self.create(), 1)
        self.assertTrue(os.path.exists(old))

    def test_retention_failure_and_termination_reported_distinctly(self):
        with mock.patch.object(afbackup, "apply_retention", side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(self.create(), afbackup.EXIT_RETENTION)
        self.assertIn("RETENTION FAILED", self.stderr)
        for name in self.archives():
            os.unlink(os.path.join(self.dest, name))
        time.sleep(1.1)
        with mock.patch.object(afbackup, "apply_retention", side_effect=afbackup.Terminated("terminated by SIGTERM")):
            self.assertEqual(self.create(), 1)
        self.assertIn("during retention", self.stderr)
        self.assertNotIn("RETENTION FAILED", self.stderr)
        self.assertEqual(len(self.archives()), 1)

    def test_invalid_retention_settings_refused(self):
        for key, value in (("BACKUP_RETENTION_DAYS", "0"), ("BACKUP_RETENTION_DAYS", "3651"),
                           ("BACKUP_RETENTION_DAYS", "abc"), ("BACKUP_MIN_KEEP", "0"), ("BACKUP_MIN_KEEP", "1001")):
            with self.subTest(key=key, value=value):
                self.config = self.write_config(**{key: value})
                self.assertEqual(self.create(), 2)
                self.assertIn(key, self.stderr)
                self.assertEqual(self.calls(), [])


# --- Destination, configuration and path security (M7, R11, L3, L5) --------------------------------------

class DestinationAndConfigTests(BackupTestCase):

    def assert_refused_untouched(self, phrase):
        self.assertEqual(self.create(), 2, self.stderr)
        self.assertIn(phrase, self.stderr)
        self.assertEqual(self.calls(), [], "no tool may run after a refusal")
        self.assertEqual(self.service(), "active")
        self.assertEqual(self.archives(), [])

    def test_refuses_when_destination_not_mounted(self):
        with mock.patch.object(afbackup, "is_mount_point", return_value=False):
            self.assert_refused_untouched("is not a mount point")

    def test_refuses_missing_or_symlinked_marker_or_destination(self):
        marker = os.path.join(self.dest, afbackup.MARKER)
        os.unlink(marker)
        self.assert_refused_untouched("marker file")
        os.symlink(self.recipients, marker)
        self.assert_refused_untouched("marker file")
        link = os.path.join(self.tmp, "dest-link")
        os.symlink(self.dest, link)
        self.config = self.write_config(BACKUP_DESTINATION=link)
        self.assert_refused_untouched("does not exist or is a symbolic link")

    def test_allowed_filesystem_types_enforced_when_configured(self):
        self.config = self.write_config(BACKUP_ALLOWED_FS_TYPES="nfs4,cifs")
        with mock.patch.object(afbackup, "mount_fs_type", return_value="ext4"):
            self.assert_refused_untouched("not in BACKUP_ALLOWED_FS_TYPES")
        with mock.patch.object(afbackup, "mount_fs_type", return_value="nfs4"):
            self.assertEqual(self.create(), 0, self.stderr)

    def test_mount_fs_type_parses_mountinfo(self):
        sample = ("36 35 98:0 / /mnt/applyforge-backups rw,noatime master:1 - nfs4 nas:/export rw\n"
                  "37 35 98:0 / /mnt/with\\040space rw - cifs //nas/share rw\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=sample)):
            self.assertEqual(afbackup.mount_fs_type("/mnt/applyforge-backups"), "nfs4")
            self.assertEqual(afbackup.mount_fs_type("/mnt/with space"), "cifs")
            self.assertIsNone(afbackup.mount_fs_type("/mnt/other"))

    def test_refuses_private_key_or_invalid_recipients(self):
        self.touch(self.recipients, f"{RECIPIENT}\nAGE-SECRET-KEY-1FICTIONALNOTAREALKEY\n".encode())
        self.assert_refused_untouched("PRIVATE key")
        for content in (b"ssh-ed25519 AAAAfictional\n", b"# nothing\n", b"age1tooshort\n"):
            with self.subTest(content=content):
                self.touch(self.recipients, content)
                self.assertEqual(self.create(), 2)
                self.assertEqual(self.calls(), [])

    def test_refuses_unknown_duplicate_or_missing_settings(self):
        with open(self.config, "a") as fh:
            fh.write("BACKUP_DESTINATON=/typo\n")
        self.assert_refused_untouched("unknown or malformed setting")
        self.config = self.write_config()
        with open(self.config, "a") as fh:
            fh.write(f"BACKUP_DESTINATION={self.dest}\n")
        self.assert_refused_untouched("duplicate setting BACKUP_DESTINATION")
        self.config = self.write_config(MEDIA_ROOT=None)
        self.assert_refused_untouched("missing required setting")
        self.config = self.write_config(MEDIA_ROOT="relative/media")
        self.assert_refused_untouched("absolute")

    def test_refuses_overlapping_paths(self):
        cases = {
            "work inside destination": {"BACKUP_WORK_DIR": os.path.join(self.dest, "work")},
            "destination inside media": {"BACKUP_DESTINATION": os.path.join(self.media, "backups")},
            "media inside work": {"MEDIA_ROOT": os.path.join(self.work, "media")},
            "state inside destination": {"BACKUP_STATE_DIR": os.path.join(self.dest, "state")},
            "work equals state": {"BACKUP_WORK_DIR": self.state_dir},
        }
        for label, overrides in cases.items():
            with self.subTest(case=label):
                self.config = self.write_config(**overrides)
                self.assert_refused_untouched("must be separate directories")
        self.config = self.write_config(BACKUP_LOCK_FILE=os.path.join(self.dest, "x.lock"))
        self.assert_refused_untouched("BACKUP_LOCK_FILE must not be inside")

    def test_refuses_media_root_under_sensitive_directories(self):
        for media in ("/etc/applyforge/media", "/root/media", "/home/someone/media", "/srv/applyforge/media"):
            with self.subTest(media=media):
                self.config = self.write_config(MEDIA_ROOT=media)
                self.assert_refused_untouched("MEDIA_ROOT must not be under")

    def test_refuses_unsafe_config_and_recipients_files(self):
        os.chmod(self.config, 0o664)
        self.assert_refused_untouched("must not be writable by group or others")
        os.chmod(self.config, 0o644)
        os.chmod(self.recipients, 0o646)
        self.assert_refused_untouched("recipients file")
        os.chmod(self.recipients, 0o644)
        with mock.patch.object(afbackup, "TRUSTED_UID", os.getuid() + 1):
            self.assert_refused_untouched("must be owned by root")
        link = os.path.join(self.tmp, "link.conf")
        os.symlink(self.config, link)
        self.config = link
        self.assert_refused_untouched("not a symbolic link")

    def test_refuses_trusted_files_in_writable_directories(self):
        open_dir = self.mkdir("open-config-dir")
        os.chmod(open_dir, 0o777)
        self.config = self.write_config(path=os.path.join(open_dir, "backup.conf"))
        self.assert_refused_untouched("must not be writable by group or others")
        os.chmod(open_dir, 0o1777)            # sticky: entries cannot be replaced by others
        self.assertEqual(self.create(), 0, self.stderr)

    def test_refuses_missing_tools(self):
        os.unlink(os.path.join(self.bin, "age"))
        with mock.patch.object(afbackup, "SAFE_PATH", self.bin):
            self.assert_refused_untouched("required tool(s) not found on the fixed search path: age")

    def test_refuses_insecure_work_dir(self):
        os.makedirs(self.work, mode=0o755)
        os.chmod(self.work, 0o755)
        self.assertEqual(self.create(), 2)
        self.assertIn("must not be accessible to group or others", self.stderr)


# --- Restore refusals -------------------------------------------------------------------------------------------

class RestoreRefusalTests(BackupTestCase):

    def setUp(self):
        super().setUp()
        self.archive = self.make_backup()
        self.target = os.path.join(self.tmp, "restored-media")

    def assert_refused_untouched(self, code, phrase):
        before = self.databases()
        self.assertEqual(code, 2, self.stderr)
        self.assertIn(phrase, self.stderr)
        self.assertEqual(self.calls("createdb") + self.calls("pg_restore"), [])
        self.assertEqual(self.databases(), before)
        self.assert_no_leftovers()

    def test_refuses_existing_database(self):
        self.set_databases(["postgres", "applyforge", "applyforge_restore_test"])
        self.assert_refused_untouched(self.restore(self.archive, media=self.target), "already exists")

    def test_refuses_production_and_reserved_database_names(self):
        for name, phrase in (("applyforge", "production database"), ("postgres", "plain new database name"),
                             ("Bad-Name", "plain new database name")):
            with self.subTest(name=name):
                self.assert_refused_untouched(self.restore(self.archive, db=name, media=self.target), phrase)

    def test_refuses_non_empty_file_or_symlink_media_target(self):
        existing = self.touch(os.path.join(self.target, "keep.txt"), b"existing fictional file")
        self.assert_refused_untouched(self.restore(self.archive, media=self.target), "is not empty")
        with open(existing, "rb") as fh:
            self.assertEqual(fh.read(), b"existing fictional file")
        as_file = self.touch(os.path.join(self.tmp, "file-target"), b"x")
        self.assert_refused_untouched(self.restore(self.archive, media=as_file), "is not a directory")

    def test_refuses_media_target_overlapping_protected_or_sensitive_areas(self):
        for target, phrase in ((self.media, "overlaps"), (os.path.join(self.dest, "restored"), "overlaps"),
                               (os.path.join(self.state_dir, "restored"), "overlaps"),
                               ("/etc/applyforge-restored", "must not be under")):
            with self.subTest(target=target):
                self.assert_refused_untouched(self.restore(self.archive, media=target), phrase)

    def test_refuses_confirmation_mismatch(self):
        self.assert_refused_untouched(
            self.restore(self.archive, media=self.target, confirm_db="applyforge_restore_tset"), "--confirm-database")
        self.assert_refused_untouched(
            self.restore(self.archive, media=self.target, confirm_media=self.target + "-other"), "--confirm-media-dir")

    def test_refuses_missing_restore_os_account(self):
        with mock.patch.object(afbackup, "os_user_exists", return_value=False):
            self.assert_refused_untouched(self.restore(self.archive, media=self.target),
                                          f"OS account {RESTORE_ROLE}")

    @unittest.skipUnless(NOT_ROOT, "root bypasses permission checks")
    def test_refuses_without_permissions(self):
        parent = self.mkdir("readonly-parent")
        os.chmod(parent, 0o500)
        self.assert_refused_untouched(self.restore(self.archive, media=os.path.join(parent, "media")),
                                      "no write permission")
        os.chmod(self.identity, 0o000)
        self.assert_refused_untouched(self.restore(self.archive, media=self.target), "insufficient permissions")

    def test_restores_into_existing_empty_directory(self):
        os.mkdir(self.target)
        self.assertEqual(self.restore(self.archive, media=self.target), 0, self.stderr)


# --- Cleanup after failures ---------------------------------------------------------------------------------------

class CleanupTests(BackupTestCase):

    def test_dump_failure_cleans_up(self):
        os.environ["FAKE_PG_DUMP_FAIL"] = "1"
        self.assertEqual(self.create(), 1)
        self.assertIn("pg_dump failed", self.stderr)
        self.assert_no_leftovers()

    def test_restore_failure_removes_staging_and_reports_created_database(self):
        archive = self.make_backup()
        target = os.path.join(self.tmp, "restored-media")
        os.environ["FAKE_PG_RESTORE_FAIL"] = "1"
        self.assertEqual(self.restore(archive, media=target), 1)
        self.assertFalse(os.path.lexists(target))
        self.assertIn("sudo -u postgres dropdb applyforge_restore_test", self.stderr)
        self.assertIn("applyforge_restore_test", self.databases())
        self.assert_no_leftovers()

    def test_createdb_failure_restores_nothing(self):
        archive = self.make_backup()
        target = os.path.join(self.tmp, "restored-media")
        real_exists = afbackup.database_exists
        with mock.patch.object(afbackup, "database_exists", side_effect=lambda cfg, name: (
                self.set_databases(self.databases() + [name]), real_exists(cfg, "never-listed"))[1]):
            self.assertEqual(self.restore(archive, media=target), 1)
        self.assertIn("createdb failed, nothing was restored", self.stderr)
        self.assertEqual(self.calls("pg_restore"), [])
        self.assert_no_leftovers()


# --- Service maintenance window -----------------------------------------------------------------------------------

class ServiceRestartTests(BackupTestCase):

    def test_service_stopped_during_capture_and_started_before_encryption(self):
        self.assertEqual(self.create(), 0, self.stderr)
        self.assertEqual(self.calls("pg_dump")[0]["service"], "inactive")
        order = self.order()
        stop = order.index(("systemctl", "stop"))
        dump = next(i for i, c in enumerate(order) if c[0] == "pg_dump")
        start = order.index(("systemctl", "start"))
        encrypt = order.index(("age", "--encrypt"))
        self.assertLess(stop, dump)
        self.assertLess(dump, start)
        self.assertLess(start, encrypt)

    def test_inactive_or_failed_service_is_not_started(self):
        for state in ("inactive", "failed"):
            with self.subTest(state=state):
                self.clear_calls()
                self.set_service(state)
                for name in self.archives():
                    os.unlink(os.path.join(self.dest, name))
                time.sleep(1.1)
                self.assertEqual(self.create(), 0, self.stderr)
                self.assertEqual([c for c in self.order() if c in (("systemctl", "stop"), ("systemctl", "start"))], [])
                self.assertEqual(self.service(), state)

    def test_stop_failure_still_attempts_start(self):
        os.environ["FAKE_SYSTEMCTL_STOP_FAIL"] = "1"
        self.assertEqual(self.create(), 1)
        self.assertIn("could not stop", self.stderr)
        self.assertIn(("systemctl", "start"), self.order())
        self.assertEqual(self.calls("pg_dump"), [])

    def test_transitional_state_refused_without_stopping(self):
        for state in ("activating", "deactivating", "reloading"):
            with self.subTest(state=state):
                self.clear_calls()
                self.set_service(state)
                self.assertEqual(self.create(), 2)
                self.assertNotIn(("systemctl", "stop"), self.order())


# --- Low findings: root, clean errors, fixed PATH --------------------------------------------------------------

class PrivilegeAndErrorTests(BackupTestCase):

    @unittest.skipUnless(NOT_ROOT, "tests must run as a normal user to check the root requirement")
    def test_root_required_for_create_restore_and_recover(self):
        archive = self.make_backup()
        with mock.patch.object(afbackup, "require_root", side_effect=ORIGINAL_REQUIRE_ROOT):
            self.assertEqual(self.create(), 2)
            self.assertEqual(self.recover(), 2)
            self.assertEqual(self.restore(archive), 2)
            self.assertIn("must run as root", self.stderr)
        self.assertEqual(self.calls(), [])

    def test_unexpected_error_is_reported_cleanly_and_service_restarted(self):
        with mock.patch.object(afbackup, "scan_media", side_effect=ValueError("fictional surprise")):
            self.assertEqual(self.create(), 1)
        self.assertIn("unexpected error (ValueError: fictional surprise)", self.stderr)
        self.assertNotIn("Traceback", self.stderr)
        self.assertEqual(self.service(), "active")
        self.assert_no_leftovers()

    def test_error_messages_skip_ages_generic_report_hint(self):
        # Real age 1.2 ends every error with this hint line (seen in the real-age integration test).
        stderr = (b"age: error: no identity matched any of the recipients\n"
                  b"age: report unexpected or unhelpful errors at https://filippo.io/age/report\n")
        self.assertEqual(afbackup.last_line(stderr), "age: error: no identity matched any of the recipients")
        self.assertEqual(afbackup.last_line(b""), "no error output")
        self.assertEqual(afbackup.last_line(b"age: report unexpected or unhelpful errors at x\n"),
                         "age: report unexpected or unhelpful errors at x")

    def test_tools_resolved_only_on_fixed_path(self):
        evil = self.mkdir("evil-bin")
        canary = os.path.join(self.tmp, "evil-ran")
        script = os.path.join(evil, "age")
        with open(script, "w") as fh:
            fh.write(f"#!/bin/sh\ntouch {canary}\nexit 1\n")
        os.chmod(script, 0o755)
        os.environ["PATH"] = f"{evil}{os.pathsep}{os.environ['PATH']}"
        self.assertEqual(self.create(), 0, self.stderr)
        self.assertFalse(os.path.exists(canary))


# --- probe-destination ------------------------------------------------------------------------------------------

class ProbeDestinationTests(BackupTestCase):
    """probe-destination (8.4e): writes only its own probe directory and reports what the
    destination supports."""

    def probe(self):
        return self.run_tool("probe-destination", "--config", self.config)

    def lines(self, tag):
        return [line for line in self.stdout.splitlines() if line.startswith(tag)]

    def test_local_filesystem_passes_and_leaves_nothing_behind(self):
        archive = self.make_backup()
        before = sorted(os.listdir(self.dest))
        with open(archive, "rb") as fh:
            content = fh.read()
        self.assertEqual(self.probe(), 0, self.stdout + self.stderr)
        self.assertEqual(self.lines("FAIL"), [])
        for expected in ("O_CREAT|O_EXCL refuses", "read-back matches", "identity is stable",
                         "hard link refuses to replace", "O_NOFOLLOW refuses", "probe files and directory removed"):
            self.assertIn(expected, self.stdout)
        self.assertIn("RESULT  OK", self.stdout)
        self.assertEqual(sorted(os.listdir(self.dest)), before)
        with open(archive, "rb") as fh:
            self.assertEqual(fh.read(), content)
        self.assertEqual(self.calls(), [], "the probe must not run any external tool")

    def test_falls_back_to_rename_noreplace_without_hard_links(self):
        if not hasattr(afbackup.ctypes.CDLL(None), "renameat2"):
            self.skipTest("renameat2 not available in this libc")
        with mock.patch.object(afbackup.os, "link", side_effect=OSError(errno.EPERM, "Operation not permitted")):
            code = self.probe()
        if "renameat2(RENAME_NOREPLACE) is not supported" in self.stdout:
            self.skipTest("this filesystem does not support RENAME_NOREPLACE")
        self.assertEqual(code, 0, self.stdout)
        self.assertIn("renameat2(RENAME_NOREPLACE) refuses to replace", self.stdout)
        self.assertIn("create will publish archives by renameat2", self.stdout)
        self.assertEqual(os.listdir(self.dest), [afbackup.MARKER])

    def test_no_safe_publication_method_fails(self):
        with mock.patch.object(afbackup.os, "link", side_effect=OSError(errno.EPERM, "Operation not permitted")), \
                mock.patch.object(afbackup, "rename_noreplace", side_effect=OSError(errno.EINVAL, "Invalid argument")):
            self.assertEqual(self.probe(), 1)
        self.assertIn("create would refuse to publish", self.stdout)
        self.assertIn("RESULT  FAILED", self.stdout)
        self.assertEqual(os.listdir(self.dest), [afbackup.MARKER])

    def test_link_that_replaces_an_existing_name_fails(self):
        real_link = os.link

        def clobbering_link(src, dst, **kwargs):
            if os.path.lexists(dst):
                os.unlink(dst)
            return real_link(src, dst, **kwargs)
        with mock.patch.object(afbackup.os, "link", side_effect=clobbering_link):
            self.assertEqual(self.probe(), 1)
        self.assertIn("REPLACED an existing file", self.stdout)
        self.assertEqual(os.listdir(self.dest), [afbackup.MARKER])

    def test_corrupting_storage_fails(self):
        real_open = afbackup.open_regular_nofollow

        class Corrupting:
            def __init__(self, handle):
                self.handle = handle

            def fileno(self):
                return self.handle.fileno()

            def read(self, *args):
                data = bytearray(self.handle.read(*args))
                if data:
                    data[0] ^= 0xFF
                return bytes(data)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()
        with mock.patch.object(afbackup, "open_regular_nofollow", side_effect=lambda p: Corrupting(real_open(p))):
            self.assertEqual(self.probe(), 1, self.stdout)
        self.assertIn("read-back DIFFERS", self.stdout)
        self.assertIn("published probe file's content changed", self.stdout)
        self.assertEqual(os.listdir(self.dest), [afbackup.MARKER])

    def test_refused_without_marker_and_nothing_written(self):
        os.unlink(os.path.join(self.dest, afbackup.MARKER))
        self.assertEqual(self.probe(), 2)
        self.assertIn("marker", self.stderr)
        self.assertEqual(os.listdir(self.dest), [])

    def test_refused_when_destination_not_mounted(self):
        with mock.patch.object(afbackup, "is_mount_point", return_value=False):
            self.assertEqual(self.probe(), 2)
        self.assertIn("not a mount point", self.stderr)
        self.assertEqual(os.listdir(self.dest), [afbackup.MARKER])

    def test_refused_while_a_backup_holds_the_lock(self):
        with afbackup.exclusive_lock(self.lock_file):
            self.assertEqual(self.probe(), 2)
        self.assertIn("lock", self.stderr)
        self.assertEqual(os.listdir(self.dest), [afbackup.MARKER])

    def test_needs_only_the_destination_setting(self):
        self.config = self.write_config(BACKUP_RECIPIENTS_FILE=None, MEDIA_ROOT=None, DB_NAME=None)
        self.assertEqual(self.probe(), 0, self.stdout + self.stderr)

    def test_mount_option_warnings(self):
        notes = afbackup.mount_option_warnings("nfs", "rw,soft,vers=3,sec=sys")
        self.assertEqual(len(notes), 3)
        self.assertEqual(afbackup.mount_option_warnings("nfs4", "rw,hard,vers=4.2,sec=krb5p"), [])
        self.assertEqual(len(afbackup.mount_option_warnings("cifs", "rw,vers=2.1,hard")), 2)
        self.assertEqual(afbackup.mount_option_warnings("cifs", "rw,vers=3.1.1,seal,hard,serverino"), [])
        # what the Linux cifs client actually reported in the 8.4e loopback test (soft is its default)
        observed = "rw,vers=3.1.1,uid=0,forceuid,gid=0,forcegid,file_mode=0600,dir_mode=0700,seal,soft,serverino"
        notes = afbackup.mount_option_warnings("cifs", observed)
        self.assertEqual(len(notes), 2)
        self.assertTrue(any("soft" in n for n in notes) and any("forced" in n for n in notes))
        self.assertTrue(any("noserverino" in n for n in
                            afbackup.mount_option_warnings("cifs", "vers=3.1.1,seal,hard,noserverino")))
        self.assertEqual(len(afbackup.mount_option_warnings("fuse.sshfs", "rw")), 1)
        self.assertEqual(afbackup.mount_option_warnings("ext4", "rw,relatime"), [])

    def test_mount_details_parses_mountinfo(self):
        sample = ("36 35 98:0 / /mnt/applyforge\\040backups rw,noatime master:1 - nfs4 nas:/export/af "
                  "rw,vers=4.2,hard,sec=sys\n")
        with mock.patch("builtins.open", mock.mock_open(read_data=sample)):
            details = afbackup.mount_details("/mnt/applyforge backups")
        self.assertEqual(details, ("nfs4", "nas:/export/af", "rw,noatime", "rw,vers=4.2,hard,sec=sys"))


# --- Real age (skipped unless installed) ------------------------------------------------------------------------
# The decorator must stay directly on RealAgeTests. In the 8.4e commit a class was inserted between
# them, which skipped the probe-destination tests without age and crashed RealAgeTests (network
# rehearsal, 2026-11-21); test_real_age_skip_is_attached_to_the_right_class guards it.

@unittest.skipUnless(REAL_AGE and REAL_AGE_KEYGEN, "age/age-keygen not installed")
class RealAgeTests(BackupTestCase):
    """Round trip with the real age binary and a THROWAWAY identity created in the
    test's temporary directory (deleted with it); never a real backup key."""

    def setUp(self):
        super().setUp()
        os.unlink(os.path.join(self.bin, "age"))
        os.symlink(REAL_AGE, os.path.join(self.bin, "age"))
        self.identity = os.path.join(self.tmp, "offline", "throwaway-identity.txt")
        os.unlink(os.path.join(self.tmp, "offline", "identity.txt"))
        subprocess.run([REAL_AGE_KEYGEN, "-o", self.identity], check=True, capture_output=True)
        public = subprocess.run([REAL_AGE_KEYGEN, "-y", self.identity], check=True, capture_output=True, text=True)
        self.touch(self.recipients, public.stdout.encode())
        self.wrong_identity = os.path.join(self.tmp, "offline", "throwaway-wrong.txt")
        subprocess.run([REAL_AGE_KEYGEN, "-o", self.wrong_identity], check=True, capture_output=True)

    def test_real_age_round_trip_and_rejections(self):
        archive = self.make_backup()
        self.assertEqual(self.index_lines()[0][2], sha256_path(archive))
        self.assertEqual(self.verify(archive, expected=sha256_path(archive)), 0, self.stderr)
        self.assertEqual(self.verify(archive, identity=self.wrong_identity), 1)
        with open(archive, "rb") as fh:
            data = bytearray(fh.read())
        data[-10] ^= 0x01
        damaged = self.touch(os.path.join(self.tmp, "damaged.tar.age"), bytes(data))
        self.assertEqual(self.verify(damaged), 1)
        self.assert_no_leftovers()


class SuiteStructureTests(unittest.TestCase):
    """Regression (network rehearsal 2026-11-21): the 'age installed' skip belongs to RealAgeTests only."""

    def test_real_age_skip_is_attached_to_the_right_class(self):
        self.assertNotIn("age", getattr(ProbeDestinationTests, "__unittest_skip_why__", "") or "",
                         "probe-destination tests must not depend on age being installed")
        if not (REAL_AGE and REAL_AGE_KEYGEN):
            self.assertTrue(getattr(RealAgeTests, "__unittest_skip__", False))
            self.assertIn("age", RealAgeTests.__unittest_skip_why__)


if __name__ == "__main__":
    unittest.main()
