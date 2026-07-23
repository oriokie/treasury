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

MEMBER = "Member"   # a member of the congregation using the self-service
                    # portal. Deliberately NOT a staff role and not a
                    # read-only office role either: a Leader or an Auditor sees
                    # the church's figures, a Member sees only their own. Its
                    # access is object-level (scoped by benevolent.MemberAccount)
                    # rather than page-level, which is why it is confined to the
                    # portal by middleware rather than gated view by view.

ALL_ROLES = [TREASURER, ASSISTANT, AUDITOR, LEADER, ELDER, MEMBER]

# Roles whose holders belong to the church office in any capacity. A portal
# member is not one of them, and this tuple is what the confinement middleware
# and the navigation both ask.
OFFICE_ROLES = {TREASURER, ASSISTANT, AUDITOR, LEADER, ELDER}


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


def is_portal_member(user):
    """A congregation member using the self-service portal.

    Two conditions, both required. Holding the group alone is not enough: the
    portal shows a person their own money and their own family, so it needs the
    *binding* that says which person that is. An account that is suspended,
    closed or not yet activated fails here and the member is turned away —
    which is what makes suspending portal access take effect on the next click
    rather than the next login.

    Never true for a superuser or an office role. An administrator who also
    happens to be enrolled in a scheme uses the office application; giving them
    two identities in one session is how object-level scoping gets confused.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser or bool(user_roles(user) & OFFICE_ROLES):
        return False
    if MEMBER not in user_roles(user):
        return False
    account = member_account(user)
    return bool(account and account.is_usable)


def member_account(user):
    """The MemberAccount bound to this login, or None.

    The single place the binding is read. Everything object-level in the portal
    derives from what this returns, so there is exactly one function to audit
    and exactly one to get wrong.
    """
    if not getattr(user, "is_authenticated", False):
        return None
    try:
        return user.member_account
    except Exception:      # no account bound, or the relation is unavailable
        return None


def is_portal_only(user):
    """True for a login that has the Member role and no office role at all.

    Distinct from `is_portal_member`: this asks "should this login be confined
    to the portal", and answers yes even when the account is suspended — a
    suspended member must not fall *through* to the office application. The
    two questions are separated on purpose; conflating them is how a
    deactivated portal login would end up on the treasurer's dashboard.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    if user.is_superuser:
        return False
    r = user_roles(user)
    return MEMBER in r and not (r & OFFICE_ROLES)


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


def can_view_benevolent(user):
    """Treasurers, assistants and auditors see the schemes by default; grantable."""
    from .rights import has_right
    return (is_treasurer(user) or is_assistant(user) or is_auditor(user)
            or has_right(user, "view_benevolent"))


def can_manage_benevolent(user):
    """Enrol members, raise cases, record contributions — day-to-day scheme
    administration. Treasurers and assistants by default; grantable to (say) a
    welfare secretary who runs the scheme but never touches the bank.

    Kept exactly as it always was — the SUPERSET of the three role-specific
    checks below, so nothing that already checks `can_manage_benevolent` (or
    holds the `manage_benevolent` right) loses any capability. The three
    functions below are the NEW, finer split (Phase 9): a church that wants
    "this person only registers members, that person only handles cases" can
    now say so; a church happy with one broad administrator role keeps using
    this one, unchanged.
    """
    from .rights import has_right
    return (is_treasurer(user) or is_assistant(user)
            or has_right(user, "manage_benevolent"))


def can_register_benevolent_members(user):
    """Registration Officer: enrol, admit, transfer, reinstate members, manage
    households and exemptions. Does NOT, on its own, raise a case or record a
    contribution — see can_manage_benevolent_cases / can_manage_benevolent_
    finance for those. `can_manage_benevolent` (the old, broader right) still
    satisfies this, so an existing administrator keeps every capability they
    already had."""
    from .rights import has_right
    return can_manage_benevolent(user) or has_right(user, "benevolent_register_members")


def can_manage_benevolent_cases(user):
    """Case Officer: raise, submit and assess cases, attach documents, set a
    funding target. Deliberately does NOT include approving a case (see
    can_approve_benevolent) — raising a claim and authorising one are kept
    separate, the same segregation the rest of this module already insists
    on for money decisions."""
    from .rights import has_right
    return can_manage_benevolent(user) or has_right(user, "benevolent_manage_cases")


def can_manage_benevolent_finance(user):
    """Finance Officer: record contributions, resolve the intake queue, charge
    or waive dues, process refunds. The recording half of the module's money —
    approving a benefit (can_approve_benevolent) and paying it (the ordinary
    expense approval) both remain separate gates, exactly as before."""
    from .rights import has_right
    return can_manage_benevolent(user) or has_right(user, "benevolent_manage_finance")


def can_approve_benevolent(user):
    """Authorise a benefit. A money decision, so it sits with the treasurer by
    default — exactly like expense approval and loan conversion. Note this only
    authorises the CASE; the payment voucher it produces still has to clear the
    ordinary expense approval on its own."""
    from .rights import has_right
    return is_treasurer(user) or has_right(user, "approve_benevolent")


def can_vote_benevolent(user):
    """Sit on the benevolent committee. Deliberately its OWN right, not folded
    into the treasurer role: the whole point of a committee is that it is not one
    person, so a church must be able to grant a seat on it to an elder or a
    welfare secretary who has no other treasury access at all."""
    from .rights import has_right
    return has_right(user, "benevolent_committee")


def can_manage_benevolent_settings(user):
    """Change the module's settings, profiles and automation. Separate from
    scheme setup: a church may want its administrator tuning notifications and
    automation without also being able to publish a policy."""
    from .rights import has_right
    return is_treasurer(user) or has_right(user, "manage_benevolent_settings")


def can_manage_benevolent_schemes(user):
    """Create schemes and publish policies — the rule-making power, deliberately
    narrower than day-to-day administration."""
    from .rights import has_right
    return is_treasurer(user) or has_right(user, "manage_benevolent_schemes")


def is_benevolent_committee_chair(user, scheme=None):
    """Committee Chairperson — deliberately not a separate RIGHT. Holding the
    chair is a SEAT on a specific scheme's roster (benevolent.CommitteeMember,
    Phase 6), not a different permission from an ordinary committee member: a
    chair votes with the same `benevolent_committee` right everyone else on
    the committee needs, and what makes their vote matter more is
    `SchemePolicy.committee_requires_chair` (also Phase 6), not a bigger
    permission. This function exists so the UI (a badge, a dashboard section,
    a "you are the chair — the committee is waiting on your vote" notice) can
    ask the one question it actually needs answered, without a second rights
    concept duplicating what the committee roster already records.

    `scheme=None` asks "chair of ANY committee"; passed a scheme, asks
    specifically about that one.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    from benevolent.models import CommitteeMember
    qs = CommitteeMember.objects.filter(
        user=user, role=CommitteeMember.Role.CHAIR, active=True)
    if scheme is not None:
        qs = qs.filter(scheme=scheme)
    return qs.exists()


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
    for r in (TREASURER, ASSISTANT, AUDITOR, LEADER, MEMBER):
        if r in roles:
            if r == LEADER:
                return "Department leader"
            if r == MEMBER:
                return "Member"
            return r
    return "—"
