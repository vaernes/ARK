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
from ark_ingest import Checkpoint, Document, Log, run as ark_ingest_run  # noqa: E402
from llm_backend import build_llm_headers, resolve_llm_backend_config  # noqa: E402
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

        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"**Version:** `{version}`", readme)

        dockerfile = (REPO_ROOT / "admin" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(f"ARG ARK_VERSION={version}", dockerfile)
        self.assertIn('org.opencontainers.image.licenses="Apache-2.0"', dockerfile)

        llm_dockerfile = (REPO_ROOT / "llm-admin" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(f"ARG ARK_VERSION={version}", llm_dockerfile)

        llm_app = (REPO_ROOT / "llm-admin" / "app.py").read_text(encoding="utf-8")
        self.assertIn(f'ARK_VERSION = os.environ.get("ARK_VERSION", "{version}")', llm_app)

        compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn(f"${{ARK_VERSION:-{version}}}", compose)

        compose_llm = (REPO_ROOT / "docker-compose.llm.yml").read_text(encoding="utf-8")
        self.assertIn(f"${{ARK_VERSION:-{version}}}", compose_llm)

        env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn(f"ARK_VERSION={version}", env_example)

        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{version}]", changelog)

        license_text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Apache License", license_text)
        self.assertIn("Version 2.0", license_text)
        self.assertTrue((REPO_ROOT / "NOTICE").is_file())

    def test_bump_version_script_updates_readme_and_all_targets(self) -> None:
        import shutil
        import subprocess

        rel_files = (
            "VERSION",
            "README.md",
            "CHANGELOG.md",
            "admin/version.py",
            "admin/Dockerfile",
            "llm-admin/Dockerfile",
            "llm-admin/app.py",
            "docker-compose.yml",
            "docker-compose.llm.yml",
            ".env.example",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_root = Path(tmpdir)
            for rel in rel_files:
                dst = tmp_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO_ROOT / rel, dst)
            script_dst = tmp_root / "scripts" / "bump-version.sh"
            script_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / "scripts" / "bump-version.sh", script_dst)

            proc = subprocess.run(
                ["bash", str(script_dst), "--update-changelog", "2027.03.1"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual((tmp_root / "VERSION").read_text(encoding="utf-8").strip(), "2027.03.1")
            self.assertIn("**Version:** `2027.03.1`", (tmp_root / "README.md").read_text(encoding="utf-8"))
            self.assertIn("## [2027.03.1]", (tmp_root / "CHANGELOG.md").read_text(encoding="utf-8"))
            self.assertIn("v2027.03.1...HEAD", (tmp_root / "CHANGELOG.md").read_text(encoding="utf-8"))
            self.assertIn('DEFAULT_VERSION = "2027.03.1"', (tmp_root / "admin/version.py").read_text(encoding="utf-8"))
            self.assertIn("ARG ARK_VERSION=2027.03.1", (tmp_root / "admin/Dockerfile").read_text(encoding="utf-8"))
            self.assertIn("ARG ARK_VERSION=2027.03.1", (tmp_root / "llm-admin/Dockerfile").read_text(encoding="utf-8"))
            self.assertIn(
                'ARK_VERSION = os.environ.get("ARK_VERSION", "2027.03.1")',
                (tmp_root / "llm-admin/app.py").read_text(encoding="utf-8"),
            )
            self.assertIn("${ARK_VERSION:-2027.03.1}", (tmp_root / "docker-compose.yml").read_text(encoding="utf-8"))
            self.assertIn("${ARK_VERSION:-2027.03.1}", (tmp_root / "docker-compose.llm.yml").read_text(encoding="utf-8"))
            self.assertIn("ARK_VERSION=2027.03.1", (tmp_root / ".env.example").read_text(encoding="utf-8"))

            proc_auto = subprocess.run(
                ["bash", str(script_dst), "--update-changelog", "--bump", "patch"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_auto.returncode, 0, proc_auto.stderr)
            self.assertEqual((tmp_root / "VERSION").read_text(encoding="utf-8").strip(), "2027.03.2")
            self.assertIn("**Version:** `2027.03.2`", (tmp_root / "README.md").read_text(encoding="utf-8"))

            changelog_path = tmp_root / "CHANGELOG.md"
            changelog_text = changelog_path.read_text(encoding="utf-8").replace(
                "## [Unreleased]\n",
                "## [Unreleased]\n\n### Added\n- New connector option.\n",
                1,
            )
            changelog_path.write_text(changelog_text, encoding="utf-8")
            proc_month = subprocess.run(
                ["bash", str(script_dst), "--update-changelog", "--bump", "calver-month"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc_month.returncode, 0, proc_month.stderr)
            self.assertEqual((tmp_root / "VERSION").read_text(encoding="utf-8").strip(), "2027.04.0")
            self.assertIn("**Version:** `2027.04.0`", (tmp_root / "README.md").read_text(encoding="utf-8"))
            updated_changelog = changelog_path.read_text(encoding="utf-8")
            self.assertIn("## [2027.04.0]", updated_changelog)
            self.assertIn("### Added\n- New connector option.", updated_changelog)

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


class TestLlmBackendConfig(unittest.TestCase):
    def test_explicit_external_openai_backend(self) -> None:
        cfg = resolve_llm_backend_config(
            {
                "LLM_BACKEND_TYPE": "third_party",
                "LLM_BACKEND_ADMIN_URL": "http://should-be-hidden:8095",
                "LLM_API_BASE_URL": "https://llm.corp.internal/v1/",
                "LLM_API_KEY": "sk-external-123",
            }
        )
        self.assertEqual(cfg["backend_type"], "third_party")
        self.assertEqual(cfg["backend_admin_url"], "")
        self.assertEqual(cfg["api_base_url"], "https://llm.corp.internal/v1")
        self.assertEqual(cfg["api_key"], "sk-external-123")
        self.assertEqual(cfg["health_url"], "https://llm.corp.internal/v1/models")
        self.assertEqual(
            build_llm_headers(cfg["api_key"], json_content=True),
            {"Content-Type": "application/json", "Authorization": "Bearer sk-external-123"},
        )

    def test_ark_llm_backend_includes_admin_url(self) -> None:
        cfg = resolve_llm_backend_config(
            {
                "LLM_BACKEND_TYPE": "ark_llm",
                "LLM_BACKEND_ADMIN_URL": "http://gpu-server.internal:8095/",
                "LLM_API_BASE_URL": "http://gpu-server.internal:8000/v1",
            }
        )
        self.assertEqual(cfg["backend_type"], "ark_llm")
        self.assertEqual(cfg["backend_admin_url"], "http://gpu-server.internal:8095")

    def test_legacy_vllm_url_fallback(self) -> None:
        cfg = resolve_llm_backend_config(
            {
                "VLLM_URL": "http://10.0.0.50:8000/",
                "VLLM_API_KEY": "sk-vllm-legacy",
            }
        )
        self.assertEqual(cfg["api_base_url"], "http://10.0.0.50:8000/v1")
        self.assertEqual(cfg["api_key"], "sk-vllm-legacy")
        self.assertEqual(cfg["health_url"], "http://10.0.0.50:8000/v1/models")

    def test_no_api_key_omits_authorization_header(self) -> None:
        self.assertEqual(build_llm_headers("", json_content=True), {"Content-Type": "application/json"})
        self.assertEqual(build_llm_headers(""), {})


class TestArkIngestSdkAndBuiltinPlugins(unittest.TestCase):
    def test_ark_ingest_sdk_emits_ndjson_ir(self) -> None:
        import io

        def sample_sync(ctx):
            yield Log(f"cursor={ctx.cursor.get('pos', 0)}")
            yield Document(doc_id="d1", filename="d1.md", content="hello", metadata={"k": "v"})
            yield Document(doc_id="d2", filename="d2.bin", content=b"\x00\x01")
            yield Checkpoint({"pos": 2})

        fake_in = io.StringIO(json.dumps({"config": {"a": 1}, "cursor": {"pos": 1}, "dry_run": True}))
        fake_out = io.StringIO()
        with mock.patch.object(sys, "stdin", fake_in), mock.patch.object(sys, "stdout", fake_out):
            ark_ingest_run(sample_sync)

        lines = [json.loads(line) for line in fake_out.getvalue().strip().splitlines()]
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0]["type"], "log")
        self.assertEqual(lines[1]["type"], "document")
        self.assertEqual(lines[1]["content_type"], "text")
        self.assertEqual(lines[2]["content_type"], "binary")
        self.assertEqual(lines[3], {"type": "checkpoint", "cursor": {"pos": 2}})

    def test_builtin_plugin_manifests_are_valid(self) -> None:
        plugins_root = REPO_ROOT / "admin" / "builtin_plugins"
        expected = {"smb_folder", "sharepoint", "mfiles", "http_json"}
        found = {p.name for p in plugins_root.iterdir() if p.is_dir()}
        self.assertTrue(expected.issubset(found), f"Missing builtin plugins: {expected - found}")
        for pid in expected:
            manifest = json.loads((plugins_root / pid / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["id"], pid)
            self.assertEqual(manifest["ark_protocol"], "1")
            self.assertTrue((plugins_root / pid / manifest["entrypoint"]).is_file())


if __name__ == "__main__":
    unittest.main()
