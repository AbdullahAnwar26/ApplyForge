"""
Account endpoints (accounts milestone), mounted at ``/api/v1/auth/``:

* ``GET  csrf/``      sets the ``csrftoken`` cookie; call once before the first POST.
* ``POST signup/``    ``{username, password, email?, remember?}`` -> 201 ``{user}``, signed in.
                      403 when sign-up is disabled (``SIGNUP_ENABLED=False``). The first account
                      ever created receives any data that existed before accounts.
* ``POST login/``     ``{username, password, remember?}`` -> 200 ``{user}``. Wrong username or
                      password -> 400 with one generic message (no hint which one was wrong).
                      ``remember=false`` ends the session when the browser closes.
* ``POST logout/``    -> 204.
* ``GET  me/``        -> 200 ``{user}`` or 401.
* ``POST password/``  ``{current_password, new_password}`` -> 204; stays signed in.

Login and sign-up are throttled (scope ``auth``, per client address) and check CSRF.
"""

import re

from django.conf import settings
from django.contrib.auth import authenticate, get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import ensure_csrf_cookie
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from tracker.accounts import claim_unowned_data

from .auth import SessionAuthentication401

User = get_user_model()
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@+-]{3,150}$")
BAD_LOGIN = "Incorrect username or password."


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email", "date_joined"]
        read_only_fields = fields


class SignupSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150, trim_whitespace=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)
    remember = serializers.BooleanField(required=False, default=True)

    def validate_username(self, value):
        if not USERNAME_RE.match(value):
            raise serializers.ValidationError(
                "Use 3-150 characters: letters, digits and . _ @ + - (no spaces)."
            )
        if User.objects.filter(username__iexact=value).exists():
            raise serializers.ValidationError("This username is already taken.")
        return value

    def validate(self, attrs):
        candidate = User(username=attrs["username"], email=attrs.get("email") or "")
        try:
            validate_password(attrs["password"], user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"password": list(exc.messages)})
        return attrs


class LoginSerializer(serializers.Serializer):
    username = serializers.CharField(max_length=150)
    password = serializers.CharField(max_length=128, trim_whitespace=False)
    remember = serializers.BooleanField(required=False, default=True)


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(max_length=128, trim_whitespace=False)
    new_password = serializers.CharField(max_length=128, trim_whitespace=False)


def _start_session(request, user, remember):
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    # 0 = until the browser closes; None = SESSION_COOKIE_AGE.
    request.session.set_expiry(None if remember else 0)


class _AccountView(APIView):
    authentication_classes = [SessionAuthentication401]


class _AnonymousPostView(_AccountView):
    """Login/sign-up: open to anonymous users, CSRF-checked and throttled."""

    permission_classes = [AllowAny]
    throttle_scope = "auth"

    def check_csrf(self, request):
        SessionAuthentication401().check_csrf(request)


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CsrfView(_AccountView):
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"detail": "CSRF cookie set."})


class SignupView(_AnonymousPostView):
    def post(self, request):
        self.check_csrf(request)
        if not settings.SIGNUP_ENABLED:
            return Response({"detail": "Sign-up is disabled on this server."}, status=status.HTTP_403_FORBIDDEN)
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            with transaction.atomic():
                first_account = not User.objects.exists()
                user = User.objects.create_user(
                    username=data["username"], email=data.get("email") or "", password=data["password"]
                )
                claimed = claim_unowned_data(user) if first_account else {}
        except IntegrityError:
            return Response({"username": ["This username is already taken."]}, status=status.HTTP_400_BAD_REQUEST)
        _start_session(request, user, data["remember"])
        body = {"user": UserSerializer(user).data}
        if any(claimed.values()):
            body["claimed"] = claimed
        return Response(body, status=status.HTTP_201_CREATED)


class LoginView(_AnonymousPostView):
    def post(self, request):
        self.check_csrf(request)
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        # Usernames are matched case-insensitively ("Maya" signs in as "maya").
        match = User.objects.filter(username__iexact=data["username"].strip()).first()
        user = authenticate(request, username=match.username if match else data["username"], password=data["password"])
        if user is None:
            return Response({"detail": BAD_LOGIN}, status=status.HTTP_400_BAD_REQUEST)
        _start_session(request, user, data["remember"])
        return Response({"user": UserSerializer(user).data})


class LogoutView(_AccountView):
    permission_classes = [AllowAny]

    def post(self, request):
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(_AccountView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({"user": UserSerializer(request.user).data})


class PasswordChangeView(_AccountView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "auth"

    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        user = request.user
        if not user.check_password(data["current_password"]):
            return Response({"current_password": ["The current password is not correct."]}, status=status.HTTP_400_BAD_REQUEST)
        try:
            validate_password(data["new_password"], user=user)
        except DjangoValidationError as exc:
            return Response({"new_password": list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)
        user.set_password(data["new_password"])
        user.save(update_fields=["password"])
        update_session_auth_hash(request, user)  # keep this session; other sessions are signed out
        return Response(status=status.HTTP_204_NO_CONTENT)
