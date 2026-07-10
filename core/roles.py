"""Role definitions and permission helpers.

Three roles, modelled as Django Groups so they map onto the admin and the
standard permission framework:

  TREASURER  - full access, including expense approval and remittance.
  ASSISTANT  - data entry, allocation, queue work, expense entry. No approval/remit.
  AUDITOR    - read-only: reports and the audit log.
"""

TREASURER = "Treasurer"
ASSISTANT = "Assistant"
AUDITOR = "Auditor"
LEADER = "Leader"   # departmental leader: read-only, scoped to own department(s)
ELDER = "Elder"     # church elder: read-only, scoped to a small curated set of
                    # board-level pages (its own dashboard, the executive
                    # overview) — deliberately not a staff role; broader access
                    # such as reports is opt-in via an assignable right, not
                    # granted to every elder by default.

ALL_ROLES = [TREASURER, ASSISTANT, AUDITOR, LEADER, ELDER]


def user_roles(user):
    if not user.is_authenticated:
        return set()
    if user.is_superuser:
        return set([TREASURER, ASSISTANT, AUDITOR])  # admin is staff, not a leader
    return set(user.groups.values_list("name", flat=True))


def is_treasurer(user):
    return user.is_superuser or TREASURER in user_roles(user)


def is_assistant(user):
    return user.is_superuser or ASSISTANT in user_roles(user)


def is_auditor(user):
    return AUDITOR in user_roles(user)


def is_leader(user):
    """A departmental leader (read-only, scoped to their own departments)."""
    return user.is_authenticated and not user.is_superuser \
        and LEADER in user_roles(user)


def is_elder(user):
    """A church elder: read-only, scoped to a small curated set of board-level
    pages. Deliberately not a staff role (see is_staff_role)."""
    return user.is_authenticated and not user.is_superuser \
        and ELDER in user_roles(user)


def is_staff_role(user):
    """Treasurer/Assistant/Auditor/admin — the church office roles that may use
    the full application. Explicitly excludes a pure departmental Leader."""
    if user.is_superuser:
        return True
    r = user_roles(user)
    return bool(r & {TREASURER, ASSISTANT, AUDITOR})


def can_enter_data(user):
    """Treasurers and assistants can create/allocate entries."""
    return is_treasurer(user) or is_assistant(user)


def can_approve(user):
    """Only treasurers approve expenses and post remittances."""
    return is_treasurer(user)


def can_allocate_dev_offering(user):
    """Treasurers, and anyone granted the development-offering right, may view and
    allocate unassigned development offering."""
    return is_treasurer(user) or has_right(user, RIGHT_DEV_OFFERING)


def can_manage_advances(user):
    """Treasurers and assistants manage advances by default; the right can also be
    granted to any other user (e.g. a leader who runs their own advances)."""
    return is_treasurer(user) or is_assistant(user) or has_right(user, RIGHT_ADVANCES)


def can_build_dev_groups(user):
    """Treasurers, and anyone granted the dev-group-builder right, may generate
    balanced development groups."""
    return is_treasurer(user) or has_right(user, RIGHT_DEV_GROUP_BUILDER)


def can_allocate_dev_offering(user):
    """Treasurers, and anyone granted the development-offering right, may view and
    allocate unassigned development offering."""
    from .rights import has_right
    return is_treasurer(user) or has_right(user, "allocate_dev_offering")


def can_manage_advances(user):
    """Treasurers and assistants manage advances by default; the right can also be
    granted to any other user (e.g. a leader who runs their own advances)."""
    from .rights import has_right
    return (is_treasurer(user) or is_assistant(user)
            or has_right(user, "manage_advances"))


def can_view_loans(user):
    """Treasurers, assistants and auditors see loans by default; grantable."""
    from .rights import has_right
    return (is_treasurer(user) or is_assistant(user) or is_auditor(user)
            or has_right(user, "view_loans"))


def can_manage_loans(user):
    """Treasurers and assistants record loan activity by default; grantable."""
    from .rights import has_right
    return (is_treasurer(user) or is_assistant(user)
            or has_right(user, "manage_loans"))


def can_view_liabilities(user):
    """Treasurers, assistants and auditors see the liability register by
    default; grantable to others (department leaders get a scoped view)."""
    from .rights import has_right
    return (is_treasurer(user) or is_assistant(user) or is_auditor(user)
            or has_right(user, "view_liabilities"))


def can_manage_liabilities(user):
    from .rights import has_right
    return (is_treasurer(user) or is_assistant(user)
            or has_right(user, "manage_liabilities"))


def can_view_payments(user):
    from .rights import has_right
    return (is_treasurer(user) or is_assistant(user) or is_auditor(user)
            or has_right(user, "view_payments"))


def can_manage_payments(user):
    from .rights import has_right
    return (is_treasurer(user) or is_assistant(user)
            or has_right(user, "manage_payments"))


def can_approve_payments(user):
    from .rights import has_right
    return is_treasurer(user) or has_right(user, "approve_payments")


def can_clear_payments(user):
    from .rights import has_right
    return (is_treasurer(user) or is_assistant(user)
            or has_right(user, "clear_payments"))


def can_void_payments(user):
    from .rights import has_right
    return is_treasurer(user) or has_right(user, "void_payments")


def can_convert_loans(user):
    """Retiring a loan (conversion / write-off) is a treasurer decision, like
    expense approval; grantable to others via the right."""
    from .rights import has_right
    return is_treasurer(user) or has_right(user, "convert_loans")


def can_view_fund_budget(user, dept):
    """View (read-only) a specific fund's budget & goals page. Treasurers and
    assistants always can, for any fund. A leader can too, but only for a fund
    they actually lead (or a sub-account of one), and only once granted the
    view_fund_budget right explicitly via a profile — it is not bundled into
    the base Leader role by default, so a treasurer opts leaders into it
    fund by fund rather than it being switched on for everyone at once.
    Editing/saving a budget is never covered by this — that stays
    treasurer/assistant only regardless of this right."""
    from .rights import has_right
    if is_treasurer(user) or is_assistant(user):
        return True
    if is_leader(user) and has_right(user, "view_fund_budget"):
        from departments.models import departments_led_by
        return departments_led_by(user).filter(pk=dept.pk).exists()
    return False


def can_build_dev_groups(user):
    """Treasurers, and anyone granted the dev-group-builder right, may generate
    balanced development groups."""
    from .rights import has_right
    return is_treasurer(user) or has_right(user, "build_dev_groups")


def can_allocate(user):
    """Resolve/allocate items in the giving review queue. Treasurers and
    assistants by default; also anyone granted the allocate_transactions right."""
    from .rights import has_right
    return (is_treasurer(user) or is_assistant(user)
            or has_right(user, "allocate_transactions"))


def can_classify_debits(user):
    """Classify bank-statement debits. Treasurers and assistants by default;
    also anyone granted the classify_debits right."""
    from .rights import has_right
    return (is_treasurer(user) or is_assistant(user)
            or has_right(user, "classify_debits"))


def role_label(user):
    """Human label for a user's primary role."""
    if user.is_superuser:
        return "Administrator"
    roles = user_roles(user)
    for r in (TREASURER, ASSISTANT, AUDITOR, LEADER):
        if r in roles:
            return "Department leader" if r == LEADER else r
    return "—"
