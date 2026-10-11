import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import backup_database as backup


class DatabaseBackupTests(unittest.TestCase):
    def test_wrapped_s3_error_reports_code_without_private_details(self):
        error = RuntimeError('Failed to upload /private/path to private-bucket: An error occurred (AccessDenied) when calling PutObject: secret credentials')
        message = backup.s3_failure(error)
        self.assertTrue(message.startswith('S3_AccessDenied:'))
        self.assertNotIn('private', message)
        self.assertNotIn('secret', message)

    def test_structured_s3_error_and_unknown_error(self):
        error = RuntimeError('sensitive text')
        error.response = {'Error': {'Code': 'NoSuchBucket', 'Message': 'private bucket'}}
        self.assertTrue(backup.s3_failure(error).startswith('S3_NoSuchBucket:'))
        self.assertIsNone(backup.s3_failure(RuntimeError('An error occurred (SecretValue) private')))

    def test_database_diagnostics_never_echo_server_output(self):
        for source, code in [
            ('password authentication failed for user secret', 'PG_AUTH'),
            ('server does not support SSL, but SSL was required', 'PG_SSL'),
            ('could not translate host name secret', 'PG_DNS'),
            ('aborting because of server version mismatch secret', 'PG_VERSION'),
            ('unrecognized failure secret', 'PG_DUMP'),
        ]:
            message = backup.dump_failure(source.encode())
            self.assertTrue(message.startswith(code + ':'))
            self.assertNotIn('secret', message)

    @patch.object(backup.shutil, 'which', side_effect=lambda name: name)
    @patch.object(backup.subprocess, 'run', return_value=SimpleNamespace(
        returncode=1, stderr=b'password authentication failed for user secret'))
    def test_authentication_failure_is_actionable_and_does_not_upload(self, run, which):
        with self.assertRaisesRegex(backup.BackupError, 'PG_AUTH') as raised:
            backup.run_backup(self.s3, 'postgresql://user:secret@host/db', 'bucket')
        self.assertNotIn('secret', str(raised.exception))
        self.s3.upload_file.assert_not_called()

    def setUp(self):
        self.s3 = Mock()
        self.s3.get_object.return_value = {"Body": io.BytesIO(b"PGDMP-test-data")}

    def process(self, args, **kwargs):
        if "--file" in args:
            Path(args[args.index("--file") + 1]).write_bytes(b"PGDMP-test-data")
        return SimpleNamespace(returncode=0)

    @patch.object(backup.shutil, "which", side_effect=lambda name: name)
    @patch.object(backup.subprocess, "run")
    def test_verified_backup_keeps_credentials_out_of_arguments(self, run, which):
        run.side_effect = self.process
        url = "postgresql://user:secret@host/database"
        manifest = backup.run_backup(self.s3, url, "backup-bucket")
        self.assertFalse(manifest["restore_tested"])
        self.assertEqual(manifest["bytes"], 15)
        self.assertNotIn(url, str(run.call_args_list[0].args))
        env = run.call_args_list[0].kwargs['env']
        self.assertEqual(env['PGDATABASE'], 'database')
        self.assertEqual(env['PGHOST'], 'host')
        self.assertEqual(env['PGUSER'], 'user')
        self.assertEqual(env['PGPASSWORD'], 'secret')
        self.s3.put_object.assert_called_once()
        upload = self.s3.upload_file.call_args
        self.assertEqual(upload.kwargs["ExtraArgs"]["ServerSideEncryption"], "AES256")
        self.assertFalse(Path(upload.args[0]).exists())

    @patch.object(backup.shutil, "which", side_effect=lambda name: name)
    @patch.object(backup.subprocess, "run", return_value=SimpleNamespace(returncode=1))
    def test_failed_dump_never_uploads(self, run, which):
        with self.assertRaises(RuntimeError):
            backup.run_backup(self.s3, "postgresql://host/db", "backup-bucket")
        self.s3.upload_file.assert_not_called()

    @patch.object(backup.shutil, "which", side_effect=lambda name: name)
    @patch.object(backup.subprocess, "run")
    def test_corrupt_download_does_not_publish_manifest(self, run, which):
        run.side_effect = self.process
        self.s3.get_object.return_value = {"Body": io.BytesIO(b"corrupt")}
        with self.assertRaises(RuntimeError):
            backup.run_backup(self.s3, "postgresql://host/db", "backup-bucket")
        self.s3.put_object.assert_not_called()

    @patch.object(backup.shutil, "which", side_effect=lambda name: name)
    @patch.object(backup.subprocess, "run")
    def test_upload_failure_does_not_publish_manifest(self, run, which):
        run.side_effect = self.process
        self.s3.upload_file.side_effect = RuntimeError("upload failed")
        with self.assertRaises(RuntimeError):
            backup.run_backup(self.s3, "postgresql://host/db", "backup-bucket")
        self.s3.get_object.assert_not_called()
        self.s3.put_object.assert_not_called()

    @patch.dict(backup.os.environ, {'PGHOSTADDR': 'wrong-host', 'PGSERVICE': 'wrong-service', 'PGDATABASE': 'wrong-db'})
    def test_url_decoding_ipv6_and_ssl_are_preserved(self):
        env = backup.postgres_environment('postgresql://user%40name:p%40ss%3Aword@[::1]:5433/my%20db?sslmode=verify-full&connect_timeout=10')
        self.assertEqual(env['PGUSER'], 'user@name')
        self.assertEqual(env['PGPASSWORD'], 'p@ss:word')
        self.assertEqual(env['PGHOST'], '::1')
        self.assertEqual(env['PGPORT'], '5433')
        self.assertEqual(env['PGDATABASE'], 'my db')
        self.assertEqual(env['PGSSLMODE'], 'verify-full')
        self.assertEqual(env['PGCONNECT_TIMEOUT'], '10')
        self.assertNotIn('PGHOSTADDR', env)
        self.assertNotIn('PGSERVICE', env)

    def test_invalid_urls_fail_without_disclosing_input(self):
        for url in ['secret', 'https://user:secret@host/db', 'postgresql://host',
                    'postgresql://host:bad/db', 'postgresql://host/db?unknown=secret']:
            with self.subTest(url=url), self.assertRaises(backup.BackupError) as raised:
                backup.postgres_environment(url)
            self.assertNotIn('secret', str(raised.exception))


if __name__ == "__main__":
    unittest.main()
