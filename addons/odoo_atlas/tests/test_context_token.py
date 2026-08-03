import base64
import json
import os
import time
from unittest.mock import patch

from odoo.addons.odoo_atlas.services import context_token, secrets
from odoo.addons.odoo_atlas.tests.common import AtlasCase

SECRET = "test-context-secret"
OTHER_SECRET = "a-different-context-secret"


class ContextTokenCase(AtlasCase):
    """Pins the signing key, so a token minted here cannot verify anywhere else."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.startClassPatcher(patch.dict(os.environ, {secrets.CONTEXT_SECRET_VAR: SECRET}))

    def mint_for(self, user, **kwargs):
        return context_token.mint(self.env(user=user), **kwargs)

    def signed(self, **payload):
        """Build a well-signed token with a payload of our choosing.

        Used to assert on claims Odoo would never mint, which is the interesting
        half of what verification is for.
        """
        encoded = encode(**payload)
        return f"{context_token.TOKEN_VERSION}.{encoded}.{context_token._sign(encoded)}"


class TestContextToken(ContextTokenCase):
    def test_a_minted_token_verifies_and_names_its_user(self):
        claim = context_token.verify(self.mint_for(self.alice))

        self.assertEqual(claim.uid, self.alice.id)
        self.assertEqual(claim.company_ids, (self.company.id,))
        self.assertGreater(claim.expires_at, time.time())

    def test_a_token_carries_nothing_but_an_identity(self):
        # Signed, not encrypted, and meant to be readable. This asserts there is
        # nothing in it that should not be readable.
        token = self.mint_for(self.alice)
        payload = json.loads(base64.urlsafe_b64decode(pad(token.split(".")[1])))

        self.assertEqual(set(payload), {"uid", "cid", "exp"})

    def test_a_tampered_payload_is_refused(self):
        signature = self.mint_for(self.alice).rsplit(".", 1)[1]
        forged = encode(uid=self.bob.id, cid=[self.company.id], exp=later())

        with self.assertRaises(context_token.ContextTokenError):
            context_token.verify(f"{context_token.TOKEN_VERSION}.{forged}.{signature}")

    def test_a_tampered_signature_is_refused(self):
        version, encoded, signature = self.mint_for(self.alice).split(".")
        flipped = ("0" if signature[0] != "0" else "1") + signature[1:]

        with self.assertRaises(context_token.ContextTokenError):
            context_token.verify(f"{version}.{encoded}.{flipped}")

    def test_a_token_signed_with_another_secret_is_refused(self):
        with patch.dict(os.environ, {secrets.CONTEXT_SECRET_VAR: OTHER_SECRET}):
            token = self.mint_for(self.alice)

        with self.assertRaises(context_token.ContextTokenError):
            context_token.verify(token)

    def test_an_expired_token_is_refused(self):
        expired = self.signed(uid=self.alice.id, cid=[self.company.id], exp=int(time.time()) - 1)

        with self.assertRaises(context_token.ContextTokenError):
            context_token.verify(expired)

    def test_a_malformed_token_is_refused(self):
        for candidate in ("", None, "nonsense", "v1.only-two-parts", "v2.a.b", "v1..sig"):
            with self.assertRaises(context_token.ContextTokenError):
                context_token.verify(candidate)

    def test_a_correctly_signed_but_empty_claim_is_refused(self):
        with self.assertRaises(context_token.ContextTokenError):
            context_token.verify(self.signed(uid=0, cid=[], exp=later()))

    def test_without_a_signing_key_nothing_verifies(self):
        token = self.mint_for(self.alice)

        with (
            patch.dict(os.environ, {secrets.CONTEXT_SECRET_VAR: ""}),
            self.assertRaises(secrets.SecretNotConfiguredError),
        ):
            context_token.verify(token)


class TestContextTokenUsability(ContextTokenCase):
    """What ``assert_usable`` adds: the database's opinion, not the payload's."""

    def test_a_valid_token_yields_the_users_companies(self):
        claim = context_token.verify(self.mint_for(self.alice))

        allowed = context_token.assert_usable(self.env(user=self.alice), claim)

        self.assertEqual(allowed, (self.company.id,))

    def test_losing_atlas_access_takes_effect_immediately(self):
        claim = context_token.verify(self.mint_for(self.alice))
        atlas_user = self.env.ref("odoo_atlas.group_atlas_user")
        self.alice.write({"group_ids": [(3, atlas_user.id)]})

        with self.assertRaises(context_token.ContextTokenError):
            context_token.assert_usable(self.env(user=self.alice), claim)

    def test_an_archived_user_cannot_be_acted_for(self):
        claim = context_token.verify(self.mint_for(self.alice))
        self.alice.write({"active": False})

        with self.assertRaises(context_token.ContextTokenError):
            context_token.assert_usable(self.env(user=self.alice), claim)

    def test_a_token_cannot_widen_its_own_company_scope(self):
        claim = context_token.verify(
            self.signed(
                uid=self.alice.id,
                cid=[self.company.id, self.other_company.id],
                exp=later(),
            )
        )

        allowed = context_token.assert_usable(self.env(user=self.alice), claim)

        self.assertEqual(allowed, (self.company.id,))

    def test_a_token_naming_only_companies_the_user_lacks_is_refused(self):
        claim = context_token.verify(
            self.signed(uid=self.alice.id, cid=[self.other_company.id], exp=later())
        )

        with self.assertRaises(context_token.ContextTokenError):
            context_token.assert_usable(self.env(user=self.alice), claim)


def encode(**payload):
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def pad(encoded):
    return encoded + "=" * (-len(encoded) % 4)


def later():
    return int(time.time()) + 900
