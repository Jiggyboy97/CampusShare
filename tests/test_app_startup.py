import os
import subprocess
import sys
import tempfile
import textwrap
import unittest


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DirectAppStartupTests(unittest.TestCase):
    def test_running_app_py_registers_routes_on_the_server_instance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env = os.environ.copy()
            env["FLASK_SECRET_KEY"] = "test-only-campusshare-startup-secret"
            env["CAMPUSSHARE_ENV"] = "development"
            env["CAMPUSSHARE_TRUSTED_PROXY_HOPS"] = "0"
            env["CAMPUSSHARE_TEST_DATABASE"] = os.path.join(
                temp_dir,
                "startup-test.db",
            )
            script = textwrap.dedent(
                """
                import os
                import runpy
                import sqlite3
                import database
                from flask import Flask

                database_path = os.environ["CAMPUSSHARE_TEST_DATABASE"]
                database.connect_database = lambda path, timeout=5: (
                    sqlite3.connect(database_path, timeout=timeout)
                )

                def inspect_routes(self, **kwargs):
                    print("SERVER_HOST=" + str(kwargs.get("host")))
                    print("SERVER_PORT=" + str(kwargs.get("port")))
                    print("SERVER_DEBUG=" + str(kwargs.get("debug")))
                    print("HOME_REGISTERED=" + str("home" in self.view_functions))
                    print("ROUTE_COUNT=" + str(len(self.url_map._rules)))
                    print("VERIFY_EMAIL_REGISTERED=" + str(
                        "verify_email" in self.view_functions
                    ))
                    print("RESEND_VERIFICATION_REGISTERED=" + str(
                        "resend_verification" in self.view_functions
                    ))

                Flask.run = inspect_routes
                runpy.run_path("app.py", run_name="__main__")
                """
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=PROJECT_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("HOME_REGISTERED=True", result.stdout)
        self.assertIn("VERIFY_EMAIL_REGISTERED=True", result.stdout)
        self.assertIn("RESEND_VERIFICATION_REGISTERED=True", result.stdout)
        self.assertIn("SERVER_HOST=127.0.0.1", result.stdout)
        self.assertIn("SERVER_PORT=5000", result.stdout)
        self.assertIn("SERVER_DEBUG=False", result.stdout)


if __name__ == "__main__":
    unittest.main()
