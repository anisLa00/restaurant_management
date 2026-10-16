from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import TransactionCase, tagged

from odoo.addons.restaurant_reception.models.reception_security import (
    MANAGER_GROUP,
    require_role,
)


@tagged("post_install", "-at_install")
class TestRestaurantManagement(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.owner_group = cls.env.ref("restaurant_core.group_restaurant_owner")
        cls.operations_group = cls.env.ref(
            "restaurant_core.group_restaurant_operations_manager"
        )
        cls.hr_group = cls.env.ref("restaurant_core.group_restaurant_hr")
        cls.accountant_group = cls.env.ref("restaurant_core.group_restaurant_accountant")
        cls.reception_group = cls.env.ref("restaurant_core.group_restaurant_reception")
        cls.users_group = cls.env.ref("base.group_user")

        def make_user(login, group):
            return cls.env["res.users"].with_context(no_reset_password=True).create(
                {
                    "name": login.title(),
                    "login": login,
                    "email": f"{login}@example.com",
                    "company_id": cls.company.id,
                    "company_ids": [Command.set(cls.company.ids)],
                    "group_ids": [Command.set((cls.users_group | group).ids)],
                }
            )

        cls.owner = make_user("management-owner", cls.owner_group)
        cls.operations = make_user("management-operations", cls.operations_group)
        cls.manager = make_user(
            "management-branch-manager",
            cls.env.ref("restaurant_core.group_restaurant_branch_manager"),
        )
        cls.hr = make_user("management-hr", cls.hr_group)
        cls.accountant = make_user("management-accountant", cls.accountant_group)
        cls.reception = make_user("management-reception", cls.reception_group)

    def test_01_standard_conversations_are_private_and_scoped(self):
        Channel = self.env["discuss.channel"].sudo()
        Channel._sync_restaurant_management_conversations()
        executive = Channel.search(
            [
                ("restaurant_management_kind", "=", "executive"),
                ("restaurant_company_id", "=", self.company.id),
            ],
            limit=1,
        )
        self.assertTrue(executive)
        self.assertEqual(executive.channel_type, "group")
        member_users = executive.channel_member_ids.partner_id.user_ids
        self.assertIn(self.owner, member_users)
        self.assertIn(self.operations, member_users)
        self.assertIn(self.hr, member_users)
        self.assertIn(self.accountant, member_users)
        self.assertNotIn(self.reception, member_users)
        owner_chat = Channel.search(
            [
                ("restaurant_management_kind", "=", "direct_owner"),
                ("channel_member_ids.partner_id", "=", self.owner.partner_id.id),
                ("channel_member_ids.partner_id", "=", self.hr.partner_id.id),
            ],
            limit=1,
        )
        self.assertTrue(owner_chat)
        self.assertEqual(owner_chat.channel_type, "chat")
        with self.assertRaises(AccessError):
            owner_chat.with_user(self.reception).check_access("read")
        with self.assertRaises(ValidationError):
            owner_chat.with_user(self.owner)._add_members(users=self.operations)

    def test_02_management_groups_reject_non_management_users(self):
        Channel = self.env["discuss.channel"].sudo()
        Channel._sync_restaurant_management_conversations()
        executive = Channel.search(
            [
                ("restaurant_management_kind", "=", "executive"),
                ("restaurant_company_id", "=", self.company.id),
            ],
            limit=1,
        )
        with self.assertRaises(ValidationError):
            executive.with_user(self.owner)._add_members(users=self.reception)

    def test_02b_only_owner_and_operations_create_scoped_management_groups(self):
        Channel = self.env["discuss.channel"]
        owner_group = Channel.with_user(self.owner)._create_group(
            self.hr | self.accountant,
            name="Owner Custom Management",
        )
        self.assertTrue(owner_group.restaurant_management_channel)
        operations_group = Channel.with_user(self.operations)._create_group(
            self.env["res.users"],
            name="Operations Custom Management",
        )
        operation_members = operations_group.channel_member_ids.partner_id.user_ids
        self.assertIn(self.operations, operation_members)
        self.assertIn(self.owner, operation_members)
        with self.assertRaises(AccessError):
            Channel.with_user(self.operations)._create_group(
                self.hr,
                name="Forbidden Operations Group",
            )
        with self.assertRaises(AccessError):
            Channel.with_user(self.hr)._create_group(
                self.accountant,
                name="Forbidden HR Group",
            )

    def test_03_dashboard_hides_sensitive_metrics_from_operations(self):
        Dashboard = self.env["restaurant.management.dashboard"]
        operations_dashboard = Dashboard.with_user(self.operations).create(
            {"company_id": self.company.id}
        )
        operations_values = operations_dashboard.read(
            ["is_owner", "payroll_pending_count", "supplier_invoice_pending_amount"]
        )[0]
        self.assertFalse(operations_values["is_owner"])
        self.assertEqual(operations_values["payroll_pending_count"], 0)
        self.assertEqual(operations_values["supplier_invoice_pending_amount"], 0)
        owner_dashboard = Dashboard.with_user(self.owner).create(
            {"company_id": self.company.id}
        )
        self.assertTrue(owner_dashboard.read(["is_owner"])[0]["is_owner"])

    def test_04_operations_drilldowns_are_read_only(self):
        dashboard = self.env["restaurant.management.dashboard"].with_user(
            self.operations
        ).create({"company_id": self.company.id})
        actions = (
            dashboard.action_open_daily_closing_reviews(),
            dashboard.action_open_attendance_reviews(),
            dashboard.action_open_waiter_sales_reviews(),
            dashboard.action_open_service_event_reviews(),
            dashboard.action_open_daily_stock_reviews(),
        )
        for action in actions:
            with self.subTest(model=action["res_model"]):
                self.assertFalse(action["context"]["create"])
                self.assertFalse(action["context"]["edit"])
                self.assertFalse(action["context"]["delete"])
                self.assertFalse(any(
                    domain[0] == "state"
                    for domain in action["domain"]
                    if isinstance(domain, (list, tuple)) and domain
                ))

        self.assertIn(
            ("closing_date", ">=", dashboard.date_from),
            actions[0]["domain"],
        )
        self.assertIn(
            ("closing_date", "<=", dashboard.date_to),
            actions[0]["domain"],
        )

    def test_05_management_roles_are_peers_with_scoped_review(self):
        branch_manager_group = self.env.ref(
            "restaurant_core.group_restaurant_branch_manager"
        )
        self.assertIn(self.users_group, self.operations_group.implied_ids)
        self.assertIn(self.users_group, self.owner_group.implied_ids)
        self.assertNotIn(self.operations_group, self.owner.all_group_ids)
        self.assertNotIn(branch_manager_group, self.operations.all_group_ids)
        with self.assertRaises(AccessError):
            require_role(self.env(user=self.operations.id), MANAGER_GROUP)
        with self.assertRaises(AccessError):
            require_role(self.env(user=self.owner.id), MANAGER_GROUP)

    def test_06_configuration_is_read_only_for_owner_and_operations(self):
        menu_setup = self.env.ref(
            'restaurant_stock_events.restaurant_branch_menu_setup_root'
        )
        for user in (self.owner, self.operations):
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    self.env["restaurant.stock.section"].with_user(user).check_access(
                        "create"
                    )
                with self.assertRaises(AccessError):
                    self.env["restaurant.menu.item"].with_user(user).check_access(
                        "create"
                    )
                with self.assertRaises(AccessError):
                    self.env["restaurant.shift.roster"].with_user(user).check_access(
                        "create"
                    )
                self.assertNotIn(
                    menu_setup.id,
                    self.env['ir.ui.menu'].with_user(user)._visible_menu_ids(),
                )

        self.assertNotIn(
            menu_setup.id,
            self.env['ir.ui.menu'].with_user(self.manager)._visible_menu_ids(),
        )
        for model_name in (
            'restaurant.menu.category',
            'restaurant.menu.flavor',
            'restaurant.menu.mapping.profile',
            'restaurant.menu.mapping.profile.line',
            'restaurant.menu.item',
            'restaurant.menu.variant',
            'restaurant.menu.stock.mapping',
        ):
            with self.subTest(model=model_name):
                with self.assertRaises(AccessError):
                    self.env[model_name].with_user(self.manager).check_access('create')

    def test_07_owner_receives_read_only_daily_attendance(self):
        menu = self.env.ref("restaurant_reception.restaurant_attendance_sheet_menu")
        self.assertIn(self.owner_group, menu.group_ids)
        self.assertIn(self.operations_group, menu.group_ids)
        for model_name in ('restaurant.attendance.sheet', 'restaurant.attendance.entry'):
            with self.subTest(model=model_name):
                model = self.env[model_name].with_user(self.owner)
                self.assertTrue(model.has_access('read'))
                self.assertFalse(model.has_access('create'))
                self.assertFalse(model.has_access('write'))
                self.assertFalse(model.has_access('unlink'))

    def test_07b_operations_does_not_receive_monthly_accounting_close_menu(self):
        menu = self.env.ref(
            "restaurant_accounting.restaurant_accounting_period_close_menu"
        )
        self.assertNotIn(self.operations_group, menu.group_ids)
        self.assertNotIn(
            menu.id,
            self.env["ir.ui.menu"].with_user(self.operations)._visible_menu_ids(),
        )
        self.assertIn(self.owner_group, menu.group_ids)

    def test_08_shift_roster_workflow_buttons_are_manager_only(self):
        view = self.env.ref('restaurant_reception.restaurant_shift_roster_view_form')
        for user in (self.operations, self.owner):
            with self.subTest(user=user.login):
                arch = self.env['restaurant.shift.roster'].with_user(user).get_view(
                    view_id=view.id,
                    view_type='form',
                )['arch']
                self.assertNotIn('name="action_populate_staff"', arch)
                self.assertNotIn('name="action_publish"', arch)
                self.assertNotIn('name="action_reopen"', arch)

        manager_arch = self.env['restaurant.shift.roster'].get_view(
            view_id=view.id,
            view_type='form',
        )['arch']
        self.assertIn('name="action_populate_staff"', manager_arch)
        self.assertIn('name="action_publish"', manager_arch)
        self.assertIn('name="action_reopen"', manager_arch)

    def test_09_operations_can_download_approved_daily_attendance_pdf(self):
        branch = self.env['restaurant.branch'].sudo().create({
            'name': 'Management PDF Branch',
            'code': 'MGMT-PDF',
            'company_id': self.company.id,
            'user_ids': [Command.set(self.reception.ids)],
        })
        employee = self.env['hr.employee'].sudo().create({
            'name': 'Attendance PDF Employee',
            'company_id': self.company.id,
            'restaurant_branch_id': branch.id,
        })
        sheet = self.env['restaurant.attendance.sheet'].with_user(
            self.reception
        ).create({
            'branch_id': branch.id,
            'attendance_date': '2026-10-13',
        })
        line = self.env['restaurant.attendance.entry'].with_user(
            self.reception
        ).create({
            'sheet_id': sheet.id,
            'employee_id': employee.id,
            'branch_id': branch.id,
            'attendance_date': sheet.attendance_date,
            'status': 'day_off',
        })
        self.env.cr.execute(
            "UPDATE restaurant_attendance_sheet SET state = 'approved' WHERE id = %s",
            [sheet.id],
        )
        self.env.cr.execute(
            "UPDATE restaurant_attendance_entry SET state = 'approved' WHERE id = %s",
            [line.id],
        )
        sheet.invalidate_recordset(['state', 'line_ids'])
        line.invalidate_recordset(['state'])

        for user in (self.operations, self.owner):
            with self.subTest(user=user.login):
                action = sheet.with_user(user).action_download_official_pdf()
                attachment_id = int(action['url'].split('/')[3].split('?')[0])
                attachment = self.env['ir.attachment'].sudo().browse(attachment_id)
                self.assertEqual(attachment.mimetype, 'application/pdf')
                self.assertTrue(attachment.raw.content.startswith(b'%PDF'))
