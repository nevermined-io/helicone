"""The ClickHouse password must never reach ch_hcone.py's output: in the migrations image that output
is jawn's init container log (nevermined-io/argocd#920). Run: python3 -m unittest clickhouse/test_ch_hcone.py
"""
import os
import re
import subprocess
import sys
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
