"""
Per-user data isolation for the API (accounts milestone).

``owned(queryset, user)`` limits any queryset of an owned model to the user's rows, and
``OwnedRelationsSerializer`` applies the same limit to every writable related field
(``profile``, ``job``, ``contact``, ``resume``...) so an id of another account is answered
exactly like an id that does not exist. Without a signed-in user both fail closed (no rows).
"""

from rest_framework import serializers
from rest_framework.relations import ManyRelatedField, RelatedField

from collector.models import CollectedPost, CollectorRun
from tracker.models import (
    Application,
    ApplicationEvent,
    Company,
    Contact,
    JobOpportunity,
    Profile,
    Resume,
    SendingAccount,
    SendRecord,
    Skill,
)

# How each model reaches its owner.
OWNER_PATHS = {
    Profile: "owner",
    JobOpportunity: "owner",
    Company: "owner",
    Contact: "owner",
    CollectorRun: "owner",
    Skill: "profile__owner",
    Resume: "profile__owner",
    Application: "profile__owner",
    ApplicationEvent: "application__profile__owner",
    CollectedPost: "run__owner",
    SendingAccount: "owner",
    SendRecord: "application__profile__owner",
}


def owned(queryset, user):
    """``queryset`` limited to ``user``'s rows; empty without an authenticated user."""
    if user is None or not getattr(user, "is_authenticated", False):
        return queryset.none()
    return queryset.filter(**{OWNER_PATHS[queryset.model]: user})


class OwnedRelationsMixin:
    """Limit writable related fields to the requesting user's objects.

    Needs ``context={"request": request}`` (generic views pass it automatically).
    """

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        user = getattr(request, "user", None)
        for field in fields.values():
            target = field.child_relation if isinstance(field, ManyRelatedField) else field
            queryset = getattr(target, "queryset", None) if isinstance(target, RelatedField) else None
            if queryset is not None and queryset.model in OWNER_PATHS:
                target.queryset = owned(queryset, user)
        return fields


class OwnedSerializer(OwnedRelationsMixin, serializers.Serializer):
    pass


class OwnedModelSerializer(OwnedRelationsMixin, serializers.ModelSerializer):
    pass
