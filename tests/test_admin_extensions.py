import gzip
import importlib.util
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
ADMIN_DIR = ROOT_DIR / "admin"
LLM_ADMIN_DIR = ROOT_DIR / "llm-admin"

sys.path.insert(0, str(ADMIN_DIR))

import backup_utils  # noqa: E402
import security  # noqa: E402

try:
    import ingest  # noqa: E402
    HAS_INGEST_DEPS = True
except ImportError:
    ingest = None
    HAS_INGEST_DEPS = False

try:
    import pipelines  # noqa: E402
    HAS_PIPELINE_DEPS = True
except ImportError:
    pipelines = None
    HAS_PIPELINE_DEPS = False

try:
    import plugins  # noqa: E402
    HAS_PLUGIN_DEPS = True
except ImportError:
    plugins = None
    HAS_PLUGIN_DEPS = False


class TestSecurityCronAndMessages(unittest.TestCase):
    def test_parse_cron_expression_valid_and_invalid(self):
        parsed = security.parse_cron_expression("*/15 9-17 * * 1-5")
        self.assertIsNotNone(parsed)
        mins, hours, doms, months, dows = parsed
        self.assertEqual(mins, {0, 15, 30, 45})
        self.assertEqual(hours, set(range(9, 18)))
        self.assertEqual(doms, set(range(1, 32)))
        self.assertEqual(months, set(range(1, 13)))
        self.assertEqual(dows, {1, 2, 3, 4, 5})

        sunday = security.parse_cron_expression("0 2 * * 7")
        self.assertIsNotNone(sunday)
        self.assertEqual(sunday[4], {0})

        self.assertIsNone(security.parse_cron_expression("not a cron"))
        self.assertIsNone(security.parse_cron_expression("60 * * * *"))
        self.assertIsNone(security.parse_cron_expression("*/0 * * * *"))

    def test_schedule_interval_seconds(self):
        self.assertEqual(security.schedule_interval_seconds("@5m"), 300)
        self.assertEqual(security.schedule_interval_seconds("@15m"), 900)
        self.assertEqual(security.schedule_interval_seconds("hourly"), 3600)
        self.assertEqual(security.schedule_interval_seconds("every:2h"), 7200)
        self.assertEqual(security.schedule_interval_seconds("*/10 * * * *"), 600)
        self.assertIsNone(security.schedule_interval_seconds(""))
        self.assertIsNone(security.schedule_interval_seconds("bogus"))

    def test_is_schedule_due_interval_and_cron(self):
        now = datetime(2026, 11, 14, 14, 30, 20, tzinfo=timezone.utc)
        self.assertTrue(security.is_schedule_due("@15m", None, now=now))
        self.assertTrue(security.is_schedule_due("@15m", now - timedelta(minutes=16), now=now))
        self.assertFalse(security.is_schedule_due("@15m", now - timedelta(minutes=5), now=now))

        # Exact cron window at 14:30
        self.assertTrue(security.is_schedule_due("30 14 * * *", now - timedelta(hours=23), now=now))
        # Already ran at 14:30:05 during the same minute -> should not re-trigger
        self.assertFalse(
            security.is_schedule_due(
                "30 14 * * *",
                datetime(2026, 11, 14, 14, 30, 5, tzinfo=timezone.utc),
                now=now,
            )
        )
        # Wrong hour/minute with recent sync -> False
        self.assertFalse(security.is_schedule_due("0 3 * * *", now - timedelta(minutes=10), now=now))

    def test_extract_message_text_and_merge_system_context(self):
        self.assertEqual(security.extract_message_text("hello"), "hello")
        multimodal = [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            {"type": "text", "text": "Part one"},
            {"type": "input_text", "text": "Part two"},
        ]
        self.assertEqual(security.extract_message_text(multimodal), "Part one\nPart two")

        messages = [
            {"role": "system", "content": "Answer concisely."},
            {"role": "developer", "content": "Cite clauses."},
            {"role": "user", "content": "What is policy A?"},
        ]
        merged = security.merge_system_and_context_messages(
            messages,
            project_system_prompt="You are Project Alpha Assistant.",
            context_blocks=["[1] Clause A applies."],
        )
        self.assertEqual(merged[0]["role"], "system")
        sys_txt = merged[0]["content"]
        self.assertIn("You are Project Alpha Assistant.", sys_txt)
        self.assertIn("Client Instructions:\nAnswer concisely.\n\nCite clauses.", sys_txt)
        self.assertIn("Retrieved Context:\n[1] Clause A applies.", sys_txt)
        self.assertEqual(merged[1], {"role": "user", "content": "What is policy A?"})


class TestBackupManifestExtraArchives(unittest.TestCase):
    def test_verify_backup_manifest_validates_extra_archives(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_gz = root / "ark_admin.sql.gz"
            with gzip.open(db_gz, "wb") as fh:
                fh.write(b"-- sql dump\n")
            plugins_gz = root / "plugins.tar.gz"
            with gzip.open(plugins_gz, "wb") as fh:
                fh.write(b"fake plugin archive payload\n")

            manifest = {
                "ark_version": "test",
                "databases": [{"name": "ark_admin", "file": "ark_admin.sql.gz", "sha256": backup_utils.sha256_file(db_gz)}],
                "qdrant_snapshots": [],
                "extra_archives": {
                    "plugins": {"file": "plugins.tar.gz", "sha256": backup_utils.sha256_file(plugins_gz)},
                },
            }
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            loaded = backup_utils.verify_backup_manifest(root)
            self.assertIn("plugins", loaded["extra_archives"])

            # Tamper with extra archive -> SHA-256 verification must fail
            plugins_gz.write_bytes(b"corrupted bytes")
            with self.assertRaises(ValueError):
                backup_utils.verify_backup_manifest(root)

            # Missing extra archive -> FileNotFoundError
            plugins_gz.unlink()
            with self.assertRaises(FileNotFoundError):
                backup_utils.verify_backup_manifest(root)


class TestUiAndLlmAdminSecurity(unittest.TestCase):
    def test_all_ui_and_theme_assets_are_airgapped_and_xss_safe(self):
        html_assets = [
            ROOT_DIR / "admin" / "static" / "index.html",
            ROOT_DIR / "admin" / "static" / "portal.html",
            LLM_ADMIN_DIR / "static" / "index.html",
        ]
        for path in html_assets:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("innerHTML", text, msg=f"Unsafe innerHTML in {path}")
            self.assertNotIn("outerHTML", text, msg=f"Unsafe outerHTML in {path}")
            self.assertNotIn("insertAdjacentHTML", text, msg=f"Unsafe insertAdjacentHTML in {path}")
            self.assertFalse(
                re.findall(r'(?:src|href)=["\']https?://[^"\']+', text, re.I),
                msg=f"External URL found in {path}",
            )

        css_text = (ROOT_DIR / "config" / "open-webui" / "custom.css").read_text(encoding="utf-8")
        self.assertFalse(
            re.findall(r'url\(\s*["\']?https?://[^)]+', css_text, re.I),
            msg="External URL in Open WebUI custom.css",
        )

        admin_html = (ROOT_DIR / "admin" / "static" / "index.html").read_text(encoding="utf-8")
        portal_html = (ROOT_DIR / "admin" / "static" / "portal.html").read_text(encoding="utf-8")
        self.assertIn("/oauth2/sign_out", admin_html)
        self.assertIn("/oauth2/sign_out", portal_html)
        self.assertIn("key-scope-hint", portal_html)

    def test_llm_admin_protects_status_and_exposes_healthz(self):
        app_py = (LLM_ADMIN_DIR / "app.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/healthz")', app_py)
        self.assertRegex(
            app_py,
            r'@app\.get\("/api/status",\s*dependencies=\[Depends\(require_admin_token\)\]\)',
        )


@unittest.skipUnless(HAS_INGEST_DEPS, "admin ingest dependencies not installed in host python")
class TestIngestSecretsAndChunking(unittest.TestCase):
    def test_secret_value_roundtrip_and_legacy_plaintext(self):
        tok = ingest.encrypt_secret_value("s3cr3t-token", secret_seed="unit-test-seed")
        self.assertTrue(tok.startswith("enc:v1:"))
        self.assertNotIn("s3cr3t-token", tok)
        # Re-encrypting already encrypted token is idempotent
        self.assertEqual(ingest.encrypt_secret_value(tok, secret_seed="unit-test-seed"), tok)
        self.assertEqual(ingest.decrypt_secret_value(tok, secret_seed="unit-test-seed"), "s3cr3t-token")
        # Legacy plaintext continues to decode cleanly
        self.assertEqual(ingest.decrypt_secret_value("legacy-plain", secret_seed="unit-test-seed"), "legacy-plain")
        # Wrong key fails closed without raising
        self.assertEqual(ingest.decrypt_secret_value(tok, secret_seed="wrong-seed"), "")

    def test_split_config_and_secrets_and_chunkers(self):
        pub, sec = ingest.split_config_and_secrets(
            {"host": "intranet.local", "password": "********", "api_key": "new-key"},
            secret_fields=["password", "api_key"],
            existing_secrets={"password": "old-password"},
        )
        self.assertEqual(pub, {"host": "intranet.local"})
        self.assertEqual(sec, {"password": "old-password", "api_key": "new-key"})

        self.assertIn("doc_id", ingest.RESERVED_PAYLOAD_KEYS)
        md = "# Section One\nFirst paragraph.\n\n# Section Two\nSecond paragraph."
        md_chunks = ingest.chunk_markdown(md, chunk_size=30, overlap=5)
        self.assertGreaterEqual(len(md_chunks), 2)
        line_chunks = ingest.chunk_lines("\n".join(f"line-{i}" for i in range(30)), chunk_size=40, overlap=10)
        self.assertGreaterEqual(len(line_chunks), 2)


@unittest.skipUnless(HAS_PIPELINE_DEPS, "admin pipeline dependencies not installed in host python")
class TestPipelineConfinementAndReadOnlySql(unittest.TestCase):
    def test_validate_allowed_ingest_path_blocks_escapes_and_symlinks(self):
        with tempfile.TemporaryDirectory() as allowed_dir, tempfile.TemporaryDirectory() as outside_dir:
            prev_env = os.environ.get("ARK_ALLOWED_INGEST_PATHS")
            os.environ["ARK_ALLOWED_INGEST_PATHS"] = allowed_dir
            try:
                ok_file = Path(allowed_dir) / "notes.csv"
                ok_file.write_text("id,title\n1,Hello\n", encoding="utf-8")
                self.assertEqual(pipelines.validate_allowed_ingest_path(str(ok_file)), ok_file.resolve())

                for bad in ("/etc/passwd", "../etc/passwd", str(Path(outside_dir) / "secret.txt")):
                    with self.assertRaises(ValueError, msg=f"Should reject {bad}"):
                        pipelines.validate_allowed_ingest_path(bad)

                secret_target = Path(outside_dir) / "outside.txt"
                secret_target.write_text("confidential", encoding="utf-8")
                escape_link = Path(allowed_dir) / "escape.txt"
                escape_link.symlink_to(secret_target)
                with self.assertRaises(ValueError):
                    pipelines.validate_allowed_ingest_path(str(escape_link))
            finally:
                if prev_env is None:
                    os.environ.pop("ARK_ALLOWED_INGEST_PATHS", None)
                else:
                    os.environ["ARK_ALLOWED_INGEST_PATHS"] = prev_env

    def test_validate_readonly_sql_query_and_sqlite_dry_run(self):
        self.assertTrue(pipelines.validate_readonly_sql_query("SELECT id, name FROM docs").startswith("SELECT"))
        self.assertTrue(pipelines.validate_readonly_sql_query("WITH x AS (SELECT 1) SELECT * FROM x").startswith("WITH"))

        for dangerous in (
            "DELETE FROM docs",
            "SELECT * FROM docs; DROP TABLE docs",
            "/* comment */ INSERT INTO docs VALUES (1)",
            "SELECT pg_read_file('/etc/passwd')",
            "PRAGMA table_info(docs)",
        ):
            with self.assertRaises(ValueError, msg=f"Should reject SQL: {dangerous}"):
                pipelines.validate_readonly_sql_query(dangerous)

        with self.assertRaises(ValueError):
            pipelines._validate_sql_dsn("postgresql://ark:pw@postgres:5432/ark_admin")

        with tempfile.TemporaryDirectory() as allowed_dir:
            prev_env = os.environ.get("ARK_ALLOWED_INGEST_PATHS")
            os.environ["ARK_ALLOWED_INGEST_PATHS"] = allowed_dir
            try:
                db_path = Path(allowed_dir) / "docs.sqlite"
                conn = sqlite3.connect(db_path)
                conn.execute("CREATE TABLE items (id INT, title TEXT, body TEXT)")
                conn.execute("INSERT INTO items VALUES (1, 'Policy A', 'Airgap rules')")
                conn.commit()
                conn.close()

                spec_yaml = f"""
apiVersion: ark-ingest/v1
name: sqlite-ro-test
source:
  type: sql_query
  dsn: "sqlite:///{db_path}"
  query: "SELECT id, title, body FROM items"
transform:
  doc_id: "item/{{{{ item.id }}}}"
  filename: "{{{{ item.title | slugify }}}}.md"
  template: "{{{{ item.body }}}}"
"""
                preview = pipelines.dry_run_pipeline(spec_yaml, limit=5)
                self.assertEqual(preview["preview_count"], 1)
                self.assertEqual(preview["documents"][0]["doc_id"], "item/1")
                self.assertEqual(preview["documents"][0]["filename"], "policy-a.md")
                docs = pipelines.execute_pipeline(spec_yaml, limit=5)
                self.assertEqual(len(docs), 1)
                self.assertEqual(docs[0].doc_id, "item/1")
            finally:
                if prev_env is None:
                    os.environ.pop("ARK_ALLOWED_INGEST_PATHS", None)
                else:
                    os.environ["ARK_ALLOWED_INGEST_PATHS"] = prev_env


@unittest.skipUnless(HAS_PLUGIN_DEPS, "admin plugin dependencies not installed in host python")
class TestPluginDiscoveryAndEnvScrubbing(unittest.TestCase):
    def test_builtin_plugins_and_env_scrubbing(self):
        found = plugins.discover_plugins(extra_dir=Path("/nonexistent-plugins-dir"))
        self.assertIn("smb_folder", found)
        self.assertIn("mfiles", found)
        self.assertIn("sharepoint", found)

        orig = os.environ.copy()
        try:
            os.environ["ARK_ADMIN_DB_URL"] = "postgresql://secret"
            os.environ["QDRANT_API_KEY"] = "secret-qdrant"
            os.environ["ARK_PROXY_AUTH_SECRET"] = "secret-proxy"
            os.environ["ARK_ALLOWED_INGEST_PATHS"] = "/shares"
            scrubbed = plugins.build_scrubbed_env()
            self.assertNotIn("ARK_ADMIN_DB_URL", scrubbed)
            self.assertNotIn("QDRANT_API_KEY", scrubbed)
            self.assertNotIn("ARK_PROXY_AUTH_SECRET", scrubbed)
            self.assertEqual(scrubbed.get("ARK_ALLOWED_INGEST_PATHS"), "/shares")
        finally:
            os.environ.clear()
            os.environ.update(orig)


if __name__ == "__main__":
    unittest.main()
