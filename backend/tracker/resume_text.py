"""
Resume text extraction for PDF and DOCX files.

Text is not stored: the ``Resume`` model has no text field. It is extracted
when a resume is uploaded (to reject unreadable files) and again on demand from
the stored file. See docs/PROJECT_SPEC.md.
"""

import io
import os
import zipfile

import docx
from pypdf import PdfReader

MAX_RESUME_BYTES = 10 * 1024 * 1024  # 10 MB upload limit
MAX_DOCX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024  # zip-bomb guard
SUPPORTED_EXTENSIONS = (".pdf", ".docx")

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"


class ResumeTextError(Exception):
    """The file is unsupported, invalid, or contains no extractable text."""


def file_extension(name):
    return os.path.splitext(name or "")[1].lower()


def extract_resume_text(file_obj, filename):
    """Return the plain text of a PDF or DOCX resume.

    ``file_obj`` is any binary file-like object (an upload or a stored file).
    It raises ``ResumeTextError`` with a user-facing message on any problem.
    """
    extension = file_extension(filename)
    if extension not in SUPPORTED_EXTENSIONS:
        raise ResumeTextError(
            "Unsupported file type. Upload a PDF (.pdf) or Word (.docx) file."
        )

    size = getattr(file_obj, "size", None)
    if size is not None and size > MAX_RESUME_BYTES:
        raise ResumeTextError("File is too large. The maximum size is 10 MB.")

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    data = file_obj.read(MAX_RESUME_BYTES + 1)
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)

    if not data:
        raise ResumeTextError("The file is empty.")
    if len(data) > MAX_RESUME_BYTES:
        raise ResumeTextError("File is too large. The maximum size is 10 MB.")

    if extension == ".pdf":
        text = _extract_pdf(data)
    else:
        text = _extract_docx(data)

    text = text.strip()
    if not text:
        raise ResumeTextError(
            "No text could be extracted from the file. Scanned or image-only "
            "resumes are not supported."
        )
    return text


def _extract_pdf(data):
    if not data.startswith(PDF_MAGIC):
        raise ResumeTextError("The file is not a valid PDF.")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            # Many PDFs are "encrypted" with an empty user password only.
            if not reader.decrypt(""):
                raise ResumeTextError("Password-protected PDFs are not supported.")
        pages = [page.extract_text() or "" for page in reader.pages]
    except ResumeTextError:
        raise
    except Exception as exc:  # PyPdfError, ValueError, KeyError... on corrupt input
        raise ResumeTextError("The PDF could not be read.") from exc
    return "\n".join(pages)


def _extract_docx(data):
    if not data.startswith(ZIP_MAGIC):
        raise ResumeTextError("The file is not a valid DOCX document.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if "word/document.xml" not in archive.namelist():
                raise ResumeTextError("The file is not a valid DOCX document.")
            total = sum(info.file_size for info in archive.infolist())
            if total > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise ResumeTextError("The DOCX document is too large to process.")
        document = docx.Document(io.BytesIO(data))
    except ResumeTextError:
        raise
    except Exception as exc:  # BadZipFile, KeyError, lxml errors, ValueError...
        raise ResumeTextError("The DOCX document could not be read.") from exc

    lines = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            cells = []
            for cell in row.cells:
                cell_text = cell.text.strip()
                # Merged cells repeat; keep each value once per row.
                if cell_text and cell_text not in cells:
                    cells.append(cell_text)
            if cells:
                lines.append(" | ".join(cells))
    return "\n".join(lines)
