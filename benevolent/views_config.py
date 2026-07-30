"""Phase 2 views — the settings area, policy profiles, and the Constitution Wizard.

The settings area is deliberately its OWN page rather than a tab bolted onto the
church-wide settings screen. It inherits the application's theme, layout, tab
framework, form styling and permission model wholesale — a treasurer will not be
able to tell it was built separately — but it is reached by its own right, under
its own nav, so a welfare secretary can be given the module without also being
given the keys to the church's SMS gateway and bank feed.
"""
import datetime as dt
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views import View

from core.permissions import (BenevolentCommitteeMixin, BenevolentManageMixin,
                              BenevolentRegistrationMixin, BenevolentSettingsMixin,
                              BenevolentSetupMixin, BenevolentViewMixin)

from .forms import (ApplyProfileForm, FeeForm, MembershipEditForm, NomineeForm,
                    PolicyProfileForm, SaveAsProfileForm, SettingsForm, VoteForm)
from .models import (BenevolentCase, BenevolentScheme, BenevolentSettings,
                     PolicyProfile, SchemeMembership, SchemePolicy)
from .services import cases as case_svc
from .services import contributions as contrib_svc
from .services import bulk_sms
from .services import profiles as profile_svc
from .services import schemes as scheme_svc
from .services import wizard as wizard_svc


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class BenevolentSettingsView(BenevolentSettingsMixin, View):
    template_name = "benevolent/settings.html"

    def _context(self, request, form=None):
        cfg = BenevolentSettings.get()
        return {
            "form": form or SettingsForm(instance=cfg),
            "cfg": cfg,
            "profiles": PolicyProfile.objects.all(),
            "schemes": BenevolentScheme.objects.select_related("fund"),
            "profile_form": PolicyProfileForm(),
        }

    def get(self, request):
        return render(request, self.template_name, self._context(request))

    def post(self, request):
        cfg = BenevolentSettings.get()

        if "run_automation" in request.POST:
            # a dry run a treasurer can trigger by hand and SEE the result of,
            # before trusting a nightly job to do it unattended
            result = scheme_svc.run_automation(force=True)
            if result["changed"]:
                lines = "; ".join(
                    f"{c['membership'].member.name}: "
                    f"{c['from'].lower()} → {c['to'].lower()} ({c['reason']})"
                    for c in result["changes"][:6])
                messages.success(request, f"{result['summary']} {lines}")
            else:
                messages.info(request, "Automation ran: nothing needed changing.")
            return redirect(reverse("benevolent_settings") + "?tab=automation")

        form = SettingsForm(request.POST, instance=cfg)
        if form.is_valid():
            form.save()
            messages.success(request, "Benevolent settings saved.")
            tab = (request.POST.get("active_tab") or "").strip()
            url = reverse("benevolent_settings")
            return redirect(f"{url}?tab={tab}" if tab else url)
        messages.error(request, "Check the highlighted settings.")
        return render(request, self.template_name, self._context(request, form))


# ---------------------------------------------------------------------------
# Policy profiles
# ---------------------------------------------------------------------------

class ProfileListView(BenevolentSetupMixin, View):
    def get(self, request):
        return render(request, "benevolent/profile_list.html", {
            "profiles": PolicyProfile.objects.all(),
            "schemes": BenevolentScheme.objects.exclude(
                status=BenevolentScheme.Status.CLOSED),
        })


class ProfileDetailView(BenevolentSetupMixin, View):
    def get(self, request, pk):
        profile = get_object_or_404(PolicyProfile, pk=pk)
        return render(request, "benevolent/profile_detail.html", {
            "profile": profile,
            "apply_form": ApplyProfileForm(initial={"profile": profile}),
            "schemes": BenevolentScheme.objects.exclude(
                status=BenevolentScheme.Status.CLOSED),
            "rules": SchemePolicy.RULE_FIELDS,
        })

    def post(self, request, pk):
        profile = get_object_or_404(PolicyProfile, pk=pk)
        if request.POST.get("duplicate"):
            copy = profile_svc.duplicate(profile, user=request.user)
            messages.success(request, f"Copied to '{copy.name}'. Adjust it freely — the "
                                      f"original is untouched.")
            return redirect("benevolent_profile_detail", pk=copy.pk)
        if request.POST.get("delete"):
            try:
                name = profile.name
                profile.delete()
                messages.success(request, f"Profile '{name}' deleted.")
                return redirect("benevolent_profile_list")
            except ValidationError as e:
                messages.error(request, "; ".join(e.messages))
                return redirect("benevolent_profile_detail", pk=pk)
        # apply to a scheme
        form = ApplyProfileForm(request.POST)
        scheme = BenevolentScheme.objects.filter(pk=request.POST.get("scheme")).first()
        if not scheme or not form.is_valid():
            messages.error(request, "Choose a scheme and the date the rules take effect.")
            return redirect("benevolent_profile_detail", pk=pk)
        try:
            draft = profile_svc.apply_profile(
                profile, scheme, effective_from=form.cleaned_data["effective_from"],
                user=request.user)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("benevolent_profile_detail", pk=pk)
        messages.success(
            request, f"Draft policy v{draft.version} created for {scheme.name} from "
                     f"'{profile.name}'. Review it and publish when you're satisfied — "
                     f"nothing is in force until you do.")
        return redirect("benevolent_policy_edit", pk=scheme.pk, policy_id=draft.pk)


class ProfileSaveAsView(BenevolentSetupMixin, View):
    """Capture a working policy as a reusable profile."""

    def post(self, request, pk, policy_id):
        policy = get_object_or_404(SchemePolicy, pk=policy_id, scheme_id=pk)
        form = SaveAsProfileForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Give the profile a name.")
            return redirect("benevolent_policy_edit", pk=pk, policy_id=policy_id)
        try:
            p = profile_svc.save_as_profile(
                policy, name=form.cleaned_data["name"],
                description=form.cleaned_data.get("description") or "",
                user=request.user)
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("benevolent_policy_edit", pk=pk, policy_id=policy_id)
        messages.success(request, f"Saved as the profile '{p.name}'. Any scheme can now "
                                  f"start from these rules.")
        return redirect("benevolent_profile_detail", pk=p.pk)


# ---------------------------------------------------------------------------
# The Constitution & Policy Wizard
# ---------------------------------------------------------------------------

SESSION_KEY = "benevolent_wizard"


class WizardView(BenevolentSetupMixin, View):
    """A section at a time. Answers live in the session until the treasurer has
    seen the summary and the derivation and chosen to create the draft — the
    wizard writes nothing to the database until then."""

    def _answers(self, request):
        return request.session.get(SESSION_KEY, {})

    def get(self, request, step=0):
        answers = self._answers(request)
        sections = wizard_svc.SECTIONS
        step = max(0, min(int(step), len(sections)))

        if step >= len(sections):        # the review step
            cfg, lines, why = wizard_svc.build_config(answers)
            return render(request, "benevolent/wizard_review.html", {
                "answers": answers, "config": cfg, "lines": lines, "why": why,
                "summary": wizard_svc.summarise(answers),
                "schemes": BenevolentScheme.objects.exclude(
                    status=BenevolentScheme.Status.CLOSED),
                "sections": sections, "step": step,
                "today": dt.date.today(),
            })

        section = sections[step]
        return render(request, "benevolent/wizard.html", {
            "section": section, "step": step, "sections": sections,
            # Offered once the scheme's shape is settled — what it is for and
            # how it is funded. Everything after that has a defensible default,
            # and the summary lists every one of them with its reasoning, so
            # nothing is adopted silently. The skippable sections are not the
            # last ones, so "are the rest optional" is never true; the honest
            # offer is "take the defaults from here and let me read them".
            "can_skip_rest": step >= wizard_svc.SKIP_ALLOWED_FROM,
            "questions": wizard_svc.questions_for(section, answers),
            "answers": answers,
            "progress": int(100 * step / max(1, len(sections))),
        })

    def post(self, request, step=0):
        answers = dict(self._answers(request))
        step = int(step)
        sections = wizard_svc.SECTIONS

        if request.POST.get("restart"):
            request.session.pop(SESSION_KEY, None)
            return redirect("benevolent_wizard", step=0)

        if step < len(sections):
            # take every question in the section, not only those "visible" given
            # the answers we had BEFORE this page — the controlling answer for a
            # same-section dependency arrives in this very POST
            for q in wizard_svc.questions_for(sections[step], answers):
                if q.key in request.POST:
                    answers[q.key] = request.POST.get(q.key, "").strip()
            request.session[SESSION_KEY] = answers
            request.session.modified = True
            if request.POST.get("skip_rest"):
                # Take the defaults for everything still unanswered and go
                # straight to the summary. The answers already given are kept —
                # this fills the gaps, it does not overwrite the treasurer.
                answers = wizard_svc.fill_defaults(answers)
                request.session[SESSION_KEY] = answers
                request.session.modified = True
                messages.info(
                    request,
                    "The remaining sections have been set to their defaults. "
                    "They are listed on this summary with the reasoning, and "
                    "every one can be changed here or on the scheme's policy "
                    "afterwards.")
                return redirect("benevolent_wizard", step=len(sections))
            nxt = step - 1 if request.POST.get("back") else step + 1
            return redirect("benevolent_wizard", step=max(0, nxt))

        # ---- the review step: create the draft ---------------------------
        scheme = BenevolentScheme.objects.filter(pk=request.POST.get("scheme")).first()
        if not scheme:
            messages.error(request, "Choose the scheme these rules are for.")
            return redirect("benevolent_wizard", step=step)
        try:
            eff = dt.date.fromisoformat(request.POST.get("effective_from") or "")
        except ValueError:
            messages.error(request, "Give the date these rules take effect.")
            return redirect("benevolent_wizard", step=step)

        cfg, lines, _why = wizard_svc.build_config(answers)
        # the wizard's output is expressed as a profile and then APPLIED, so it
        # travels the exact same code path a hand-picked profile does — one route
        # into a policy, not two that could drift apart
        temp = PolicyProfile(name="(wizard)", config=cfg, benefit_lines=lines,
                             kind=answers.get("purpose", "BENEVOLENT"))
        draft = profile_svc.apply_profile(temp, scheme, effective_from=eff,
                                          user=request.user)
        draft.notes = ("Generated by the Constitution Wizard.\n\n"
                       + wizard_svc.summarise(answers))
        draft.save(update_fields=["notes"])

        if request.POST.get("save_profile"):
            name = (request.POST.get("profile_name") or "").strip()
            if name:
                try:
                    profile_svc.save_as_profile(draft, name=name, user=request.user)
                    messages.info(request, f"Also saved as the profile '{name}'.")
                except ValidationError as e:
                    messages.warning(request, "; ".join(e.messages))

        request.session.pop(SESSION_KEY, None)
        messages.success(
            request,
            f"Draft policy v{draft.version} created for {scheme.name}. Read it through — "
            f"the wizard has interpreted your constitution, and you are the one who knows "
            f"whether it got it right. Nothing is in force until you publish it.")
        return redirect("benevolent_policy_edit", pk=scheme.pk, policy_id=draft.pk)


# ---------------------------------------------------------------------------
# Committee voting
# ---------------------------------------------------------------------------

class CaseVoteView(BenevolentCommitteeMixin, View):
    """Record one committee member's decision on a case."""

    def post(self, request, pk):
        case = get_object_or_404(BenevolentCase, pk=pk)
        form = VoteForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Choose a decision.")
            return redirect("benevolent_case_detail", pk=pk)
        try:
            case_svc.record_vote(
                case, user=request.user,
                decision=form.cleaned_data["decision"],
                amount=form.cleaned_data.get("amount"),
                note=form.cleaned_data.get("note") or "")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
            return redirect("benevolent_case_detail", pk=pk)

        state = case_svc.committee_state(case)
        if state["carried"]:
            messages.success(
                request, f"Your decision is recorded. The committee now has "
                         f"{state['have']} of {state['quorum']} approvals — the quorum is "
                         f"reached and a treasurer can authorise the benefit.")
        else:
            messages.success(
                request, f"Your decision is recorded ({state['have']} of "
                         f"{state['quorum']} approvals).")
        return redirect("benevolent_case_detail", pk=pk)


# ---------------------------------------------------------------------------
# Registration, renewal, nominees
# ---------------------------------------------------------------------------

class MembershipAdminView(BenevolentRegistrationMixin, View):
    """The registration / renewal / nominee actions on one membership."""

    def post(self, request, pk, action):
        m = get_object_or_404(
            SchemeMembership.objects.select_related("scheme", "member"), pk=pk)
        # The "Admit" button lives in the same shared form as suspend/withdraw/
        # reinstate/close (see membership_detail.html's #lifeform) even though
        # this view handles it — so it should honour the same date/reason
        # fields those other actions do, rather than silently ignoring
        # whatever a treasurer typed into them.
        from .forms import LifecycleForm
        lf = LifecycleForm(request.POST)
        on = lf.cleaned_data["on"] if lf.is_valid() else None
        reason = lf.cleaned_data["reason"] if lf.is_valid() else ""
        try:
            if action == "admit":
                scheme_svc.admit(m, user=request.user, on=on, reason=reason)
                messages.success(
                    request, f"{m.member.name} admitted. Cover — and any waiting period — "
                             f"runs from {(on or dt.date.today()):%d %b %Y}.")
            elif action == "reinstate":
                scheme_svc.reinstate(m, user=request.user, on=on, reason=reason)
                messages.success(
                    request, f"{m.member.name} reinstated. Note their waiting period runs "
                             f"again from {(on or dt.date.today()):%d %b %Y}.")
            elif action == "fee":
                form = FeeForm(request.POST)
                if not form.is_valid():
                    messages.error(request, "Check the fee details.")
                    return redirect("benevolent_membership_detail", pk=pk)
                d = form.cleaned_data
                c = contrib_svc.record_fee(
                    m, kind=d["kind"], amount=d.get("amount"), date=d["date"],
                    user=request.user, channel=d.get("channel"))
                messages.success(
                    request, f"{d['kind'].title()} fee of {c.amount} receipted.")
            elif action == "papers":
                m.registration_form_on_file = bool(request.POST.get("form_on_file"))
                m.id_document_on_file = bool(request.POST.get("id_on_file"))
                m.save(update_fields=["registration_form_on_file", "id_document_on_file"])
                messages.success(request, "Registration papers updated.")
            elif action == "nominee":
                form = NomineeForm(request.POST)
                if not form.is_valid():
                    messages.error(request, "; ".join(
                        f"{k}: {v[0]}" for k, v in form.errors.items()))
                    return redirect("benevolent_membership_detail", pk=pk)
                n = form.save(commit=False)
                n.membership = m
                n.full_clean()
                n.save()
                messages.success(request, f"{n.name} recorded as a nominee "
                                          f"({n.share_percent}%).")
            elif action == "edit":
                form = MembershipEditForm(request.POST, instance=m)
                if not form.is_valid():
                    messages.error(request, "; ".join(
                        f"{k}: {v[0]}" for k, v in form.errors.items()))
                    return redirect("benevolent_membership_detail", pk=pk)
                form.save()
                messages.success(request, "Membership details updated.")
            else:
                messages.error(request, "Unknown action.")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        return redirect("benevolent_membership_detail", pk=pk)


# ---------------------------------------------------------------------------
# Levy round
# ---------------------------------------------------------------------------

class CaseLevyView(BenevolentViewMixin, View):
    """The levy round for a case: who owes it, who has paid, who has not."""

    def get(self, request, pk):
        case = get_object_or_404(
            BenevolentCase.objects.select_related("scheme", "membership__member"), pk=pk)
        if case.status == BenevolentCase.Status.DRAFT:
            messages.info(
                request, f"{case.number} is still a draft — it has not been submitted "
                        f"for review yet, so there is nothing settled to collect a "
                        f"levy against. Submit it first.")
            return redirect("benevolent_case_detail", pk=pk)
        try:
            summary = contrib_svc.raise_case_levy(case)
        except ValidationError as e:
            messages.info(request, "; ".join(e.messages))
            return redirect("benevolent_case_detail", pk=pk)
        return render(request, "benevolent/case_levy.html",
                      {"case": case, "levy": summary})

    def post(self, request, pk):
        """Receipt one member's levy payment."""
        from core.roles import can_manage_benevolent
        case = get_object_or_404(BenevolentCase, pk=pk)
        if not can_manage_benevolent(request.user):
            messages.error(request, "You don't have the benevolent-administration right.")
            return redirect("benevolent_case_levy", pk=pk)
        if case.status == BenevolentCase.Status.DRAFT:
            messages.error(
                request, f"{case.number} is still a draft — submit it for review "
                        f"before collecting a levy against it.")
            return redirect("benevolent_case_detail", pk=pk)
        m = SchemeMembership.objects.filter(
            pk=request.POST.get("membership"), scheme=case.scheme).first()
        try:
            amount = Decimal(request.POST.get("amount") or 0)
        except Exception:  # noqa: BLE001
            amount = Decimal(0)
        if not m or amount <= 0:
            messages.error(request, "Choose a member and a positive amount.")
            return redirect("benevolent_case_levy", pk=pk)
        try:
            contrib_svc.record_contribution(
                case.scheme, date=dt.date.today(), amount=amount, user=request.user,
                membership=m, case=case,
                note=f"Levy for {case.number}",
                reference=f"{case.scheme.code} LEVY {case.number}")
        except ValidationError as e:
            messages.error(request, "; ".join(e.messages))
        else:
            messages.success(request, f"Levy of {amount} receipted from {m.member.name}.")
        return redirect("benevolent_case_levy", pk=pk)


class SmsCenterView(BenevolentManageMixin, View):
    """One versatile page for every "text the members" scenario (Round 9,
    items 8/9): a case just approved and the levy roster should know, a
    defaulter needs a nudge before they are marked inactive, or just reaching
    a scheme's whole membership. One audience-picker, one composer, one send
    path — reusing the exact arrears/inactivity/levy-roster logic the rest of
    the module already computes, so an audience here can never disagree with
    what a member's own standing page says about them.

    Sending is synchronous and immediate — a typical scheme's membership is
    small enough (tens to a few hundred) that this does not need a background
    queue, and every attempt is logged to SmsLog exactly as any other SMS in
    the app is.
    """
    template_name = "benevolent/sms_center.html"

    def get(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        preview_audience = request.GET.get("audience") or ""
        preview_case = request.GET.get("case") or ""
        recipients = self._resolve_audience(scheme, preview_audience, preview_case)
        with_phone = sum(1 for m, _ in recipients if m.member.receipt_phone)
        return render(request, self.template_name, {
            "scheme": scheme,
            "audiences": bulk_sms.AUDIENCES,
            "presets": bulk_sms.PRESETS,
            "cases": (BenevolentCase.objects.filter(
                        scheme=scheme, status__in=BenevolentCase.OPEN_STATUSES)
                     .order_by("-event_date")),
            "selected_audience": preview_audience,
            "selected_case": preview_case,
            "recipients": recipients,
            "recipient_count": len(recipients),
            "with_phone": with_phone,
            "no_phone_count": len(recipients) - with_phone,
        })

    def post(self, request, pk):
        scheme = get_object_or_404(BenevolentScheme, pk=pk)
        audience_key = request.POST.get("audience") or ""
        case_id = request.POST.get("case") or ""
        message = (request.POST.get("message") or "").strip()
        if not message:
            messages.error(request, "Write a message before sending.")
            return redirect("benevolent_sms_center", pk=pk)

        recipients = self._resolve_audience(scheme, audience_key, case_id)
        if not recipients:
            messages.warning(request, "Nobody matched that audience — nothing sent.")
            return redirect("benevolent_sms_center", pk=pk)

        result = bulk_sms.send_bulk_sms(recipients, message, scheme=scheme)
        summary = (f"{result.sent} sent, {result.failed} failed, "
                  f"{result.no_phone} had no phone number on file "
                  f"(of {len(recipients)} in the audience).")
        if result.sent:
            messages.success(request, summary)
        else:
            messages.error(request, summary)
        return redirect("benevolent_sms_center", pk=pk)

    def _resolve_audience(self, scheme, audience_key, case_id):
        if audience_key == "CASE_ROSTER":
            if not case_id:
                return []
            case = BenevolentCase.objects.filter(pk=case_id, scheme=scheme).first()
            if case is None:
                return []
            return bulk_sms.audience_case_roster_unpaid(case)
        entry = bulk_sms.AUDIENCES.get(audience_key)
        if entry is None or entry[1] is None:
            return []
        _label, fn = entry
        return fn(scheme)
