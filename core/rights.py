"""Granular rights, layered on top of the existing role groups.

A right is a fine-grained capability (e.g. "approve expenses", "see member
phone numbers in full"). Rights are bundled into **profiles** (accounts.Profile)
that a treasurer can create freely and assign to users.

How a user's effective rights are resolved (`user_rights`):
  * superuser            -> every right.
  * has assigned profiles -> exactly the union of those profiles' rights. This
    lets a profile RESTRICT access (e.g. masked phone numbers) — the whole point
    of configurable profiles.
  * no profiles assigned  -> the rights implied by their existing role group
    (Treasurer/Assistant/Auditor/Leader). This is the backwards-compatible
    fallback, so every existing user keeps working exactly as before until a
    profile is deliberately assigned to them.

So nothing breaks for current users, and new profiles are fully configurable.
"""
from . import roles

# (key, label, group) — the catalogue shown on the profile editor.
RIGHTS = [
    # Data entry
    ("record_giving",        "Record giving / cash",               "Data entry"),
    ("count_envelopes",      "Count envelopes",                    "Data entry"),
    ("import_statements",    "Import bank statements",             "Data entry"),
    ("allocate_transactions","Allocate / resolve the review queue", "Data entry"),
    ("classify_debits",      "Classify bank-statement debits",      "Data entry"),
    ("record_expenses",      "Record expenses / claims",           "Data entry"),
    ("manage_members",       "Add / edit members",                 "Data entry"),
    ("manage_campaigns",     "Manage campaigns",                   "Data entry"),
    ("allocate_dev_offering","Allocate development offering",       "Data entry"),
    ("manage_advances",      "Manage staff advances",              "Data entry"),
    ("build_dev_groups",     "Build balanced development groups",   "Data entry"),
    # Money controls
    ("approve_expenses",     "Approve / reject expenses",          "Money controls"),
    ("second_approve",       "Second approval (high value)",       "Money controls"),
    ("mark_paid",            "Mark expenses paid",                 "Money controls"),
    ("reverse_transactions", "Reverse transactions",               "Money controls"),
    ("manage_remittance",    "Prepare / post remittances",         "Money controls"),
    ("manage_transfers",     "Make fund transfers",                "Money controls"),
    ("view_loans",           "View loans & lenders",               "Money controls"),
    ("manage_loans",         "Record loans / receipts / repayments", "Money controls"),
    ("convert_loans",        "Convert / write off loans",          "Money controls"),
    ("view_liabilities",     "View liability transactions",        "Money controls"),
    ("manage_liabilities",   "Record liability transactions",      "Money controls"),
    ("view_payments",        "View the payment register",          "Money controls"),
    ("manage_payments",      "Create / issue payment instruments", "Money controls"),
    ("approve_payments",     "Approve payment instruments",        "Money controls"),
    ("clear_payments",       "Mark payments cleared",              "Money controls"),
    ("void_payments",        "Void / cancel / reverse payments",   "Money controls"),
    ("view_benevolent",      "View benevolent schemes & cases",    "Money controls"),
    ("manage_benevolent",    "Benevolent: full day-to-day administration (superset of the "
                             "three roles below — kept for backward compatibility)", "Money controls"),
    ("benevolent_register_members", "Benevolent Registration Officer — enrol, admit, "
                             "transfer, reinstate members", "Money controls"),
    ("benevolent_manage_cases", "Benevolent Case Officer — raise, submit, assess cases, "
                             "manage documents", "Money controls"),
    ("benevolent_manage_finance", "Benevolent Finance Officer — record contributions, "
                             "fees, adjustments, refunds", "Money controls"),
    ("approve_benevolent",   "Benevolent Treasurer/Approver — authorise or refuse a case",
                             "Money controls"),
    ("benevolent_committee", "Benevolent Committee Member — sit on the committee and vote "
                             "on cases", "Money controls"),
    ("lock_periods",         "Lock / unlock periods",              "Money controls"),
    # Setup
    ("manage_funds",         "Manage funds & structure",           "Setup"),
    ("manage_budgets",       "Manage budgets",                     "Setup"),
    ("manage_rules",         "Manage allocation rules",            "Setup"),
    ("manage_assets",        "Manage assets",                      "Setup"),
    ("manage_vendors",       "Manage the supplier register",       "Setup"),
    # Its own right, not part of managing suppliers generally. Changing where a
    # supplier is paid is the single highest-risk edit in that module: the
    # commonest fraud against churches is a letter announcing new bank details.
    # A church that wants the office to maintain supplier records while only the
    # treasurer touches payment details can now express that.
    ("manage_vendor_bank_details", "Change supplier bank / M-Pesa payment details",
                             "Money controls"),
    ("manage_channels",      "Manage SMS / channels / settings",   "Setup"),
    ("manage_profiles",      "Manage profiles & users",            "Setup"),
    ("manage_benevolent_schemes", "Set up benevolent schemes & policies", "Setup"),
    ("manage_benevolent_settings", "Change benevolent settings, profiles & automation", "Setup"),
    # Reports
    ("view_reports",         "View reports",                       "Reports"),
    ("export_reports",       "Export reports (Excel / PDF)",       "Reports"),
    ("view_audit",           "View the audit log",                 "Reports"),
    ("download_backup",      "Download the full backup",           "Reports"),
    ("view_fund_budget",     "View a fund's budget & goals page",  "Reports"),
    ("view_executive_dashboard", "View the executive overview",    "Reports"),
    # Sensitive data
    ("view_member_phone_full", "See member phone numbers in full (otherwise masked)", "Sensitive data"),
    ("view_giver_identity",    "See giver identities (otherwise anonymised)",         "Sensitive data"),
    ("view_member_statements", "See individual member giving statements",             "Sensitive data"),
]

RIGHT_KEYS = [r[0] for r in RIGHTS]
RIGHT_LABELS = {r[0]: r[1] for r in RIGHTS}


def grouped_rights():
    """[(group, [(key, label), ...]), ...] preserving catalogue order."""
    out, seen = [], {}
    for key, label, group in RIGHTS:
        if group not in seen:
            seen[group] = []
            out.append((group, seen[group]))
        seen[group].append((key, label))
    return out


# Every sensitive/identity right is granted to existing staff groups so today's
# behaviour (full phone numbers, visible identities) is unchanged.
_ALL = set(RIGHT_KEYS)
_DATA_ENTRY = {"record_giving", "count_envelopes", "import_statements",
               "allocate_transactions", "classify_debits",
               "record_expenses", "manage_members", "manage_campaigns",
               "manage_rules", "allocate_dev_offering", "manage_advances",
               "view_loans", "manage_loans",
               "view_liabilities", "manage_liabilities",
               "view_payments", "manage_payments", "clear_payments",
               "view_benevolent", "manage_benevolent",
               # the three granular roles are folded into the same set as the
               # coarse manage_benevolent right, so a Treasurer/Assistant loses
               # nothing by this split — they still hold all three, exactly as
               # they held the one right that used to cover all three
               "benevolent_register_members", "benevolent_manage_cases",
               "benevolent_manage_finance"}
_SENSITIVE = {"view_member_phone_full", "view_giver_identity", "view_member_statements"}
_REPORTS = {"view_reports", "export_reports"}

GROUP_RIGHTS = {
    roles.TREASURER: set(_ALL),                                   # everything
    roles.ASSISTANT: _DATA_ENTRY | _REPORTS | _SENSITIVE,         # entry + sees identities
    roles.AUDITOR:   _REPORTS | {"view_audit", "download_backup", "view_loans",
                                 "view_liabilities", "view_payments",
                                 "view_benevolent"} | _SENSITIVE,
    roles.LEADER:    {"view_giver_identity"},     # names visible; phones masked unless a profile grants more.
                                                    # Deliberately NOT view_reports: a leader's own
                                                    # department views don't gate on it, and granting it
                                                    # would open every general report (via ReportAccessMixin)
                                                    # to every leader church-wide, not just their own fund.
    # An elder's own dashboard and the executive overview are granted by
    # default — that's the point of the role. Full "reports" access
    # (view_reports/export_reports) is deliberately NOT included here: it's
    # in the general RIGHTS catalogue so a treasurer can assign it to a
    # specific elder via a profile if wanted, but no elder gets it just by
    # being an elder.
    roles.ELDER:     {"view_executive_dashboard"},
}


def user_rights(user):
    """The set of right keys a user effectively holds."""
    if user is None or not getattr(user, "is_authenticated", False):
        return set()
    if user.is_superuser:
        return set(RIGHT_KEYS)
    # explicit profiles define access (and can restrict)
    try:
        profiles = list(user.profiles.all())
    except Exception:
        profiles = []
    if profiles:
        granted = set()
        for p in profiles:
            granted |= set(p.rights or [])
        return granted & set(RIGHT_KEYS)
    # fallback: rights implied by the user's role group(s)
    granted = set()
    for g in roles.user_roles(user):
        granted |= GROUP_RIGHTS.get(g, set())
    return granted


def has_right(user, key):
    if user is not None and getattr(user, "is_superuser", False):
        return True
    return key in user_rights(user)


def display_phone(user, phone):
    """Full number if the user may see it, otherwise masked."""
    from members.models import mask_phone
    if not phone:
        return ""
    return phone if has_right(user, "view_member_phone_full") else mask_phone(phone)


def display_giver(user, name):
    """Giver/member name if the user may see identities, else anonymised. Used
    anywhere a contributor's name is shown to a user whose profile withholds the
    'view_giver_identity' right (e.g. a department leader given totals but not
    identities)."""
    if not name:
        return ""
    return name if has_right(user, "view_giver_identity") else "Giver (hidden)"
