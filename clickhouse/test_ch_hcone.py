"""Gates the migrations image build. ch_hcone.py's output is jawn's init container log, so the ClickHouse
password must never reach it (nevermined-io/argocd#920). Its unattended --upgrade must fail closed: exit 1,
and stop at the first failure, on a failed migration, one applied but not recorded, or a ledger it cannot
read (nevermined-io/argocd#923). Run: python3 -m unittest clickhouse/test_ch_hcone.py
"""
import http.server
import os
import re
import subprocess
import sys
import threading
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "ch_hcone.py")
DOCKERFILE = os.path.join(HERE, "..", "docker", "dockerfiles", "dockerfile_migrations")


class PasswordNeverPrinted(unittest.TestCase):
    def run_unreachable(self, password, *extra_args, host="http://127.0.0.1:1"):
        """Run the upgrade against a closed port, so the connection-error branch prints its diagnostics."""
        env = {**os.environ, "CLICKHOUSE_HOST": host, "CLICKHOUSE_USER": "default"}
        env.pop("CLICKHOUSE_PASSWORD", None)
        if "--password" not in extra_args:
            env["CLICKHOUSE_PASSWORD"] = password
        result = subprocess.run(
            [sys.executable, SCRIPT, "--upgrade", "--skip-confirmation", *extra_args],
            env=env, capture_output=True, text=True, timeout=60,
        )
        output = result.stdout + result.stderr
        self.assertIn("Failed to connect to ClickHouse", output, "the diagnostics branch did not run")
        # The whole block must print, or the checks below see nothing: Command:, STDOUT: and STDERR:
        # come after the header.
        self.assertNotIn("Traceback", output)
        self.assertIn("Command:", output)
        self.assertIn("STDERR:", output)
        return output

    def assert_hidden(self, marker, output):
        self.assertNotIn(marker, output, "the password, or part of it, was printed")

    def test_env_password(self):
        marker = uuid.uuid4().hex
        output = self.run_unreachable(f"pw-{marker}", "--no-password")
        self.assert_hidden(marker, output)

    def test_argv_password_with_quotes_and_backslash(self):
        marker = uuid.uuid4().hex
        output = self.run_unreachable("x", "--password", f"pw-{marker}\\'\"")
        self.assert_hidden(marker, output)

    def test_argv_password_equals_form(self):
        marker = uuid.uuid4().hex
        output = self.run_unreachable("x", f"--password=pw-{marker}")
        self.assert_hidden(marker, output)

    def test_password_in_the_url(self):
        marker = uuid.uuid4().hex
        output = self.run_unreachable("x", "--no-password", host=f"http://default:pw-{marker}@127.0.0.1:1")
        self.assert_hidden(marker, output)

    def test_password_with_an_at_sign_in_the_url(self):
        marker = uuid.uuid4().hex
        output = self.run_unreachable("x", "--no-password", "--url", f"http://default:pw@{marker}@127.0.0.1:1")
        self.assert_hidden(marker, output)


class MaskUserinfo(unittest.TestCase):
    def test_masks_a_yarl_url(self):
        """get_url() returns a yarl URL for a URL-form host; the --start/--restart prints pass it as is."""
        sys.path.insert(0, HERE)
        import ch_hcone
        from yarl import URL

        self.assertEqual(
            ch_hcone.mask_userinfo(URL("http://default:pw@127.0.0.1:1")), "http://[REDACTED]@127.0.0.1:1"
        )


MIGRATIONS = sorted(
    (f for f in os.listdir(os.path.join(HERE, "migrations")) if re.match(r"schema_\d+", f)),
    key=lambda f: int(re.search(r"schema_(\d+)", f).group(1)),
)
ERROR = "Code: 15. DB::Exception: Cannot add index: index with this name already exists."
FULL_DISK = "Code: 243. DB::Exception: Cannot reserve 1.00 MiB, not enough space."


def fake_clickhouse(mode, pending):
    """A ClickHouse that answers the runner, records what it was sent, and fails as `mode` says:
    "ok", "migration_fails" (the first pending migration's statements), "record_fails" (every INSERT into
    helicone_migrations) or "select_fails" (the read of helicone_migrations)."""
    state = {"inserts": [], "migration_statements": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            sql = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            status, body = 200, ""
            if "FROM system.tables" in sql:
                body = "1"
            elif "SELECT migration_name FROM helicone_migrations" in sql:
                if mode == "select_fails":
                    status, body = 500, "Code: 241. DB::Exception: Memory limit exceeded. (MEMORY_LIMIT_EXCEEDED)"
                else:
                    body = "\n".join(n for n in MIGRATIONS if n not in pending)
            elif "INSERT INTO helicone_migrations" in sql:
                if mode == "record_fails":
                    status, body = 500, FULL_DISK
                else:
                    state["inserts"].append(re.search(r"'([^']+)'", sql).group(1))
            elif "CREATE TABLE IF NOT EXISTS helicone_migrations" in sql:
                pass
            else:
                state["migration_statements"] += 1
                first = open(os.path.join(HERE, "migrations", pending[0])).read() if pending else ""
                if mode == "migration_fails" and sql.strip().rstrip(";").split()[:3] == first.strip().split()[:3]:
                    status, body = 500, ERROR
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    return Handler, state


class UpgradeFailsClosed(unittest.TestCase):
    """--upgrade --skip-confirmation is jawn's init container (nevermined-io/argocd#923). A migration that
    fails, applies without being recorded (a full disk refused the INSERT), or a ledger it cannot read must
    stop jawn instead of letting it start on a partial schema, and must stop at the first failure."""

    def run_upgrade(self, mode, pending):
        handler, state = fake_clickhouse(mode, pending)
        server = http.server.HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = {**os.environ, "CLICKHOUSE_HOST": f"http://127.0.0.1:{server.server_port}",
                   "CLICKHOUSE_USER": "default", "CLICKHOUSE_PASSWORD": "x"}
            result = subprocess.run([sys.executable, SCRIPT, "--upgrade", "--no-password", "--skip-confirmation"],
                                    env=env, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        finally:
            server.shutdown()
            server.server_close()
        self.assertNotIn("Traceback", result.stderr)
        return result, state

    # The last two migrations: 78 is one statement, 79 is two.
    PENDING = MIGRATIONS[-2:]

    def test_clean_upgrade_records_each_migration(self):
        result, state = self.run_upgrade("ok", self.PENDING)
        self.assertEqual(result.returncode, 0, result.stdout[-500:])
        self.assertEqual(state["inserts"], self.PENDING)

    def test_nothing_pending_exits_zero(self):
        result, state = self.run_upgrade("ok", [])
        self.assertEqual(result.returncode, 0, result.stdout[-500:])
        self.assertIn("No migrations to apply", result.stdout)

    def test_failed_migration_is_not_recorded_and_stops_the_run(self):
        result, state = self.run_upgrade("migration_fails", self.PENDING)
        self.assertEqual(result.returncode, 1, result.stdout[-500:])
        self.assertIn(f"Failed to run migration: {self.PENDING[0]}", result.stdout)
        self.assertNotIn(self.PENDING[0], state["inserts"])
        self.assertNotIn(f"Running migration: {self.PENDING[1]}", result.stdout)

    def test_unrecorded_migration_stops_the_run_and_says_how_to_record_it(self):
        result, state = self.run_upgrade("record_fails", self.PENDING)
        self.assertEqual(result.returncode, 1, result.stdout[-500:])
        self.assertIn(f"Applied but not recorded in helicone_migrations: {self.PENDING[0]}", result.stdout)
        self.assertIn(f"INSERT INTO helicone_migrations (migration_name) VALUES ('{self.PENDING[0]}')", result.stdout)
        self.assertNotIn(f"Running migration: {self.PENDING[1]}", result.stdout)

    def test_unreadable_ledger_applies_nothing(self):
        result, state = self.run_upgrade("select_fails", self.PENDING)
        self.assertEqual(result.returncode, 1, result.stdout[-500:])
        self.assertIn("cannot read helicone_migrations", result.stdout)
        self.assertEqual(state["migration_statements"], 0)


class EntrypointKeepsPasswordOutOfArgv(unittest.TestCase):
    def test_no_password_flag_in_entrypoint(self):
        with open(DOCKERFILE) as f:
            dockerfile = f.read()
        self.assertIsNone(
            re.search(r"ch_hcone\.py[^\n]*--password\b", dockerfile),
            "the entrypoint passes the password in argv; ch_hcone.py reads CLICKHOUSE_PASSWORD from env",
        )


if __name__ == "__main__":
    unittest.main()
