"""Backup and full-data export.

* database_backup(): the raw SQLite file, for a complete restorable snapshot.
* full_excel_export(): every operational table in one multi-sheet workbook,
  for analysis or off-system archival. Both are admin/treasurer-only and stream
  to the browser; nothing is written to disk on the server.
"""
import datetime as _dt
from decimal import Decimal

from django.conf import settings
from django.http import FileResponse, HttpResponse


def _mysql_host(db):
    """The app (PyMySQL) connects over TCP even for 'localhost'; the command-line
    dump/restore tools treat 'localhost' as a Unix socket, which can match a
    different grant and be denied even though the app authenticates fine. Force
    the same TCP host the app uses so authentication is consistent."""
    host = db.get("HOST") or "127.0.0.1"
    return "127.0.0.1" if host == "localhost" else host


def _write_mysql_defaults_file(db):
    """Write a temporary my.cnf-style [client] file carrying the app's exact
    credentials over TCP. Passing them via --defaults-extra-file is the reliable,
    documented way to authenticate the dump tool and it overrides any stray
    ~/.my.cnf that could otherwise supply the wrong user/password. Caller deletes
    the file. Returns its path."""
    import os
    import tempfile
    pw = str(db.get("PASSWORD", "")).replace("\\", "\\\\").replace('"', '\\"')
    fd = tempfile.NamedTemporaryFile("w", suffix=".cnf", delete=False)
    fd.write("[client]\n")
    fd.write(f"host={_mysql_host(db)}\n")
    fd.write(f"port={db.get('PORT') or 3306}\n")
    fd.write(f"user={db['USER']}\n")
    fd.write(f'password="{pw}"\n')
    fd.write("protocol=TCP\n")
    fd.close()
    os.chmod(fd.name, 0o600)
    return fd.name


def _mysql_tool(*names):
    """Resolve a MySQL/MariaDB command-line tool, preferring the modern MariaDB
    name (so we don't trip the 'mysqldump: Deprecated program name' notice) and
    falling back to the classic name."""
    import shutil
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return names[-1]


def database_backup_bytes():
    """Produce the raw backup as (filename, bytes), independent of any HTTP
    response. Reused by the download view and the automated backup command.
    Returns (filename, data) on success or raises RuntimeError with a message."""
    import os
    import subprocess
    db = settings.DATABASES["default"]
    engine = db["ENGINE"]
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    if engine.endswith("sqlite3"):
        db_path = db["NAME"]
        if db_path is None or str(db_path).startswith("file:") \
                or not os.path.exists(db_path):
            raise RuntimeError("No on-disk database to back up.")
        with open(db_path, "rb") as fh:
            return f"treasury-backup-{stamp}.sqlite3", fh.read()

    defaults_file = None
    if engine.endswith("mysql"):
        defaults_file = _write_mysql_defaults_file(db)
        # --no-tablespaces avoids needing the PROCESS privilege (shared hosting
        # users rarely have it); we drop --routines for the same reason — a church
        # database has no stored procedures.
        cmd = [_mysql_tool("mariadb-dump", "mysqldump"),
               f"--defaults-extra-file={defaults_file}",
               "--single-transaction", "--no-tablespaces", db["NAME"]]
        env = dict(os.environ)
        ext = "sql"
    elif engine.endswith("postgresql"):
        cmd = ["pg_dump", "-h", db.get("HOST") or "localhost",
               "-p", str(db.get("PORT") or "5432"),
               "-U", db["USER"], db["NAME"]]
        env = dict(os.environ, PGPASSWORD=db.get("PASSWORD", ""))
        ext = "sql"
    else:
        raise RuntimeError("Backup isn't supported for this database engine.")

    try:
        proc = subprocess.run(cmd, capture_output=True, env=env, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"Could not run the database backup tool ({e}).")
    finally:
        if defaults_file:
            try:
                os.unlink(defaults_file)
            except OSError:
                pass
    if proc.returncode != 0:
        raise RuntimeError("The database backup tool reported an error: "
                           + (proc.stderr.decode("utf-8", "replace")[:300] or "unknown"))
    return f"treasury-backup-{stamp}.{ext}", proc.stdout


def database_backup_response():
    """Download a database backup, appropriate to the engine in use:

    * SQLite   → the raw .sqlite3 file (a complete, restorable snapshot).
    * MySQL    → a .sql dump via mysqldump.
    * Postgres → a .sql dump via pg_dump.

    For MySQL/Postgres the dump tool must be installed and on PATH (it is on a
    standard cPanel/WHM server). The Excel data export is always available as a
    fallback regardless of engine.
    """
    try:
        filename, data = database_backup_bytes()
    except RuntimeError as e:
        return HttpResponse(f"{e} The Excel data export is available as an "
                            f"alternative.", content_type="text/plain")
    if filename.endswith(".sqlite3"):
        import io
        return FileResponse(io.BytesIO(data), as_attachment=True, filename=filename)
    resp = HttpResponse(data, content_type="application/sql")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


def full_excel_export_response(year=None):
    """A complete, meaningful financial workbook as at the download date.

    Sheets:
      • Summary       — period, opening cash position, grand totals
      • Fund Balances — every fund: opening (B/F), receipts, operating expenses,
                        remittances, transfers, closing balance + grand totals
      • Trust Funds   — per trust fund: collected, remitted, still to remit
      • Income by Channel — bank / cash / envelope with counts
      • Cash Book     — every receipt and payment in date order with running balance
      • Members, Transactions, Expenses, Departments, Reconciliations — raw tables
    Figures cover the current year to the download date unless `year` is given.
    """
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from giving.models import Transaction
    from cashbook.models import Expense
    from members.models import Member
    from departments.models import Department
    from reports.services import balances
    from core.models import SiteConfig

    today = _dt.date.today()
    y = year or today.year
    start = _dt.date(y, 1, 1)
    end = today if today.year == y else _dt.date(y, 12, 31)
    cfg = SiteConfig.get()

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    HEAD_FILL = PatternFill("solid", fgColor="1F5F4F")
    HEAD_FONT = Font(bold=True, color="FFFFFF")
    TITLE_FONT = Font(bold=True, size=14, color="1F5F4F")
    TOTAL_FONT = Font(bold=True)
    money_cols_cache = {}

    def _sheet(name, header, rows, title=None, money_cols=(), total_row=None):
        ws = wb.create_sheet(name[:31])
        r = 1
        if title:
            ws.cell(row=r, column=1, value=title).font = TITLE_FONT
            r += 1
            ws.cell(row=r, column=1,
                    value=f"{cfg.church_name} · as at {today:%d %b %Y}").font = \
                Font(italic=True, size=9, color="666666")
            r += 2
        hr = r
        for ci, h in enumerate(header, 1):
            c = ws.cell(row=hr, column=ci, value=h)
            c.fill = HEAD_FILL
            c.font = HEAD_FONT
        r += 1
        for row in rows:
            for ci, val in enumerate(row, 1):
                ws.cell(row=r, column=ci, value=val)
            r += 1
        if total_row:
            for ci, val in enumerate(total_row, 1):
                c = ws.cell(row=r, column=ci, value=val)
                c.font = TOTAL_FONT
            r += 1
        # number format on money columns
        for ci in money_cols:
            for rr in range(hr + 1, r):
                cell = ws.cell(row=rr, column=ci)
                if isinstance(cell.value, (int, float)):
                    cell.number_format = "#,##0.00"
        # widths + frozen header
        for ci, h in enumerate(header, 1):
            width = max(len(str(h)) + 2,
                        *(len(str(ws.cell(row=rr, column=ci).value or "")) + 2
                          for rr in range(hr + 1, min(r, hr + 60))), 10)
            ws.column_dimensions[openpyxl.utils.get_column_letter(ci)].width = min(width, 48)
        ws.freeze_panes = ws.cell(row=hr + 1, column=1)
        return ws

    # ---- Fund Balances (the master accounting picture) --------------------
    rows = balances.department_summary(start, end)
    t = balances.totals(rows)
    fb = []
    for row in rows:
        d = row["department"]
        fb.append([
            d.name, "Trust" if row["is_trust"] else "Local",
            float(row["opening"]), float(row["receipts"]),
            float(row.get("expenses_operating", row["expenses"])),
            float(row.get("remittances", 0)),
            float(row.get("net_transfer", 0)), float(row["closing"]),
        ])
    fb.sort(key=lambda x: (x[1], x[0]))
    _sheet("Fund Balances",
           ["Fund", "Type", "Opening (B/F)", "Receipts", "Operating expenses",
            "Remittances", "Net transfers", "Closing balance"],
           fb,
           title=f"Fund balances — {start:%d %b %Y} to {end:%d %b %Y}",
           money_cols=(3, 4, 5, 6, 7, 8),
           total_row=["TOTAL", "", float(t["opening"]), float(t["receipts"]),
                      float(t["expenses_operating"]), float(t["remittances"]),
                      0.0, float(t["closing"])])

    # ---- Summary ----------------------------------------------------------
    opening_cash = (cfg.opening_bank_balance + cfg.opening_cash_on_hand
                    - cfg.opening_unremitted_trust)
    trust_rows = balances.trust_summary(start, end)
    to_remit = sum((tr["to_remit"] for tr in trust_rows), Decimal(0))
    _sheet("Summary",
           ["Item", "Amount"],
           [["Reporting period", f"{start:%d %b %Y} – {end:%d %b %Y}"],
            ["Opening bank balance", float(cfg.opening_bank_balance)],
            ["Opening cash on hand", float(cfg.opening_cash_on_hand)],
            ["Opening unremitted trust", float(cfg.opening_unremitted_trust)],
            ["Net opening position", float(opening_cash)],
            ["Total receipts (period)", float(t["receipts"])],
            ["Total operating expenses (period)", float(t["expenses_operating"])],
            ["Total trust remittances (period)", float(t["remittances"])],
            ["Trust still to remit", float(to_remit)],
            ["Closing fund balances (sum)", float(t["closing"])]],
           title="Financial summary", money_cols=(2,))

    # ---- Trust Funds ------------------------------------------------------
    _sheet("Trust Funds",
           ["Trust fund", "Collected", "Remitted", "Still to remit"],
           [[tr["department"].name, float(tr["collected"]),
             float(tr["remitted"]), float(tr["to_remit"])]
            for tr in trust_rows],
           title="Trust fund remittance schedule",
           money_cols=(2, 3, 4),
           total_row=["TOTAL",
                      float(sum((tr["collected"] for tr in trust_rows), Decimal(0))),
                      float(sum((tr["remitted"] for tr in trust_rows), Decimal(0))),
                      float(to_remit)])

    # ---- Income by Channel ------------------------------------------------
    chan = balances.income_by_channel(start, end)
    _sheet("Income by Channel",
           ["Channel", "Amount", "Count"],
           [[c["channel"], float(c["total"] or 0), c["count"]] for c in chan],
           title="Income by channel", money_cols=(2,))

    # ---- Cash Book (receipts & payments with running balance) -------------
    entries = []
    for tx in (Transaction.objects.confirmed_credits()
               .filter(date__gte=start, date__lte=end, excluded_from_income=False)
               .select_related("department", "member").order_by("date", "id")):
        entries.append((tx.date, tx.member.name if tx.member_id else (tx.payer_name or "Receipt"),
                        tx.department.name if tx.department_id else "",
                        float(tx.amount), 0.0))
    for x in (Expense.objects.filter(date__gte=start, date__lte=end,
              status__in=[Expense.Status.APPROVED, Expense.Status.PAID])
              .select_related("department").order_by("date", "id")):
        entries.append((x.date, x.description,
                        x.department.name if x.department_id else "",
                        0.0, float(x.amount)))
    entries.sort(key=lambda e: e[0])
    cashbook = []
    running = float(opening_cash)
    cashbook.append(["", "Opening position", "", 0.0, 0.0, running])
    for d, desc, fund, cr, dr in entries:
        running += cr - dr
        cashbook.append([d.isoformat(), desc, fund, cr, dr, running])
    _sheet("Cash Book",
           ["Date", "Description", "Fund", "Receipt", "Payment", "Balance"],
           cashbook, title="Cash book", money_cols=(4, 5, 6))

    # ---- Raw tables -------------------------------------------------------
    _sheet("Departments",
           ["ID", "Name", "Type", "Category", "Trust", "Opening balance"],
           [[d.id, d.name, d.fund_type, d.category, "Y" if d.is_trust else "",
             float(d.opening_balance or 0)]
            for d in Department.objects.all().order_by("name")],
           money_cols=(6,))

    _sheet("Members",
           ["ID", "Name", "Primary phone", "All phones", "Group", "Source", "Active"],
           [[m.id, m.name, m.receipt_phone or "",
             ", ".join(m.phones.values_list("number", flat=True)) or (m.phone or ""),
             m.group or "", m.get_source_display(), "Y" if m.active else ""]
            for m in Member.objects.all().order_by("name")])

    _sheet("Transactions",
           ["ID", "Date", "Channel", "Direction", "Amount", "Fund", "Dev group",
            "Member / payer", "Reference", "M-Pesa ref", "Status", "Confirmed",
            "Excluded from income"],
           [[t2.id, t2.date.isoformat(), t2.channel, t2.direction, float(t2.amount),
             t2.department.name if t2.department_id else "",
             t2.dev_group.number if t2.dev_group_id else "",
             t2.member.name if t2.member_id else (t2.payer_name or ""),
             t2.reference or "", t2.mpesa_ref or "", t2.allocation_status,
             "Y" if t2.confirmed else "", "Y" if t2.excluded_from_income else ""]
            for t2 in Transaction.objects.select_related(
                "department", "dev_group", "member").order_by("date")],
           money_cols=(5,))

    _sheet("Expenses",
           ["ID", "Date", "Fund", "Description", "Amount", "Category", "Method",
            "Status", "Claimant", "Voucher", "Recorded by"],
           [[x.id, x.date.isoformat(), x.department.name if x.department_id else "",
             x.description, float(x.amount), x.get_category_display(),
             x.method, x.get_status_display(), x.claimant or "",
             x.voucher_no or "", x.recorded_by.username if x.recorded_by_id else ""]
            for x in Expense.objects.select_related(
                "department", "recorded_by").order_by("date")],
           money_cols=(5,))

    try:
        from statements.models import BankReconciliation
        _sheet("Reconciliations",
               ["ID", "Statement date", "Bank balance", "Book balance", "Difference",
                "Reconciled"],
               [[r.id, r.statement_date.isoformat(), float(r.bank_balance),
                 float(r.book_balance or 0), float(r.difference or 0),
                 "Y" if r.is_reconciled else ""]
                for r in BankReconciliation.objects.order_by("statement_date")],
               money_cols=(3, 4, 5))
    except Exception:
        pass

    stamp = today.strftime("%Y%m%d")
    resp = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    resp["Content-Disposition"] = f'attachment; filename="treasury-data-{stamp}.xlsx"'
    wb.save(resp)
    return resp


def database_restore(uploaded_file):
    """Restore the database from an uploaded backup. Returns (ok, message).

    SAFETY: takes a fresh backup of the CURRENT database first (so a bad restore
    is itself reversible), then loads the uploaded file. SQLite restores by
    replacing the file; MySQL/Postgres restore by piping the SQL dump through the
    client. This is a destructive, treasurer-only operation guarded by an
    explicit confirmation in the view.
    """
    import os
    import subprocess
    import tempfile
    import datetime as _dt
    from django.conf import settings
    from django.db import connection

    db = settings.DATABASES["default"]
    engine = db["ENGINE"]
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    # read upload to a temp file
    suffix = ".sqlite3" if engine.endswith("sqlite3") else ".sql"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    try:
        for chunk in uploaded_file.chunks():
            tmp.write(chunk)
        tmp.close()

        if engine.endswith("sqlite3"):
            path = db["NAME"]
            if not isinstance(path, (str, bytes)) or not os.path.exists(path):
                return False, "No on-disk SQLite database to restore into."
            # safety copy of current db
            import shutil
            shutil.copy2(path, f"{path}.pre-restore-{stamp}")
            connection.close()
            shutil.copy2(tmp.name, path)
            return True, (f"Database restored from backup. The previous database "
                          f"was saved as {os.path.basename(path)}.pre-restore-{stamp}.")

        if engine.endswith("mysql"):
            # safety dump of current state
            safety = f"/tmp/treasury-pre-restore-{stamp}.sql"
            defaults_file = _write_mysql_defaults_file(db)
            env = dict(os.environ)
            try:
                with open(safety, "wb") as out:
                    subprocess.run([_mysql_tool("mariadb-dump", "mysqldump"),
                                    f"--defaults-extra-file={defaults_file}",
                                    "--single-transaction", "--no-tablespaces",
                                    db["NAME"]],
                                   stdout=out, env=env, timeout=300, check=True)
                # load the uploaded dump
                with open(tmp.name, "rb") as src:
                    proc = subprocess.run([_mysql_tool("mariadb", "mysql"),
                                           f"--defaults-extra-file={defaults_file}",
                                           db["NAME"]],
                                          stdin=src, env=env, timeout=600,
                                          capture_output=True)
            finally:
                try:
                    os.unlink(defaults_file)
                except OSError:
                    pass
            if proc.returncode != 0:
                return False, ("Restore failed while loading the backup. Your data "
                               "was not changed beyond the safety dump at "
                               f"{safety}. Detail: {proc.stderr.decode()[:300]}")
            return True, (f"Database restored from backup. A safety copy of the "
                          f"previous data was saved to {safety} on the server.")

        if engine.endswith("postgresql"):
            safety = f"/tmp/treasury-pre-restore-{stamp}.sql"
            env = dict(os.environ, PGPASSWORD=db.get("PASSWORD", ""))
            host = db.get("HOST") or "localhost"
            port = str(db.get("PORT") or "5432")
            with open(safety, "wb") as out:
                subprocess.run(["pg_dump", "-h", host, "-p", port, "-U", db["USER"],
                                db["NAME"]], stdout=out, env=env, timeout=300, check=True)
            with open(tmp.name, "rb") as src:
                proc = subprocess.run(["psql", "-h", host, "-p", port, "-U", db["USER"],
                                       db["NAME"]], stdin=src, env=env, timeout=600,
                                      capture_output=True)
            if proc.returncode != 0:
                return False, ("Restore failed while loading the backup. A safety "
                               f"copy was saved to {safety}.")
            return True, (f"Database restored. Safety copy saved to {safety}.")

        return False, "Restore isn't supported for this database engine."
    except subprocess.TimeoutExpired:
        return False, "Restore timed out."
    except Exception as e:  # noqa: BLE001
        return False, f"Restore failed: {e}"
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
