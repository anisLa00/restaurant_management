from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, ValidationError


OWNER_GROUP = "restaurant_core.group_restaurant_owner"
OPERATIONS_GROUP = "restaurant_core.group_restaurant_operations_manager"
BRANCH_MANAGER_GROUP = "restaurant_core.group_restaurant_branch_manager"
HR_GROUP = "restaurant_core.group_restaurant_hr"
ACCOUNTANT_GROUP = "restaurant_core.group_restaurant_accountant"
MANAGEMENT_GROUPS = (
    OWNER_GROUP,
    OPERATIONS_GROUP,
    BRANCH_MANAGER_GROUP,
    HR_GROUP,
    ACCOUNTANT_GROUP,
)
MANAGEMENT_SYNC_CONTEXT = "restaurant_management_conversation_sync"


class DiscussChannel(models.Model):
    _inherit = "discuss.channel"

    restaurant_management_channel = fields.Boolean(
        string="Restaurant Management Conversation",
        default=False,
        copy=False,
        index=True,
    )
    restaurant_management_kind = fields.Selection(
        [
            ("direct_owner", "Direct with Owner"),
            ("executive", "Executive Management"),
            ("operations", "Operations Management"),
            ("branch", "Branch Management"),
            ("custom", "Management Group"),
        ],
        copy=False,
        index=True,
    )
    restaurant_company_id = fields.Many2one(
        "res.company",
        string="Restaurant Company",
        copy=False,
        index=True,
        ondelete="cascade",
    )
    restaurant_branch_id = fields.Many2one(
        "restaurant.branch",
        string="Restaurant Branch",
        copy=False,
        index=True,
        ondelete="cascade",
    )

    @api.model
    def _restaurant_group(self, xmlid):
        return self.env.ref(xmlid, raise_if_not_found=False)

    @api.model
    def _users_with_direct_groups(self, xmlids):
        groups = self.env["res.groups"]
        for xmlid in xmlids:
            group = self._restaurant_group(xmlid)
            if group:
                groups |= group
        return groups.mapped("user_ids").filtered(
            lambda user: user.active and not user.share
        )

    @api.model
    def _is_direct_role(self, user, xmlids):
        group_ids = {
            group.id
            for xmlid in xmlids
            if (group := self._restaurant_group(xmlid))
        }
        return bool(group_ids.intersection(user.group_ids.ids))

    @api.model
    def _is_management_user(self, user):
        return self._is_direct_role(user, MANAGEMENT_GROUPS)

    def _allowed_management_users(self):
        self.ensure_one()
        company = self.restaurant_company_id
        if self.restaurant_management_kind == "executive":
            users = self._users_with_direct_groups(
                (OWNER_GROUP, OPERATIONS_GROUP, HR_GROUP, ACCOUNTANT_GROUP)
            )
        elif self.restaurant_management_kind == "operations":
            users = self._users_with_direct_groups(
                (OWNER_GROUP, OPERATIONS_GROUP, BRANCH_MANAGER_GROUP)
            )
        elif self.restaurant_management_kind == "branch":
            users = self._users_with_direct_groups(
                (OWNER_GROUP, OPERATIONS_GROUP)
            )
            if self.restaurant_branch_id:
                managers = self._users_with_direct_groups((BRANCH_MANAGER_GROUP,))
                users |= managers.filtered(
                    lambda user: self.restaurant_branch_id in user.restaurant_branch_ids
                )
        elif self.restaurant_management_kind == "direct_owner":
            users = self._users_with_direct_groups(MANAGEMENT_GROUPS)
        else:
            users = self._users_with_direct_groups(MANAGEMENT_GROUPS)
        if company:
            users = users.filtered(lambda user: company in user.company_ids)
        return users

    def _check_management_partners(self, partners):
        for channel in self.filtered("restaurant_management_channel"):
            if channel.restaurant_management_kind == "direct_owner" and partners:
                existing_partners = channel.channel_member_ids.partner_id
                if partners - existing_partners:
                    raise ValidationError(
                        self.env._(
                            "Direct Owner conversations are private one-to-one chats."
                        )
                    )
            users = partners.mapped("user_ids").filtered(
                lambda user: user.active and not user.share
            )
            if len(users) != len(partners):
                raise ValidationError(
                    self.env._("Management conversations can contain internal management users only.")
                )
            if users - channel._allowed_management_users():
                raise ValidationError(
                    self.env._("One or more selected users are outside this management conversation scope.")
                )

    def _add_members(self, *, partners=None, users=None, **kwargs):
        partners = (partners or self.env["res.partner"]) | (
            users.partner_id if users else self.env["res.partner"]
        )
        if not self.env.context.get(MANAGEMENT_SYNC_CONTEXT):
            guests = kwargs.get("guests") or self.env["mail.guest"]
            if guests and self.filtered("restaurant_management_channel"):
                raise ValidationError(
                    self.env._("Guests cannot join Restaurant Management conversations.")
                )
            self._check_management_partners(partners)
        return super()._add_members(partners=partners, **kwargs)

    @api.model
    def _sync_group_channel(self, company, kind, name, users, branch=None):
        users = users.filtered(
            lambda user: user.active and not user.share and company in user.company_ids
        )
        owners = users.filtered(lambda user: self._is_direct_role(user, (OWNER_GROUP,)))
        if not owners or len(users) < 2:
            return self.env["discuss.channel"]
        domain = [
            ("restaurant_management_channel", "=", True),
            ("restaurant_management_kind", "=", kind),
            ("restaurant_company_id", "=", company.id),
            ("restaurant_branch_id", "=", branch.id if branch else False),
        ]
        channel = self.sudo().with_context(active_test=False).search(domain, limit=1)
        if not channel:
            owner = owners.sorted("id")[:1]
            channel = self.with_user(owner).with_context(
                **{MANAGEMENT_SYNC_CONTEXT: True}
            ).create(
                {
                    "name": name,
                    "channel_type": "group",
                    "channel_member_ids": [
                        Command.create(
                            {
                                "partner_id": user.partner_id.id,
                                "channel_role": "owner" if user == owner else None,
                            }
                        )
                        for user in users
                    ],
                    "restaurant_management_channel": True,
                    "restaurant_management_kind": kind,
                    "restaurant_company_id": company.id,
                    "restaurant_branch_id": branch.id if branch else False,
                }
            )
        else:
            current_users = channel.channel_member_ids.partner_id.user_ids.filtered(
                lambda user: user.active and not user.share
            )
            missing = users - current_users
            if missing:
                channel.with_context(**{MANAGEMENT_SYNC_CONTEXT: True})._add_members(
                    users=missing,
                    post_joined_message=False,
                )
            extra_members = channel.channel_member_ids.filtered(
                lambda member: member.partner_id.user_ids
                and not (member.partner_id.user_ids & users)
            )
            if extra_members:
                extra_members.with_context(
                    **{MANAGEMENT_SYNC_CONTEXT: True},
                    close_chat_window=True,
                ).unlink()
            owner_member = channel.channel_member_ids.filtered(
                lambda member: member.partner_id in owners.partner_id
            ).sorted("id")[:1]
            if owner_member and owner_member.channel_role != "owner":
                owner_member.sudo().write({"channel_role": "owner"})
        return channel

    @api.model
    def _sync_direct_owner_chats(self):
        owners = self._users_with_direct_groups((OWNER_GROUP,))
        managers = self._users_with_direct_groups(
            (OPERATIONS_GROUP, BRANCH_MANAGER_GROUP, HR_GROUP, ACCOUNTANT_GROUP)
        )
        for owner in owners:
            peers = managers.filtered(
                lambda user: bool(user.company_ids & owner.company_ids) and user != owner
            )
            for peer in peers:
                channel = self.with_user(owner)._get_or_create_chat([peer.partner_id.id])
                channel.sudo().write(
                    {
                        "restaurant_management_channel": True,
                        "restaurant_management_kind": "direct_owner",
                    }
                )

    @api.model
    def _sync_restaurant_management_conversations(self):
        self = self.sudo().with_context(**{MANAGEMENT_SYNC_CONTEXT: True})
        self._sync_direct_owner_chats()
        companies = self.env["res.company"].search([])
        owners = self._users_with_direct_groups((OWNER_GROUP,))
        operations = self._users_with_direct_groups((OPERATIONS_GROUP,))
        branch_managers = self._users_with_direct_groups((BRANCH_MANAGER_GROUP,))
        hr_users = self._users_with_direct_groups((HR_GROUP,))
        accountants = self._users_with_direct_groups((ACCOUNTANT_GROUP,))
        for company in companies:
            company_users = lambda users: users.filtered(
                lambda user: company in user.company_ids
            )
            self._sync_group_channel(
                company,
                "executive",
                self.env._("Executive Management - %s", company.display_name),
                company_users(owners | operations | hr_users | accountants),
            )
            self._sync_group_channel(
                company,
                "operations",
                self.env._("Operations Management - %s", company.display_name),
                company_users(owners | operations | branch_managers),
            )
            branches = self.env["restaurant.branch"].search(
                [("company_id", "=", company.id), ("active", "=", True)]
            )
            for branch in branches:
                assigned_managers = branch_managers.filtered(
                    lambda user: branch in user.restaurant_branch_ids
                )
                self._sync_group_channel(
                    company,
                    "branch",
                    self.env._("Branch Management - %s", branch.display_name),
                    company_users(owners | operations | assigned_managers),
                    branch=branch,
                )
        return True

    @api.model
    def _create_group(self, users_to, default_display_mode=False, name=""):
        users_to = users_to.with_env(self.env)
        current_is_management = self._is_management_user(self.env.user)
        if current_is_management:
            is_owner = self._is_direct_role(self.env.user, (OWNER_GROUP,))
            is_operations = self._is_direct_role(
                self.env.user, (OPERATIONS_GROUP,)
            )
            if not (is_owner or is_operations):
                raise AccessError(
                    self.env._(
                        "Only the Owner or Operations can create management groups."
                    )
                )
            allowed_groups = (
                MANAGEMENT_GROUPS
                if is_owner
                else (OWNER_GROUP, OPERATIONS_GROUP, BRANCH_MANAGER_GROUP)
            )
            if any(
                not self._is_direct_role(user, allowed_groups)
                for user in users_to | self.env.user
            ):
                raise AccessError(
                    self.env._(
                        "This management group contains a role outside its allowed scope."
                    )
                )
            if is_operations:
                owners = self._users_with_direct_groups((OWNER_GROUP,)).filtered(
                    lambda user: bool(user.company_ids & self.env.user.company_ids)
                )
                users_to |= owners
        channel = super()._create_group(
            users_to,
            default_display_mode=default_display_mode,
            name=name,
        )
        if current_is_management:
            channel.sudo().write(
                {
                    "restaurant_management_channel": True,
                    "restaurant_management_kind": "custom",
                }
            )
        return channel


class DiscussChannelMember(models.Model):
    _inherit = "discuss.channel.member"

    @api.model_create_multi
    def create(self, vals_list):
        if not self.env.context.get(MANAGEMENT_SYNC_CONTEXT):
            for vals in vals_list:
                channel = self.env["discuss.channel"].browse(vals.get("channel_id")).sudo()
                if not channel.restaurant_management_channel:
                    continue
                if vals.get("guest_id"):
                    raise ValidationError(
                        self.env._("Guests cannot join Restaurant Management conversations.")
                    )
                partner = self.env["res.partner"].browse(vals.get("partner_id"))
                channel._check_management_partners(partner)
        return super().create(vals_list)
