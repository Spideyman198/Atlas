"""The prohibition from ADR-0006, enforced rather than documented.

``sudo()`` anywhere Atlas serves a request would mean a read that Odoo's record
rules never filtered — the single failure this addon exists to prevent. A
comment saying "don't" is a convention. This is a test, so adding one fails the
build.

The scan covers the whole addon except its own tests. There is no allow-list on
purpose: the moment one exists, the rule becomes a judgement call, and the
reason to trust it goes away. Everything Atlas needs that would normally justify
a ``sudo()`` — the service token, the signing key, the engine's address — comes
from the environment instead (``services/secrets.py``, ``services/engine.py``).
"""

import pathlib
import re

from odoo.tests import TransactionCase

ADDON_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Every way to step outside the acting user's rights, not just the obvious one.
ESCALATIONS = (
    ("sudo()", re.compile(r"\.sudo\s*\(")),
    ("su=True", re.compile(r"\bsu\s*=\s*True")),
    ("SUPERUSER_ID", re.compile(r"\bSUPERUSER_ID\b")),
    ("with_user(1)", re.compile(r"\.with_user\s*\(\s*1\s*\)")),
)


class TestNoPrivilegeEscalation(TransactionCase):
    def python_sources(self):
        return [
            path
            for path in sorted(ADDON_ROOT.rglob("*.py"))
            if "tests" not in path.relative_to(ADDON_ROOT).parts
        ]

    def test_the_scan_actually_reads_the_addon(self):
        # Guards the guard: a glob that matched nothing would make every
        # assertion below pass without checking anything.
        sources = self.python_sources()

        self.assertGreater(len(sources), 5)
        names = {path.name for path in sources}
        self.assertIn("atlas_api.py", names)
        self.assertIn("context_token.py", names)

    def test_nothing_in_the_addon_escalates_privileges(self):
        offences = []
        for path in self.python_sources():
            source = path.read_text(encoding="utf-8")
            for line_number, line in enumerate(source.splitlines(), start=1):
                for label, pattern in ESCALATIONS:
                    if pattern.search(line):
                        relative = path.relative_to(ADDON_ROOT)
                        offences.append(f"{relative}:{line_number} uses {label}")

        self.assertFalse(
            offences,
            "Atlas answers every request as the user who asked (ADR-0006). "
            "Found:\n  " + "\n  ".join(offences),
        )

    def test_the_scan_would_notice(self):
        # The detector, tested. A rule nobody has ever seen fail is a rule
        # nobody knows works.
        samples = (
            "records = self.env['res.partner'].sudo().search([])",
            "env = self.env(su=True)",
            "user = SUPERUSER_ID",
            "self.env['res.partner'].with_user(1).read()",
        )
        for sample in samples:
            self.assertTrue(
                any(pattern.search(sample) for _label, pattern in ESCALATIONS),
                f"the scan missed {sample!r}",
            )
