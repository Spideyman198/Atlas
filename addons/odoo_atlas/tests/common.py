from odoo.tests import TransactionCase, new_test_user


class AtlasCase(TransactionCase):
    """Two Atlas users who do not know each other, and one administrator.

    Nearly every test here is about what one user can see of another's
    conversation, so the cast is set up once and shared.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.company = cls.env.ref("base.main_company")
        cls.other_company = cls.env["res.company"].create({"name": "Atlas Test Company"})

        cls.alice = new_test_user(
            cls.env,
            login="atlas-alice",
            groups="base.group_user,odoo_atlas.group_atlas_user",
            company_id=cls.company.id,
        )
        cls.bob = new_test_user(
            cls.env,
            login="atlas-bob",
            groups="base.group_user,odoo_atlas.group_atlas_user",
            company_id=cls.company.id,
        )
        cls.manager = new_test_user(
            cls.env,
            login="atlas-manager",
            groups="base.group_user,odoo_atlas.group_atlas_manager",
            company_id=cls.company.id,
        )

    def conversation_for(self, user, **values):
        """Create a conversation owned by ``user``, as ``user``."""
        return self.env["atlas.conversation"].with_user(user).create(values)

    def ask(self, conversation, content, **values):
        """Add a user-role message to ``conversation``, as its owner."""
        return (
            self.env["atlas.message"]
            .with_user(conversation.user_id)
            .create(
                {
                    "conversation_id": conversation.id,
                    "role": "user",
                    "content": content,
                    **values,
                }
            )
        )
