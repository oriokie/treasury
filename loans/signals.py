from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver


@receiver(post_save, sender="loans.LoanNarrationPattern")
@receiver(post_delete, sender="loans.LoanNarrationPattern")
def _pattern_changed(sender, **kwargs):
    from loans.services.narration import clear_pattern_cache
    clear_pattern_cache()
