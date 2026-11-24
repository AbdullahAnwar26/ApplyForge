"""
Accounts and data ownership (accounts milestone).

Each user owns their ``Profile`` rows (and through them skills, resumes, applications and
their history), their ``JobOpportunity``, ``Company`` and ``Contact`` rows and their
``CollectorRun`` rows. The API never shows or accepts another user's rows.

Rows created before accounts existed have no owner and are invisible. They are given to an
account by ``claim_unowned_data``: automatically for the very first account created, or
explicitly with ``manage.py claim_unowned_data <username>``.
"""

from django.db import transaction

from collector.models import CollectorRun
from tracker.models import Company, Contact, JobOpportunity, Profile

# Models with a direct ``owner`` foreign key. Everything else is owned through a profile.
OWNED_MODELS = (Profile, JobOpportunity, Company, Contact, CollectorRun)


def unowned_counts():
    return {model.__name__: model.objects.filter(owner__isnull=True).count() for model in OWNED_MODELS}


@transaction.atomic
def claim_unowned_data(user):
    """Give every row without an owner to ``user``. Returns ``{model name: rows claimed}``."""
    return {
        model.__name__: model.objects.filter(owner__isnull=True).update(owner=user)
        for model in OWNED_MODELS
    }
