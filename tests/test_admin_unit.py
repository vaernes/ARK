"""Unit tests for ARK security boundaries, backup/restore utilities, and mock inference (stdlib only)."""

from __future__ import annotations

import gzip
import json
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "admin"))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from backup_utils import (  # noqa: E402
    dump_postgres_gzip,
    restore_postgres_gzip,
    sha256_file,
    validate_backup_dir,
    validate_backup_name,
    verify_backup_manifest,
)
from mock_inference import deterministic_embedding  # noqa: E402
from security import authorize_proxy_request, normalize_groups  # noqa: E402
from version import get_ark_version, get_version_info  # noqa: E402


class TestMockInference(unittest.TestCase):
    def test_deterministic_embedding_dimension_and_norm(self) -> None:
        vec1 = deterministic_embedding("ARK air-gapped test", dim=1024)
        vec2 = deterministic_embedding("ARK air-gapped test", dim=1024)
        vec3 = deterministic_embedding("Different text", dim=1024)
        self.assertEqual(len(vec1), 1024)
        self.assertEqual(vec1, vec2)
        self.assertNotEqual(vec1, vec3)


class TestProxyAuthorization(unittest.TestCase):
    SECRET = "super-secret-proxy-token"

    def test_fail_closed_when_secret_unconfigured(self) -> None:
        allowed, code, _ = authorize_proxy_request(
            "/admin-console/api/projects",
            {"X-Forwarded-User": "alice", "X-Forwarded-Groups": "ark-admins"},
            "",
        )
        self.assertFalse(allowed)
        self.assertEqual(code, 403)

    def test_rejects_spoofed_admin_headers_without_proxy_secret(self) -> None:
        for path in ("/admin-console/", "/admin-console/api/projects", "/admin-console/docs", "/portal/api/keys"):
            allowed, code, _ = authorize_proxy_request(
                path,
                {
                    "X-Forwarded-User": "attacker",
                    "X-Forwarded-Groups": "ark-admins",
                    "X-Ark-Proxy-Secret": "wrong-secret",
                },
                self.SECRET,
            )
            self.assertFalse(allowed, path)
            self.assertEqual(code, 403, path)

    def test_rejects_missing_identity(self) -> None:
        allowed, code, _ = authorize_proxy_request(
            "/admin-console/api/health",
            {"X-Ark-Proxy-Secret": self.SECRET, "X-Forwarded-Groups": "ark-admins"},
            self.SECRET,
        )
        self.assertFalse(allowed)
        self.assertEqual(code, 401)

    def test_rejects_non_admin_user_on_admin_console(self) -> None:
        allowed, code, _ = authorize_proxy_request(
            "/admin-console/api/projects",
            {
                "X-Ark-Proxy-Secret": self.SECRET,
                "X-Forwarded-Email": "dev@corp.internal",
                "X-Forwarded-Groups": "project-alpha, users",
            },
            self.SECRET,
        )
        self.assertFalse(allowed)
        self.assertEqual(code, 403)

    def test_allows_ark_admin_with_leading_slash_group(self) -> None:
        self.assertEqual(normalize_groups("/ark-admins, /project-alpha"), {"ark-admins", "project-alpha"})
        allowed, code, actor = authorize_proxy_request(
            "/admin-console/api/projects",
            {
                "x-ark-proxy-secret": self.SECRET,
                "x-forwarded-email": "admin@corp.internal",
                "x-forwarded-groups": "/ark-admins",
            },
            self.SECRET,
        )
        self.assertTrue(allowed)
        self.assertEqual(code, 200)
        self.assertEqual(actor, "admin@corp.internal")

    def test_allows_authenticated_non_admin_on_portal(self) -> None:
        allowed, code, actor = authorize_proxy_request(
            "/portal/api/keys",
            {
                "X-Ark-Proxy-Secret": self.SECRET,
                "X-Forwarded-User": "jdoe",
                "X-Forwarded-Groups": "project-alpha",
            },
            self.SECRET,
        )
        self.assertTrue(allowed)
        self.assertEqual(code, 200)
        self.assertEqual(actor, "jdoe")

    def test_project_gateway_bypasses_proxy_secret(self) -> None:
        allowed, code, _ = authorize_proxy_request("/projects/project-alpha/v1/chat/completions", {}, self.SECRET)
        self.assertTrue(allowed)
        self.assertEqual(code, 200)


class TestBackupUtils(unittest.TestCase):
    def test_validate_backup_dir(self) -> None:
        self.assertEqual(validate_backup_dir("/backups/nas"), Path("/backups/nas"))
        for bad in ("", "relative/path", "/backups/../etc", "/etc", "/app/static", "/", "/backups/a\nb", "/backups/a\x00b"):
            with self.assertRaises(ValueError, msg=f"Expected rejection for {bad!r}"):
                validate_backup_dir(bad)

    def test_validate_backup_name(self) -> None:
        self.assertEqual(validate_backup_name("ark-backup-20261113T000000Z"), "ark-backup-20261113T000000Z")
        for bad in ("", "..", "../etc", ".hidden", "a/b", "a b"):
            with self.assertRaises(ValueError, msg=f"Expected rejection for {bad!r}"):
                validate_backup_name(bad)

    def test_dump_and_restore_postgres_without_shell_and_manifest_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Directory containing shell metacharacters must not trigger command execution
            backup_dir = root / "dir;touch_pwned"
            backup_dir.mkdir()
            pwned_marker = root / "pwned"

            mock_pg_dump = root / "pg_dump"
            mock_pg_dump.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "db = sys.argv[-1]\n"
                "print(f'-- dump for {db}')\n"
                "print('SELECT 1;')\n",
                encoding="utf-8",
            )
            mock_pg_dump.chmod(mock_pg_dump.stat().st_mode | stat.S_IXUSR)

            out_gz = backup_dir / "ark_admin.sql.gz"
            db_entry = dump_postgres_gzip(
                db_name="ark_admin",
                out_file=out_gz,
                pg_host="postgres",
                pg_port=5432,
                pg_user="ark_webui",
                pg_password="pw",
                pg_dump_bin=str(mock_pg_dump),
            )
            self.assertFalse(pwned_marker.exists())
            self.assertTrue(out_gz.is_file())
            with gzip.open(out_gz, "rt", encoding="utf-8") as fh:
                self.assertIn("-- dump for ark_admin", fh.read())

            snap_dir = backup_dir / "qdrant" / "Project_Alpha"
            snap_dir.mkdir(parents=True)
            snap_file = snap_dir / "Project_Alpha-1.snapshot"
            snap_file.write_bytes(b"snapshot-binary-payload")

            manifest = {
                "created_at": "2026-11-13T00:00:00+00:00",
                "databases": [db_entry],
                "qdrant_snapshots": [
                    {
                        "collection": "Project_Alpha",
                        "name": snap_file.name,
                        "path": f"qdrant/Project_Alpha/{snap_file.name}",
                        "bytes": snap_file.stat().st_size,
                        "sha256": sha256_file(snap_file),
                    }
                ],
            }
            (backup_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            verified = verify_backup_manifest(backup_dir)
            self.assertEqual(len(verified["databases"]), 1)
            self.assertEqual(len(verified["qdrant_snapshots"]), 1)

            # Verify restore streams SQL into psql without shell
            captured_sql = root / "restored.sql"
            mock_psql = root / "psql"
            mock_psql.write_text(
                f"#!/usr/bin/env python3\n"
                f"import sys\n"
                f"open({str(captured_sql)!r}, 'wb').write(sys.stdin.buffer.read())\n",
                encoding="utf-8",
            )
            mock_psql.chmod(mock_psql.stat().st_mode | stat.S_IXUSR)

            restore_postgres_gzip(
                db_name="ark_admin",
                dump_file=out_gz,
                pg_host="postgres",
                pg_port=5432,
                pg_user="ark_webui",
                pg_password="pw",
                psql_bin=str(mock_psql),
            )
            self.assertIn("-- dump for ark_admin", captured_sql.read_text(encoding="utf-8"))

            # Corrupt snapshot -> verification must fail
            snap_file.write_bytes(b"tampered")
            with self.assertRaises(ValueError):
                verify_backup_manifest(backup_dir)

    def test_dump_failure_removes_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            failing_dump = root / "pg_dump"
            failing_dump.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "sys.stdout.write('partial')\n"
                "sys.stderr.write('connection refused\\n')\n"
                "sys.exit(2)\n",
                encoding="utf-8",
            )
            failing_dump.chmod(failing_dump.stat().st_mode | stat.S_IXUSR)
            out_gz = root / "ark_admin.sql.gz"
            with self.assertRaises(RuntimeError):
                dump_postgres_gzip("ark_admin", out_gz, "postgres", 5432, "u", "p", pg_dump_bin=str(failing_dump))
            self.assertFalse(out_gz.exists())


class TestUiXssSafety(unittest.TestCase):
    def test_no_innerhtml_interpolation_in_admin_or_portal_html(self) -> None:
        for rel_path in ("admin/static/index.html", "admin/static/portal.html"):
            content = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
            self.assertNotIn("innerHTML", content, f"{rel_path} must use safe DOM APIs instead of innerHTML")
            external_urls = re.findall(r'(?:src|href)=["\']https?://[^"\']+', content, re.I)
            self.assertEqual(external_urls, [], f"{rel_path} must not reference external URLs")


class TestVersionAndLicense(unittest.TestCase):
    def test_version_file_and_metadata_sync(self) -> None:
        version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d{4}\.(0[1-9]|1[0-2])\.\d+(-[0-9A-Za-z.-]+)?$")

        dockerfile = (REPO_ROOT / "admin" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(f"ARG ARK_VERSION={version}", dockerfile)
        self.assertIn('org.opencontainers.image.licenses="Apache-2.0"', dockerfile)

        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn(f"${{ARK_VERSION:-{version}}}", compose)

        env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn(f"ARK_VERSION={version}", env_example)

        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{version}]", changelog)

        license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Apache License", license_text)
        self.assertIn("Version 2.0", license_text)
        self.assertTrue((REPO_ROOT / "NOTICE").is_file())

    def test_get_version_info_and_env_override(self) -> None:
        expected = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARK_VERSION", None)
            self.assertEqual(get_ark_version(), expected)
            info = get_version_info()
            self.assertEqual(info["version"], expected)
            self.assertEqual(info["license"], "Apache-2.0")
            self.assertEqual(info["scheme"], "CalVer")
            self.assertTrue(info["air_gap_mode"])

        with mock.patch.dict(os.environ, {"ARK_VERSION": "2027.01.2", "ARK_GIT_COMMIT": "abc1234"}):
            info = get_version_info()
            self.assertEqual(info["version"], "2027.01.2")
            self.assertEqual(info["git_commit"], "abc1234")


if __name__ == "__main__":
    unittest.main()
