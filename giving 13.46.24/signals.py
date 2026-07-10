from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver


@receiver(post_save, sender="giving.DevGroupPattern")
@receiver(post_delete, sender="giving.DevGroupPattern")
def _clear_pattern_cache(sender, **kwargs):
    from giving.services.allocation import clear_pattern_cache
    clear_pattern_cache()
