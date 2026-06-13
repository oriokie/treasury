# Automated encrypted backups

The `backup_db` management command writes an **encrypted** database snapshot to a
directory off the web root, keeps the most recent N copies, and can email the
backup to an off-site address. Run it nightly from cron.

## What it does
- Dumps the database (mysqldump on the live server; the raw file on SQLite).
- Encrypts the dump with the app's Fernet key (`TREASURY_ENCRYPTION_KEY`, or the
  Django `SECRET_KEY` if that isn't set). The file is named `*.enc`.
- Rotates: keeps the newest `--keep` files, deletes older ones.
- Optionally emails the backup to `SiteConfig.backup_email` (Settings field).

## Important: keep the encryption key safe
The `.enc` backups can only be restored by a system that has the **same**
`TREASURY_ENCRYPTION_KEY` / `SECRET_KEY`. Record that key somewhere safe and
separate from the backups themselves. If you lose the key, the encrypted backups
cannot be opened. (Use `--no-encrypt` only if you are storing the dumps somewhere
already secure and accept that they are then plaintext.)

## Cron setup (cPanel → Cron Jobs, or crontab -e)
Nightly at 02:30, keep 30 days, email off-site:

    30 2 * * * cd /home/oriokie/apps/treasury && .venv/bin/python manage.py backup_db --out /home/oriokie/treasury_backups --keep 30 --email >> /home/oriokie/treasury_backups/backup.log 2>&1

Set the off-site address in the app: **Settings → backup email** (comma-separate
several). Email also requires the app's email settings to be configured.

## Restoring an encrypted backup
1. Decrypt to a usable dump (run on a machine with the same key):

       cd /home/oriokie/apps/treasury && .venv/bin/python -c "import base64,sys; from core.fields import decrypt; open(sys.argv[2],'wb').write(base64.b64decode(decrypt(open(sys.argv[1]).read())))" treasury-backup-YYYYMMDD-HHMMSS.sql.enc restored.sql

   (For a SQLite snapshot the output is the `.sqlite3` file directly.)
2. Restore the dump with the normal tool (`mysql < restored.sql`) **or** use the
   in-app restore at Settings → About → Restore for a guided import.

## Verify it's working
After adding the cron line, run it once by hand and confirm a `*.enc` file
appears in the backup directory and `backup.log` shows "Wrote …". Check again the
next morning that the scheduled run produced a fresh file.
