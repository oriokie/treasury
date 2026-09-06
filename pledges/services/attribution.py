"""Apply match-code attribution when creating a bank contribution."""


def apply_code_to_import(*, reference, member, dept, dev_group, campaign,
                         campaign_group, status, Transaction):
    """Apply match-code attribution on a bank credit being created.

    Returns updated (member, dept, dev_group, campaign, campaign_group, status,
    attributed_via_code). Safe no-op when no code is present.

    ``member`` becomes the *beneficiary* (who gets credit); payer identity
    stays on payer_name / payer_phone set by the caller.

    Pledge (PG), member (MB), campaign (CM) and development-group (DEV) codes
    all go through this path so group tallies and "paid by others" stay consistent.
    """
    from pledges.services.codes import resolve_code_attribution
    try:
        hit = resolve_code_attribution(reference, payer_member=member)
    except Exception:  # noqa: BLE001
        return member, dept, dev_group, campaign, campaign_group, status, False
    if not hit:
        return member, dept, dev_group, campaign, campaign_group, status, False

    payer = member
    if hit.get("beneficiary") is not None:
        member = hit["beneficiary"]
    if hit.get("department") is not None and dept is None:
        dept = hit["department"]
        status = Transaction.Status.AUTO
    # Any code that names a beneficiary's / group's home group credits that
    # group — pledge, member, campaign, and DEV group codes alike.
    if hit.get("dev_group") is not None:
        if hit.get("source") in ("member", "pledge", "campaign", "dev_group") \
                or dev_group is None:
            dev_group = hit["dev_group"]
    if hit.get("campaign") is not None:
        campaign = hit["campaign"]
        campaign_group = hit.get("campaign_group") or ""
        if hit.get("department") is not None:
            dept = hit["department"]
            status = Transaction.Status.AUTO
    # Group-only DEV code (or member code with home group) may still leave
    # dept unset if allocate() missed — stamp Development + AUTO.
    if dept is None and hit.get("dev_group") is not None \
            and hit.get("department") is not None:
        dept = hit["department"]
        status = Transaction.Status.AUTO
    elif dept is None and hit.get("department") is not None:
        dept = hit["department"]
        status = Transaction.Status.AUTO
    # Flag only when the gift was redirected to someone other than the payer
    via = bool(
        hit.get("via_code")
        and hit.get("beneficiary") is not None
        and (payer is None or getattr(payer, "pk", None) != hit["beneficiary"].pk)
    )
    return member, dept, dev_group, campaign, campaign_group, status, via
