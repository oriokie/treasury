"""
Automated, encrypted database backup — for a nightly cron job.

Writes an encrypted snapshot to a directory off the web root, keeps the most
recent N copies (rotating older ones away), and optionally emails the backup to
an off-site address so a server failure never loses the books.

Usage (typical cron line, 02:30 nightly):
    30 2 * * *  cd /home/oriokie/apps/treasury && \
        .venv/bin/python manage.py backup_db --out ~/treasury_backups --keep 30 --email

Flags:
    --out DIR     where to write backups (default: BASE_DIR/../treasury_backups)
    --keep N      how many recent backups to retain (default 30)
    --email       also email the backup to SiteConfig.backup_email if set
    --no-encrypt  write the raw dump (NOT recommended; default is encrypted)
"""
import datetime as dt
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from core.services.backup import database_backup_bytes
from core.fields import encrypt


class Command(BaseCommand):
    help = "Write an encrypted, rotated database backup (for a cron job)."

    def add_arguments(self, parser):
        parser.add_argument("--out", default=None,
                            help="Directory to write backups to.")
        parser.add_argument("--keep", type=int, default=30,
                            help="Number of recent backups to retain.")
        parser.add_argument("--email", action="store_true",
                            help="Also email the backup off-site if configured.")
        parser.add_argument("--offsite", action="store_true",
                            help="Also upload the backup to off-site storage if configured.")
        parser.add_argument("--no-encrypt", action="store_true",
                            help="Write the raw dump instead of encrypting it.")

    def handle(self, *args, **opts):
        out_dir = Path(opts["out"]) if opts["out"] else (
            Path(settings.BASE_DIR).parent / "treasury_backups")
        out_dir.mkdir(parents=True, exist_ok=True)

        try:
            filename, data = database_backup_bytes()
        except RuntimeError as e:
            self.stderr.write(self.style.ERROR(f"Backup failed: {e}"))
            return

        encrypted = not opts["no_encrypt"]
        if encrypted:
            # encrypt the dump bytes with the app's Fernet key. We base64 the
            # bytes first so encrypt() (which takes text) round-trips cleanly.
            import base64
            token = encrypt(base64.b64encode(data).decode("ascii"))
            data_out = token.encode("ascii")
            filename = filename + ".enc"
        else:
            data_out = data

        path = out_dir / filename
        path.write_bytes(data_out)
        size_kb = len(data_out) / 1024
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {path} ({size_kb:,.0f} KB){' [encrypted]' if encrypted else ''}"))

        self._rotate(out_dir, opts["keep"])

        if opts["email"]:
            self._email(path, filename)

        from core.models import SiteConfig
        if opts["offsite"] or SiteConfig.get().offsite_backup_enabled:
            from core.services.backup import upload_offsite
            ok, detail = upload_offsite(filename, data_out)
            style = self.style.SUCCESS if ok else self.style.WARNING
            self.stdout.write(style(f"Off-site: {detail}"))

    def _rotate(self, out_dir, keep):
        backups = sorted(
            [p for p in out_dir.iterdir()
             if p.is_file() and p.name.startswith("treasury-backup-")],
            key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[keep:]:
            try:
                old.unlink()
                self.stdout.write(f"Rotated out old backup: {old.name}")
            except OSError:
                pass

    def _email(self, path, filename):
        from core.models import SiteConfig
        cfg = SiteConfig.get()
        to = getattr(cfg, "backup_email", "") or ""
        if not to:
            self.stdout.write(self.style.WARNING(
                "No backup email configured (Settings → set a backup email); "
                "skipping email."))
            return
        try:
            from django.core.mail import EmailMessage
            from core.services.email import _connection, is_configured
            stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            kwargs = {}
            if is_configured(cfg):
                kwargs["connection"] = _connection(cfg)
                kwargs["from_email"] = cfg.email_from
            msg = EmailMessage(
                subject=f"{cfg.church_name or 'Treasury'} backup — {stamp}",
                body=("Automated database backup attached. Keep this file safe; "
                      "it is encrypted with the application key and can only be "
                      "restored by this system."),
                to=[t.strip() for t in to.split(",") if t.strip()], **kwargs)
            msg.attach(filename, path.read_bytes(), "application/octet-stream")
            msg.send(fail_silently=False)
            self.stdout.write(self.style.SUCCESS(f"Backup emailed to {to}."))
        except Exception as e:  # email must never crash the backup itself
            self.stderr.write(self.style.WARNING(f"Could not email backup: {e}"))
