"""What must not reach a model provider, and what deliberately must.

The obvious reading of "redact PII before it enters a prompt" is to strip names,
emails and phone numbers. That would make this product useless. Atlas exists to
answer "what is Acme's contact address", "who is the account manager", "which
customers haven't ordered" — the personal data *is* the question. Redacting it
would leave an assistant that can only discuss records in the abstract, and the
first thing anyone would do is turn the redaction off.

So the line is drawn somewhere defensible instead: **remove what no legitimate
ERP question needs, keep what the questions are about.**

Removed:

- Payment card numbers, validated with Luhn. An ERP is full of long digit
  strings — order references, VAT numbers, EANs — and a length-based rule would
  redact half of them. Luhn is what makes this precise enough to be left on.
- IBANs, validated mod-97, for the same reason.
- Credentials pasted into records: provider API keys with recognisable prefixes,
  bearer tokens, private key blocks, and `password: …` in free text. People do
  put these in internal notes, and there is no question whose answer requires
  one.
- US Social Security numbers, and only when labelled as such. The bare pattern
  ``123-45-6789`` matches too many things.

Kept, deliberately: names, email addresses, phone numbers, postal addresses,
company registration and VAT numbers. These are the subject matter. That choice
is a documented trade, not an oversight — see ``docs/security.md``.

Everything here is a pure function over text. Redaction is applied at the two
places content crosses into a prompt: assembled context, and tool results.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: What replaces a match. Named rather than blanked so a reader — human or
#: model — can tell that something was removed and what kind of thing it was,
#: instead of wondering why a sentence stops mid-way.
PLACEHOLDER: Final = "[redacted: {kind}]"


@dataclass(frozen=True, slots=True)
class Redaction:
    """Text with its secrets removed, and a note of what went.

    Attributes:
        text: Safe to put in a prompt.
        counts: How many of each kind were removed. Counts rather than values —
            this is logged, and logging what was redacted would defeat the
            redaction.
    """

    text: str
    counts: dict[str, int]

    @property
    def redacted(self) -> bool:
        """Whether anything was removed."""
        return bool(self.counts)

    @property
    def total(self) -> int:
        """How many secrets were removed in total."""
        return sum(self.counts.values())


# A run of 13-19 digits, optionally grouped by spaces or hyphens. Deliberately
# permissive: the Luhn check below decides, not the shape.
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")

# Two letters, two check digits, then up to 30 alphanumerics. Validated mod-97.
_IBAN = re.compile(r"\b([A-Z]{2}\d{2}[ ]?(?:[A-Z0-9][ ]?){10,30})\b")

# Key formats that are unambiguous on sight. Each is a published prefix, so a
# match is a credential rather than a guess about entropy.
_API_KEY = re.compile(
    r"\b("
    r"sk-[A-Za-z0-9_-]{16,}"  # OpenAI and lookalikes
    r"|sk-ant-[A-Za-z0-9_-]{16,}"  # Anthropic
    r"|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"  # Slack
    r"|AKIA[0-9A-Z]{16}"  # AWS access key id
    r"|AIza[0-9A-Za-z_-]{35}"  # Google
    r"|glpat-[A-Za-z0-9_-]{20,}"  # GitLab
    r")\b"
)

# `Authorization: Bearer …`, and the bare form people paste under it.
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*", re.IGNORECASE)

# A whole PEM block. Non-greedy so two keys in one note are two matches.
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

# A password stated in prose. The label is required: a bare token is not
# distinguishable from a product code.
_PASSWORD = re.compile(
    r"\b(?:password|passwd|pwd|passphrase|secret)\s*[:=]\s*\S{4,}",
    re.IGNORECASE,
)

# Only when labelled. `123-45-6789` on its own matches part numbers.
_SSN = re.compile(
    r"\b(?:ssn|social security(?: number)?)\s*[:#]?\s*(\d{3}-\d{2}-\d{4})\b",
    re.IGNORECASE,
)

#: Ordered. Private keys and passwords first: both can contain runs of digits
#: that a later pattern would otherwise pick apart into fragments.
_RULES: Final = (
    ("private key", _PRIVATE_KEY, None),
    ("password", _PASSWORD, None),
    ("api key", _API_KEY, None),
    ("bearer token", _BEARER, None),
    ("national id", _SSN, None),
    ("payment card", _CARD, "luhn"),
    ("bank account", _IBAN, "iban"),
)


def redact(text: str) -> Redaction:
    """Remove credentials and regulated identifiers from ``text``.

    Names, emails, phone numbers and addresses are left alone on purpose: they
    are what the questions are about. See the module docstring.
    """
    if not text:
        return Redaction(text=text, counts={})

    counts: dict[str, int] = {}
    result = text
    for kind, pattern, validator in _RULES:
        replaced = 0

        def substitute(
            match: re.Match[str], _kind: str = kind, _check: str | None = validator
        ) -> str:
            nonlocal replaced
            if _check and not _validate(_check, match.group(0)):
                return match.group(0)
            replaced += 1
            return PLACEHOLDER.format(kind=_kind)

        result = pattern.sub(substitute, result)
        if replaced:
            counts[kind] = replaced

    return Redaction(text=result, counts=counts)


def _validate(check: str, value: str) -> bool:
    """Whether a shape-matched candidate is really what it looks like."""
    if check == "luhn":
        return _luhn(value)
    if check == "iban":
        return _iban(value)
    return True


def _luhn(value: str) -> bool:
    """The check digit algorithm every payment card carries.

    This is what makes card detection usable in an ERP. Order references,
    EAN-13s and VAT numbers are all long digit strings, and roughly nine in ten
    of them fail Luhn — so the rule redacts cards without shredding the rest of
    the corpus.
    """
    digits = [int(character) for character in value if character.isdigit()]
    if not 13 <= len(digits) <= 19:  # noqa: PLR2004 - the ISO/IEC 7812 range
        return False

    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 0:
            total += digit
            continue
        doubled = digit * 2
        # Casting out nines: 12 becomes 3, which is what doubling means here.
        total += doubled - 9 if doubled > 9 else doubled  # noqa: PLR2004
    return total % 10 == 0


def _iban(value: str) -> bool:
    """The mod-97 check an IBAN carries, per ISO 13616."""
    compact = value.replace(" ", "").upper()
    if not 15 <= len(compact) <= 34:  # noqa: PLR2004 - the ISO 13616 range
        return False
    if not compact[:2].isalpha() or not compact[2:4].isdigit():
        return False

    # Move the country code and check digits to the end, then read letters as
    # numbers: A=10 … Z=35.
    rearranged = compact[4:] + compact[:4]
    try:
        numeric = "".join(
            str(int(character, 36)) if character.isalpha() else character
            for character in rearranged
        )
    except ValueError:
        return False
    return int(numeric) % 97 == 1
