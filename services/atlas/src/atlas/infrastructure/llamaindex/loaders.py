"""Chunking and file extraction, via LlamaIndex.

Implements :class:`~atlas.domain.ports.ingestion.DocumentLoader`. Two jobs:

**Splitting.** ``SentenceSplitter`` cuts on sentence boundaries and keeps an
overlap between neighbours, so a fact that straddles a boundary survives in both
pieces. Retrieval quality is mostly decided here and in the templates that
produce the text; M12 measures it and M13 tunes it.

**Reading files.** PDF and DOCX through ``llama-index-readers-file``. The
readers take a path, so bytes are written to a temporary file and deleted
immediately — an attachment is somebody's contract or payslip and has no
business outliving the call.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Final

from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import Document
from llama_index.readers.file import DocxReader, PDFReader

from atlas.domain.sources import DOCX_MIMETYPE, PDF_MIMETYPE

logger = logging.getLogger(__name__)

#: Tokens per segment, and how much neighbouring segments share. 512 is large
#: enough to hold a whole order line block and small enough that a hit is mostly
#: signal; the overlap is what stops a sentence being lost at a boundary.
DEFAULT_CHUNK_SIZE: Final = 512
DEFAULT_CHUNK_OVERLAP: Final = 64

#: Read as text and decoded directly. Running a PDF reader over Markdown would
#: be slower and worse.
_TEXT_PREFIX: Final = "text/"


class LlamaIndexDocumentLoader:
    """A :class:`DocumentLoader` backed by LlamaIndex node parsers and readers."""

    def __init__(
        self,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
    ) -> None:
        self._splitter = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        self._pdf = PDFReader(return_full_document=True)
        self._docx = DocxReader()

    def split_text(self, text: str) -> list[str]:
        """Cut text into overlapping segments, in order.

        Empty and whitespace-only input yields no segments rather than one empty
        one: a document with nothing in it should cost no embedding call.
        """
        if not text or not text.strip():
            return []
        nodes = self._splitter.get_nodes_from_documents([Document(text=text)])
        return [segment for node in nodes if (segment := node.get_content().strip())]

    def load_file(self, filename: str, content: bytes, mimetype: str) -> list[str]:
        """Extract text from a file and split it.

        Returns nothing for a file that cannot be read — a scanned PDF with no
        text layer, a corrupt upload, a type nobody taught us. That is a document
        with nothing to index, not a failure: letting one bad attachment abort a
        sync would let it block the whole corpus.
        """
        try:
            text = self._extract(filename, content, mimetype)
        except Exception:
            logger.exception(
                "could not read attachment",
                extra={"attachment": filename, "mimetype": mimetype, "bytes": len(content)},
            )
            return []

        if not text.strip():
            logger.info(
                "attachment produced no text",
                extra={"attachment": filename, "mimetype": mimetype},
            )
            return []
        return self.split_text(text)

    def _extract(self, filename: str, content: bytes, mimetype: str) -> str:
        if mimetype.startswith(_TEXT_PREFIX):
            return content.decode("utf-8", errors="replace")

        if mimetype == PDF_MIMETYPE:
            return self._read_with(self._pdf, filename, content, ".pdf")
        if mimetype == DOCX_MIMETYPE:
            return self._read_with(self._docx, filename, content, ".docx")

        logger.info(
            "no reader for this file type",
            extra={"attachment": filename, "mimetype": mimetype},
        )
        return ""

    def _read_with(
        self, reader: PDFReader | DocxReader, filename: str, content: bytes, suffix: str
    ) -> str:
        """Run a path-based reader over bytes, leaving nothing behind.

        The temporary file is removed before this returns, on success and on
        failure alike. Attachments are somebody's contracts and payslips; they
        have no business surviving the call that read them.
        """
        with tempfile.TemporaryDirectory(prefix="atlas-ingest-") as directory:
            path = Path(directory) / f"upload{suffix}"
            path.write_bytes(content)
            documents = reader.load_data(path)
            logger.debug(
                "extracted attachment text",
                extra={"attachment": filename, "documents": len(documents)},
            )
            return "\n\n".join(document.get_content() for document in documents)
