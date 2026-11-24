"""Shared helpers for tracker API tests."""

import io
import shutil
import tempfile

import docx
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.signals import pre_save
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

TEST_PASSWORD = "a-Fictional-test-passw0rd"


def make_pdf_bytes(text="Jane Doe Python Django Engineer"):
    """Build a minimal, valid one-page PDF. An empty ``text`` gives a page with no text."""
    content = (
        f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1") if text else b""
    )
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_at,
    )
    return out


def make_encrypted_pdf_bytes():
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(make_pdf_bytes())))
    writer.encrypt(user_password="secret", algorithm="RC4-128")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def make_docx_bytes(paragraphs=("Jane Doe", "Senior Django developer"), table=None):
    document = docx.Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    if table:
        grid = document.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                grid.cell(r, c).text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def upload(name, data, content_type="application/octet-stream"):
    return SimpleUploadedFile(name, data, content_type=content_type)


def make_user(username="tester", password=TEST_PASSWORD):
    return get_user_model().objects.create_user(username=username, password=password)


class APITestCase(TestCase):
    """A signed-in user (session), an anonymous client and an isolated temporary MEDIA_ROOT.

    Rows of owned models that a test creates directly without an ``owner`` get ``self.user``
    as their owner, so existing tests keep meaning "my data". Isolation tests create another
    account's rows with an explicit ``owner``.
    """

    def setUp(self):
        super().setUp()
        self.media_root = tempfile.mkdtemp(prefix="applyforge-test-media-")
        self.addCleanup(shutil.rmtree, self.media_root, ignore_errors=True)
        settings_override = override_settings(MEDIA_ROOT=self.media_root)
        settings_override.enable()
        self.addCleanup(settings_override.disable)

        self.user = make_user()
        self.client = APIClient()
        self.client.force_login(self.user)
        self.anon = APIClient()

        from tracker.accounts import OWNED_MODELS

        def default_owner(sender, instance, **kwargs):
            if instance.owner_id is None:
                instance.owner = self.user

        for model in OWNED_MODELS:
            # weak=False: the handler is a local function and would otherwise be garbage-collected.
            pre_save.connect(default_owner, sender=model, weak=False, dispatch_uid=f"test-owner-{model.__name__}")
            self.addCleanup(pre_save.disconnect, sender=model, dispatch_uid=f"test-owner-{model.__name__}")
