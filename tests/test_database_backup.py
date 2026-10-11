import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import backup_database as backup


class DatabaseBackupTests(unittest.TestCase):
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
