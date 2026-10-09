from datetime import date

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tools import mute_logger

from odoo.addons.base.tests.common import BaseCommon


class TestRestaurantRoleSecurity(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.branches = {
            'MIR': cls.env.ref('restaurant_core.branch_mirdif'),
            'JAF': cls.env.ref('restaurant_core.branch_jafilya'),
            'GV': cls.env.ref('restaurant_core.branch_global_village'),
        }
        cls.receptions = {
            'MIR': cls.env.ref('restaurant_core.user_mirdif_reception'),
            'JAF': cls.env.ref('restaurant_core.user_jafilya_reception'),
            'GV': cls.env.ref('restaurant_core.user_global_village_reception'),
        }
        cls.managers = {
            'MIR': cls.env.ref('restaurant_core.user_mirdif_manager'),
            'JAF': cls.env.ref('restaurant_core.user_jafilya_manager'),
            'GV': cls.env.ref('restaurant_core.user_global_village_manager'),
        }
        cls.owner = cls.env.ref('restaurant_core.user_owner_test')
        cls.operations = cls.env.ref('restaurant_core.user_operations_test')
        cls.hr = cls.env.ref('restaurant_core.user_hr_test')
        cls.purchasing = cls.env.ref('restaurant_core.user_purchasing_test')
        cls.accountant = cls.env.ref('restaurant_core.user_accountant_test')
        cls.employees = {
            code: cls.env['hr.employee'].create({
                'name': f'Role Security Waiter {code}',
                'company_id': cls.company.id,
                'restaurant_immigration_status': False,
                'restaurant_work_authorized': True,
            })
            for code in cls.branches
        }
        cls.today = date(2099, 1, 1)
        cls.company.restaurant_hr_official_sync_enabled = True

        # These tests isolate Reception role boundaries on a synthetic future
        # date. Stock-section closing integration has its own focused tests;
        # do not make these reception-only assertions create future stock data.
        if cls.env.registry.get('restaurant.stock.section'):
            cls.env['restaurant.stock.section'].sudo().with_context(
                restaurant_system_section_sync=True,
            ).search([
                ('branch_id', 'in', [
                    branch.id for branch in cls.branches.values()
                ]),
            ]).write({'required_for_daily_close': False})

    def _model(self, name, user):
        return self.env[name].with_user(user).with_context(
            allowed_company_ids=[self.company.id],
            tracking_disable=True,
            mail_notrack=True,
        )

    def _closing(self, code, shift='full_day'):
        closing = self._model(
            'restaurant.daily.closing', self.receptions[code],
        ).create({
            'branch_id': self.branches[code].id,
            'closing_date': self.today,
            'shift': shift,
        })
        closing.sudo().with_context(service_tracking_migration=True).write({
            'service_tracking_mode': 'legacy',
        })
        return closing

    def _line(self, code, closing):
        return self._model('restaurant.waiter.daily.line', self.receptions[code]).create({
            'closing_id': closing.id,
            'employee_id': self.employees[code].id,
            'sales_amount': 100,
            'tips_amount': 10,
            'order_count': 5,
            'table_count': 3,
        })

    def _attendance(self, code):
        return self._model('restaurant.attendance.entry', self.receptions[code]).create({
            'branch_id': self.branches[code].id,
            'employee_id': self.employees[code].id,
            'attendance_date': self.today,
            'check_in': '2099-01-01 08:00:00',
            'check_out': '2099-01-01 16:00:00',
            'overtime_hours': 2,
        })

    def test_01_provisioned_identity_group_graph_and_defaults(self):
        all_branches = self.env['restaurant.branch'].browse(
            [branch.id for branch in self.branches.values()]
        )
        system = self.env.ref('base.group_system')
        manager_group = self.env.ref('restaurant_core.group_restaurant_branch_manager')
        reception_group = self.env.ref('restaurant_core.group_restaurant_reception')
        self.assertNotIn(reception_group, manager_group.all_implied_ids)
        self.assertEqual(set(self.owner.restaurant_branch_ids.ids), set(all_branches.ids))
        self.assertEqual(set(self.operations.restaurant_branch_ids.ids), set(all_branches.ids))
        for code in self.branches:
            for user in (self.receptions[code], self.managers[code]):
                self.assertEqual(user.restaurant_branch_ids, self.branches[code])
                self.assertEqual(user.default_restaurant_branch_id, self.branches[code])
                self.assertNotIn(system, user.all_group_ids)
        for user in (
            self.owner, self.operations, self.hr, self.purchasing, self.accountant,
        ):
            self.assertNotIn(system, user.all_group_ids)

    def test_hr_role_manages_employees_and_time_off_without_system_admin(self):
        hr_group = self.env.ref('restaurant_core.group_restaurant_hr')
        employee_manager = self.env.ref('hr.group_hr_manager')
        time_off_manager = self.env.ref('hr_holidays.group_hr_holidays_manager')
        system = self.env.ref('base.group_system')

        self.assertIn(employee_manager, hr_group.all_implied_ids)
        self.assertIn(time_off_manager, hr_group.all_implied_ids)
        self.assertIn(employee_manager, self.hr.all_group_ids)
        self.assertIn(time_off_manager, self.hr.all_group_ids)
        self.assertNotIn(system, self.hr.all_group_ids)

    def test_hr_reads_but_only_system_admin_manages_staff_categories(self):
        category_model = self.env['restaurant.staff.category']
        category = category_model.create({
            'name': 'Admin Controlled Category',
        })

        hr_category = category.with_user(self.hr)
        self.assertEqual(hr_category.name, 'Admin Controlled Category')
        with self.assertRaises(AccessError):
            self._model('restaurant.staff.category', self.hr).create({
                'name': 'HR Must Not Create Category',
            })
        with self.assertRaises(AccessError):
            hr_category.write({'active': False})
        with self.assertRaises(AccessError):
            hr_category.unlink()

        menu = self.env.ref(
            'restaurant_reception.restaurant_hr_staff_categories_menu'
        )
        self.assertEqual(menu.group_ids, self.env.ref('base.group_system'))

    def test_02_mirdif_reception_workflow_and_boundaries(self):
        closing = self._closing('MIR')
        closing.notes = 'Draft corrected by Reception.'
        line = self._line('MIR', closing)
        line.sales_amount = 125
        self.assertEqual(closing.total_waiter_sales, 125)
        closing.action_submit()
        self.assertEqual(closing.state, 'manager_review')
        with self.assertRaises(AccessError):
            closing.action_confirm()
        for code in ('JAF', 'GV'):
            foreign = self._closing(code)
            self.assertFalse(self._model(
                foreign._name, self.receptions['MIR'],
            ).search([('id', '=', foreign.id)]))
            with self.assertRaises(AccessError):
                foreign.with_user(self.receptions['MIR']).read(['name'])
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                self._model('restaurant.daily.closing', self.receptions['MIR']).create({
                    'branch_id': self.branches[code].id,
                    'closing_date': date(2099, 1, 2),
                    'shift': 'morning',
                })

    def test_03_mirdif_manager_review_permissions(self):
        closing = self._closing('MIR')
        line = self._line('MIR', closing)
        manager_line = line.with_user(self.managers['MIR'])
        with self.assertRaises(AccessError):
            manager_line.write({'sales_amount': 999})
        closing.action_submit()
        manager_closing = closing.with_user(self.managers['MIR'])
        manager_closing.correction_reason = 'Correct the submitted report.'
        manager_closing.action_return_to_draft()
        closing.with_user(self.receptions['MIR']).action_submit()
        manager_closing.action_confirm()
        self.assertEqual(closing.state, 'confirmed')
        foreign = self._closing('JAF')
        foreign.action_submit()
        with self.assertRaises(AccessError):
            foreign.with_user(self.managers['MIR']).action_confirm()

    def test_04_attendance_three_stage_separation(self):
        entry = self._attendance('MIR')
        with self.assertRaises(UserError):
            entry.action_submit_to_hr()
        entry.action_submit()
        with self.assertRaises(AccessError):
            entry.action_submit_to_hr()
        entry.with_user(self.managers['MIR']).action_submit_to_hr()
        with self.assertRaises(AccessError):
            entry.with_user(self.managers['MIR']).action_approve()
        entry.with_user(self.hr).action_approve()
        self.assertEqual(entry.state, 'approved')

        rejected = self._attendance('JAF')
        rejected.action_submit()
        rejected.with_user(self.managers['JAF']).action_submit_to_hr()
        rejected_hr = rejected.with_user(self.hr)
        rejected_hr.decision_reason = 'Rejected after HR verification.'
        rejected_hr.action_reject()
        self.assertEqual(rejected.state, 'rejected')

    def test_05_manager_hr_cannot_approve_own_manager_workflow(self):
        manager = self.managers['MIR']
        hr_group = self.env.ref('restaurant_core.group_restaurant_hr')
        manager.write({'group_ids': [Command.link(hr_group.id)]})
        entry = self._attendance('MIR')
        entry.action_submit()
        entry.with_user(manager).action_submit_to_hr()
        dual_role = entry.with_user(manager)
        with self.assertRaises(AccessError):
            dual_role.write({'hr_note': 'Self approval attempt.'})
        with self.assertRaises(AccessError):
            dual_role.action_approve()
        with self.assertRaises(AccessError):
            dual_role.action_reject()
        manager.write({
            'group_ids': [Command.unlink(
                self.env.ref('restaurant_core.group_restaurant_branch_manager').id,
            )],
        })
        with self.assertRaises(AccessError):
            entry.with_user(manager).action_approve()

    def test_06_owner_operations_global_visibility_and_write_envelope(self):
        closings = self.env['restaurant.daily.closing']
        lines = self.env['restaurant.waiter.daily.line']
        entries = self.env['restaurant.attendance.entry']
        for code in self.branches:
            closing = self._closing(code)
            lines |= self._line(code, closing).sudo()
            closing.action_submit()
            closings |= closing.sudo()
            entry = self._attendance(code)
            entry.action_submit()
            entries |= entry.sudo()

        for user in (self.owner, self.operations):
            self.assertEqual(set(self._model(
                'restaurant.daily.closing', user,
            ).search([('id', 'in', closings.ids)]).ids), set(closings.ids))
            self.assertEqual(set(self._model(
                'restaurant.waiter.daily.line', user,
            ).search([('id', 'in', lines.ids)]).ids), set(lines.ids))
            self.assertEqual(set(self._model(
                'restaurant.attendance.entry', user,
            ).search([('id', 'in', entries.ids)]).ids), set(entries.ids))
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                self._model('restaurant.daily.closing', user).create({
                    'branch_id': self.branches['MIR'].id,
                    'closing_date': date(2099, 1, 2),
                })
            with self.assertRaises(AccessError):
                lines[0].with_user(user).write({'sales_amount': 999})

        closings[0].with_user(self.owner).action_confirm()
        operations_closing = closings[1].with_user(self.operations)
        operations_closing.correction_reason = 'Correct operations review.'
        operations_closing.action_return_to_draft()
        entries[0].with_user(self.owner).action_submit_to_hr()
        entries[1].with_user(self.operations).action_return_to_draft()

    def test_07_three_branch_pairwise_isolation(self):
        closings = {
            code: self._closing(code)
            for code in self.branches
        }
        for users in (self.receptions, self.managers):
            for code, user in users.items():
                branch_model = self._model('restaurant.branch', user)
                self.assertEqual(branch_model.search([]), self.branches[code])
                closing_model = self._model('restaurant.daily.closing', user)
                self.assertEqual(
                    closing_model.search([('id', 'in', [r.id for r in closings.values()])]),
                    closings[code].with_env(closing_model.env),
                )
                for foreign_code in set(self.branches) - {code}:
                    with self.assertRaises(AccessError):
                        closings[foreign_code].with_user(user).read(['name'])

    def test_08_hr_visibility_is_limited_to_hr_stage(self):
        entry = self._attendance('MIR')
        self.assertFalse(self._model(entry._name, self.hr).search([('id', '=', entry.id)]))
        entry.action_submit()
        self.assertFalse(self._model(entry._name, self.hr).search([('id', '=', entry.id)]))
        entry.with_user(self.managers['MIR']).action_submit_to_hr()
        self.assertEqual(
            self._model(entry._name, self.hr).search([('id', '=', entry.id)]).ids,
            entry.ids,
        )
        with self.assertRaises(AccessError):
            self._model('restaurant.daily.closing', self.hr).search([])

    def test_09_purchasing_accountant_have_no_reception_access(self):
        for user in (self.purchasing, self.accountant):
            branch_ids = [branch.id for branch in self.branches.values()]
            self.assertEqual(
                set(self._model('restaurant.branch', user).search([
                    ('id', 'in', branch_ids),
                ]).ids),
                set(branch_ids),
            )
            for model_name in (
                'restaurant.daily.closing',
                'restaurant.waiter.service.entry',
                'restaurant.waiter.daily.line',
                'restaurant.attendance.entry',
            ):
                with self.assertRaises(AccessError):
                    self._model(model_name, user).search([])

    @mute_logger('odoo.sql_db')
    def test_10_admin_keeps_no_restaurant_business_role(self):
        admin = self.env.ref('base.user_admin')
        restaurant_groups = self.env['res.groups'].search([
            ('privilege_id', '=', self.env.ref(
                'restaurant_core.res_groups_privilege_restaurant',
            ).id),
        ])
        self.assertFalse(admin.all_group_ids & restaurant_groups)
