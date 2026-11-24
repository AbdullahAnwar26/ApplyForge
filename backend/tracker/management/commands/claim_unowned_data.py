"""Give data created before accounts existed to an account (accounts milestone).

    python manage.py claim_unowned_data <username>          # claim
    python manage.py claim_unowned_data <username> --dry-run   # only show what would be claimed

The very first account created through sign-up claims such data automatically; use this
when the first account was created otherwise (e.g. ``createsuperuser``).
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from tracker.accounts import claim_unowned_data, unowned_counts


class Command(BaseCommand):
    help = "Give every profile, job, company, contact and collector run without an owner to one account."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--dry-run", action="store_true", help="Show the counts; change nothing.")

    def handle(self, *args, **options):
        User = get_user_model()
        try:
            user = User.objects.get(username__iexact=options["username"])
        except User.DoesNotExist:
            raise CommandError(f"No account named {options['username']!r}.")
        counts = unowned_counts() if options["dry_run"] else claim_unowned_data(user)
        verb = "Would claim" if options["dry_run"] else "Claimed"
        summary = ", ".join(f"{n} {model}" for model, n in counts.items())
        self.stdout.write(f"{verb} for {user.username}: {summary}.")
