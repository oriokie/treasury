"""Class-based-view mixins that gate access by role."""
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.contrib import messages
from django.shortcuts import redirect

from . import roles


class RoleRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Subclass and set `allow_check` to one of the role helpers."""

    allow_check = staticmethod(lambda u: u.is_authenticated)
    permission_message = "You don't have permission to do that."

    def test_func(self):
        return self.allow_check(self.request.user)

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            # a departmental leader is bounced to their own scoped dashboard,
            # never to a staff page they can't use
            if roles.is_leader(self.request.user):
                return redirect("leader_dashboard")
            messages.error(self.request, self.permission_message)
            return redirect("dashboard")
        return super().handle_no_permission()


class DataEntryRequiredMixin(RoleRequiredMixin):
    allow_check = staticmethod(roles.can_enter_data)
    permission_message = "Data entry requires Treasurer or Assistant role."


class TreasurerRequiredMixin(RoleRequiredMixin):
    allow_check = staticmethod(roles.can_approve)
    permission_message = "This action is restricted to Treasurers."


class AdvanceAccessMixin(RoleRequiredMixin):
    """Staff-advance management: treasurers/assistants, or any user granted the
    'manage staff advances' right."""
    allow_check = staticmethod(roles.can_manage_advances)
    permission_message = "You don't have the staff-advances right."


class LoanViewMixin(RoleRequiredMixin):
    """Read access to loans and lenders."""
    allow_check = staticmethod(roles.can_view_loans)
    permission_message = "You don't have the loans right."


class LoanManageMixin(RoleRequiredMixin):
    """Record loans, receipts, repayments; manage lenders and patterns."""
    allow_check = staticmethod(roles.can_manage_loans)
    permission_message = "You don't have the loans-management right."


class LoanConvertMixin(RoleRequiredMixin):
    """Convert to donation / write off — a treasurer decision."""
    allow_check = staticmethod(roles.can_convert_loans)
    permission_message = "Converting or writing off a loan is restricted to Treasurers."


class LiabilityViewMixin(RoleRequiredMixin):
    """Read access to the liability register. Department leaders are also
    admitted; the view itself scopes them to their own funds."""
    allow_check = staticmethod(
        lambda u: roles.can_view_liabilities(u) or roles.is_leader(u))
    permission_message = "You don't have the liability-transactions right."


class PaymentViewMixin(RoleRequiredMixin):
    """Read access to the payment register. Department leaders are admitted;
    the register self-scopes them to instruments on their own funds."""
    allow_check = staticmethod(
        lambda u: roles.can_view_payments(u) or roles.is_leader(u))
    permission_message = "You don't have the payment-register right."


class BenevolentViewMixin(RoleRequiredMixin):
    """Read access to schemes, memberships and cases."""
    allow_check = staticmethod(roles.can_view_benevolent)
    permission_message = "You don't have the benevolent-schemes right."


class BenevolentManageMixin(RoleRequiredMixin):
    """Day-to-day scheme administration: enrol members, raise cases, record
    contributions and payment vouchers. The broad, superset check — see the
    three role-specific mixins below (Phase 9) for views that only need ONE
    of these three responsibilities, not all of them."""
    allow_check = staticmethod(roles.can_manage_benevolent)
    permission_message = "You don't have the benevolent-administration right."


class BenevolentRegistrationMixin(RoleRequiredMixin):
    """Registration Officer: enrol, admit, transfer, reinstate members;
    manage households and exemptions. A holder of the broader
    `manage_benevolent` right (or a Treasurer/Assistant) satisfies this too —
    the split only NARROWS who else may reach these views, it never widens
    who could already reach them."""
    allow_check = staticmethod(roles.can_register_benevolent_members)
    permission_message = "You don't have the benevolent registration-officer right."


class BenevolentCaseMixin(RoleRequiredMixin):
    """Case Officer: raise, submit and assess cases, attach documents, set a
    funding target. Deliberately excludes approving a case — see
    BenevolentApproveMixin, a separate gate, exactly as it always was."""
    allow_check = staticmethod(roles.can_manage_benevolent_cases)
    permission_message = "You don't have the benevolent case-officer right."


class BenevolentFinanceMixin(RoleRequiredMixin):
    """Finance Officer: record contributions, resolve the intake queue,
    charge or waive dues, process refunds."""
    allow_check = staticmethod(roles.can_manage_benevolent_finance)
    permission_message = "You don't have the benevolent finance-officer right."


class BenevolentApproveMixin(RoleRequiredMixin):
    """Authorise (or refuse) a benefit — a money decision, treasurer by default."""
    allow_check = staticmethod(roles.can_approve_benevolent)
    permission_message = "Approving a benevolent case is restricted to Treasurers."


class BenevolentSetupMixin(RoleRequiredMixin):
    """Create schemes and publish policies — the rule-making power."""
    allow_check = staticmethod(roles.can_manage_benevolent_schemes)
    permission_message = "Setting up schemes and policies is restricted to Treasurers."


class BenevolentCommitteeMixin(RoleRequiredMixin):
    """Vote on a benevolent case. A treasurer is NOT admitted by default: sitting
    on the committee is its own right, because the committee's whole purpose is to
    be a body distinct from the treasurer who pays."""
    allow_check = staticmethod(roles.can_vote_benevolent)
    permission_message = "You are not on the benevolent committee."


class BenevolentSettingsMixin(RoleRequiredMixin):
    """The benevolent settings area."""
    allow_check = staticmethod(roles.can_manage_benevolent_settings)
    permission_message = "You don't have the benevolent-settings right."


class AllocateRequiredMixin(RoleRequiredMixin):
    """Allocate/resolve the giving review queue: treasurers/assistants, or any
    user granted the 'allocate transactions' right."""
    allow_check = staticmethod(roles.can_allocate)
    permission_message = "You don't have the allocation right."


class DebitClassifyRequiredMixin(RoleRequiredMixin):
    """Classify bank-statement debits: treasurers/assistants, or any user granted
    the 'classify debits' right."""
    allow_check = staticmethod(roles.can_classify_debits)
    permission_message = "You don't have the debit-classification right."


class ReadAccessMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Staff read access: Treasurer / Assistant / Auditor / admin.

    Departmental leaders are deliberately EXCLUDED — they are read-only but only
    over their own department, served by the dedicated, scoped leader views. This
    is the security linchpin: it stops a leader account reaching any of the full,
    unscoped office screens that use this mixin.
    """
    def test_func(self):
        return roles.is_staff_role(self.request.user)

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            if roles.is_leader(self.request.user):
                return redirect("leader_dashboard")
            if roles.is_elder(self.request.user):
                return redirect("elder_dashboard")
            messages.error(self.request, "You don't have permission to view that.")
            return redirect("dashboard")
        return super().handle_no_permission()


class ExecutiveAccessMixin(ReadAccessMixin):
    """The executive overview: staff roles (as ReadAccessMixin already allows),
    plus any user separately granted the view_executive_dashboard right — e.g.
    a church elder, for whom this is granted by default (see
    core.rights.GROUP_RIGHTS), without giving them the rest of the staff-only
    application that ReadAccessMixin alone would unlock."""
    def test_func(self):
        from .rights import has_right
        return super().test_func() or has_right(self.request.user,
                                                 "view_executive_dashboard")


class ReportAccessMixin(ReadAccessMixin):
    """Any report page: staff roles (as ReadAccessMixin already allows), plus
    any user separately granted the view_reports right — e.g. a church elder
    a treasurer has opted into report access. Not granted to elders by
    default (see core.rights.GROUP_RIGHTS); assignable via a profile."""
    def test_func(self):
        from .rights import has_right
        return super().test_func() or has_right(self.request.user, "view_reports")


class ElderRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """The elder's own dashboard: elders (and staff/admins, for setup and
    troubleshooting) — not leaders, not the general public."""
    def test_func(self):
        u = self.request.user
        return u.is_superuser or roles.is_elder(u) or roles.is_staff_role(u)

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            if roles.is_leader(self.request.user):
                return redirect("leader_dashboard")
            messages.error(self.request, "You don't have permission to view that.")
            return redirect("dashboard")
        return super().handle_no_permission()


class RightRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """Gate a view on a granular right (see core.rights). Set `required_right`."""
    required_right = None
    permission_message = "You don't have permission to do that."

    def test_func(self):
        from .rights import has_right
        return has_right(self.request.user, self.required_right)

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            from . import roles
            if roles.is_leader(self.request.user):
                return redirect("leader_dashboard")
            messages.error(self.request, self.permission_message)
            return redirect("dashboard")
        return super().handle_no_permission()


class PortalAccessMixin(LoginRequiredMixin, UserPassesTestMixin):
    """The gate on every self-service portal view.

    Passing it guarantees exactly one thing, and it is the thing the portal is
    built on: ``self.account`` is the ``MemberAccount`` of the person making
    this request. Views never re-derive that from ``request.user``, and never
    filter a queryset by anything else — the object-level rule lives in
    ``benevolent.services.portal.scope`` and takes this account as its
    argument, so there is a single implementation to audit.

    An office login fails this test on purpose. A treasurer who wants to see a
    member's portal view uses the staff screens, which show the same figures
    from the same services; letting one session hold both identities is how
    scoping mistakes get written.
    """
    permission_message = "That page is part of the member portal."

    def test_func(self):
        return roles.is_portal_member(self.request.user)

    @property
    def account(self):
        return roles.member_account(self.request.user)

    def dispatch(self, request, *args, **kwargs):
        response = super().dispatch(request, *args, **kwargs)
        account = self.account
        if account is not None:
            account.touch()
        return response

    def handle_no_permission(self):
        user = self.request.user
        if not user.is_authenticated:
            return super().handle_no_permission()
        # A login that IS a portal member but whose account is not usable —
        # suspended, closed, or invited but never activated. Say so plainly
        # rather than showing a generic refusal; the remedy differs in each
        # case and only the church office can apply it.
        account = roles.member_account(user)
        if account is not None and not account.is_usable:
            return redirect("portal_unavailable")
        messages.error(self.request, self.permission_message)
        return redirect("dashboard")


class PortalAdminMixin(RoleRequiredMixin):
    """The office side of the portal: inviting members, reviewing what they
    submit. Uses the module's existing broad administration right rather than
    inventing a new one — a church that has already decided who administers the
    scheme has already decided who handles its post.

    The individual *decisions* are gated more tightly than this, by the right
    that owns the change being made (registration, cases, finance). This mixin
    only opens the queue.
    """
    allow_check = staticmethod(roles.can_manage_benevolent)
    permission_message = "Managing member portal access requires scheme administration."
