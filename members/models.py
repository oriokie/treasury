import re
from django.db import models
from simple_history.models import HistoricalRecords


def phone_search_variants(raw):
    """Other forms of a typed number worth searching for.

    Numbers are normalised on save, so 0712345678 is stored as 254712345678 —
    and the local form is the one a treasurer is certain to type, from the
    envelope or the screen in front of them. A complete number is normalised;
    a partial one (which ``normalize_phone`` declines, rightly) has its leading
    zero stripped so "0712" still reaches "254712…".
    """
    raw = (raw or "").strip()
    if not raw or not any(c.isdigit() for c in raw):
        return []
    out = []
    full = normalize_phone(raw)
    if full and full != raw:
        out.append(full)
    digits = "".join(c for c in raw if c.isdigit())
    if digits.startswith("0") and len(digits) > 1:
        out.append(digits[1:])
    elif digits and digits not in out and digits != raw:
        out.append(digits)
    return out


def normalize_phone(raw):
    """All Kenyan forms -> '2547XXXXXXXX' / '2541XXXXXXXX'. None if implausible."""
    if not raw:
        return None
    d = re.sub(r"\D", "", str(raw))
    if len(d) == 10 and d.startswith("0"):
        d = "254" + d[1:]
    elif len(d) == 9 and d[0] in "71":
        d = "254" + d
    return d if len(d) == 12 and d.startswith("254") else None


def name_key(raw):
    """Order-insensitive match key: 'RUTH MOMANYI' and 'MOMANYI RUTH' -> same."""
    if not raw:
        return ""
    n = re.sub(r"[^A-Z ]", " ", raw.upper())
    return " ".join(sorted(n.split()))


def mask_phone(raw, visible=3):
    """Mask a phone number for privacy, keeping only the last `visible` digits,
    e.g. '254712345678' -> '*********678'. Used when a departmental leader views
    member/pledge data — they should not see full contact numbers. Returns '' for
    empty input."""
    if not raw:
        return ""
    s = str(raw).strip()
    if len(s) <= visible:
        return "*" * len(s)
    return "*" * (len(s) - visible) + s[-visible:]


class MemberTag(models.Model):
    """A role or standing a member holds — board member, Sabbath School,
    committee, elder.

    Deliberately separate from ``Member.group``, which is the one ministry a
    member belongs to (Youth, AWM, …) and is single-valued by design. A role is
    not that: a person sits on the board AND the finance committee AND teaches
    Sabbath School, and the church invents its own as it goes. So this is a
    church-defined list with a many-to-many, not another fixed choice field.

    Held on the member rather than on the pledge because it describes the
    person, not the promise — which is what makes "what have the committee
    pledged" answerable, and answerable the same way for anything else the
    church later wants grouped this way.
    """
    name = models.CharField(max_length=60, unique=True)
    description = models.CharField(max_length=160, blank=True)
    active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Member(models.Model):
    class Group(models.TextChoices):
        YOUTH = "YOUTH", "Youth"
        AMM = "AMM", "Adventist Men's Ministries"
        AWM = "AWM", "Adventist Women's Ministries"
        AMBASSADORS = "AMBASSADORS", "Ambassadors"
        CHILDREN = "CHILDREN", "Children"

    class Source(models.TextChoices):
        MANUAL = "MANUAL", "Entered manually"
        AUTO_BANK = "AUTO_BANK", "Created from bank statement"

    class MemberType(models.TextChoices):
        MEMBER = "MEMBER", "Church member"
        SS_MEMBER = "SS_MEMBER", "Sabbath School member"

    name = models.CharField(max_length=120)
    name_key = models.CharField(max_length=120, db_index=True, editable=False)
    phone = models.CharField(max_length=12, blank=True, null=True, db_index=True)
    group = models.CharField(max_length=12, choices=Group.choices, blank=True, null=True)
    member_type = models.CharField(
        max_length=12, choices=MemberType.choices, blank=True, null=True,
        help_text="Optional: church member or Sabbath School member.")
    tags = models.ManyToManyField(MemberTag, blank=True, related_name="members",
        help_text="Roles this member holds — board, committee, Sabbath School. "
                  "A member may hold several.")
    dev_group = models.ForeignKey(
        "departments.DevelopmentGroup", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="members",
    )
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.MANUAL)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    history = HistoricalRecords()

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["name_key"]), models.Index(fields=["phone"]),
                   models.Index(fields=["name"])]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        # store names in uppercase for a consistent register across imports,
        # bank statements and envelope entry (collation-independent matching too)
        if self.name:
            self.name = " ".join(self.name.upper().split())
        self.name_key = name_key(self.name)
        self.phone = normalize_phone(self.phone) or self.phone
        super().save(*args, **kwargs)

    @property
    def receipt_phone(self):
        """The single number to use for receipting/SMS when a member has several.
        Prefers the explicitly-marked primary, then the member's own phone field,
        then the most-recently-added alternate number."""
        primary = self.phones.filter(is_primary=True).first()
        if primary:
            return primary.number
        if self.phone:
            return self.phone
        any_phone = self.phones.order_by("-created_at").first()
        return any_phone.number if any_phone else None

    def add_phone(self, raw, make_primary=False):
        """Record an additional number for this member (deduped, normalised).
        Returns the MemberPhone or None if the number is implausible."""
        num = normalize_phone(raw)
        if not num:
            return None
        mp, created = self.phones.get_or_create(number=num)
        if make_primary or not self.phones.filter(is_primary=True).exists():
            self.phones.update(is_primary=False)
            mp.is_primary = True
            mp.save(update_fields=["is_primary"])
        if not self.phone:
            self.phone = num
            super().save(update_fields=["phone"])
        return mp


class MemberAlias(models.Model):
    """Other known names for a member, so a statement's spelling still matches."""
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="aliases")
    name = models.CharField(max_length=120)
    name_key = models.CharField(max_length=120, db_index=True, editable=False)

    class Meta:
        verbose_name_plural = "member aliases"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        self.name_key = name_key(self.name)
        super().save(*args, **kwargs)


class PossibleDuplicate(models.Model):
    """Surfaced when an auto-created member shares a name with an existing one."""
    member = models.OneToOneField(Member, on_delete=models.CASCADE, related_name="duplicate_flag")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved = models.BooleanField(default=False)

    def __str__(self):
        return f"Possible duplicate: {self.member}"


class MemberPhone(models.Model):
    """An additional phone number for a member who pays from more than one line.
    Exactly one number per member is marked primary and used for receipting."""
    member = models.ForeignKey(Member, on_delete=models.CASCADE, related_name="phones")
    number = models.CharField(max_length=12, db_index=True)
    is_primary = models.BooleanField(default=False)
    label = models.CharField(max_length=40, blank=True, help_text="e.g. 'M-Pesa', 'work'")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("member", "number")]
        ordering = ["-is_primary", "created_at"]

    def __str__(self):
        return f"{self.number}{' (primary)' if self.is_primary else ''}"

    def save(self, *args, **kwargs):
        self.number = normalize_phone(self.number) or self.number
        super().save(*args, **kwargs)
