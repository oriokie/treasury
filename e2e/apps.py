from django.apps import AppConfig


class E2EConfig(AppConfig):
    """Registered in INSTALLED_APPS deliberately, though it holds no models.

    `core.test_ci_coverage` derives the list of apps that must appear in a CI
    shard from INSTALLED_APPS. A plain test package sitting outside it would be
    invisible to that guard — which is exactly how `vendors` came to have
    thirty-eight tests that ran nowhere for several releases (recommendations
    #131). Being an app is what makes forgetting this suite impossible.
    """
    name = "e2e"
    verbose_name = "End-to-end business workflows"
