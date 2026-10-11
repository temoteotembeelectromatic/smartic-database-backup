import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import backup_database as backup


class DatabaseBackupTests(unittest.TestCase):
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
        self.assertEqual(run.call_args_list[0].kwargs["env"]["PGDATABASE"], url)
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


if __name__ == "__main__":
    unittest.main()
