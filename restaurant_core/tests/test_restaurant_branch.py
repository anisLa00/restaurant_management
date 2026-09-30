from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from odoo.addons.base.tests.common import BaseCommon


class TestRestaurantBranch(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other_company = cls.env['res.company'].create({'name': 'Other Restaurant Company'})
        cls.branches = cls.env['restaurant.branch'].create([
            {'name': 'Downtown', 'code': 'DT', 'company_id': cls.company.id},
            {'name': 'Marina', 'code': 'MA', 'company_id': cls.company.id},
            {'name': 'Other Company Branch', 'code': 'DT', 'company_id': cls.other_company.id},
        ])
        cls.branch, cls.unassigned_branch, cls.other_branch = cls.branches
        cls.staff = new_test_user(
            cls.env, login='restaurant_staff',
            groups='restaurant_core.group_restaurant_reception',
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
            restaurant_branch_ids=[Command.set(cls.branch.ids)],
        )
        cls.manager = new_test_user(
            cls.env, login='restaurant_manager',
            groups='restaurant_core.group_restaurant_branch_manager',
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )
        cls.outsider = new_test_user(
            cls.env, login='restaurant_outsider', groups='base.group_user',
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )
        cls.portal = new_test_user(
            cls.env, login='restaurant_portal', groups='base.group_portal',
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)],
        )

    def _as_user(self, user):
        return self.env['restaurant.branch'].with_user(user).with_context(
            allowed_company_ids=[self.company.id],
        )

    def test_staff_only_reads_assigned_branches(self):
        branches = self._as_user(self.staff)
        self.assertEqual(branches.search([]), self.branch.with_env(branches.env))
        self.assertEqual(branches.browse(self.branch.id).name, 'Downtown')
        with self.assertRaises(AccessError):
            branches.browse(self.unassigned_branch.id).read(['name'])
        for operation in (
            lambda: branches.create({'name': 'Forbidden', 'code': 'F'}),
            lambda: branches.browse(self.branch.id).write({'name': 'Forbidden'}),
            lambda: branches.browse(self.branch.id).unlink(),
        ):
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                operation()

    def test_manager_crud_and_archive(self):
        branches = self._as_user(self.manager)
        self.assertFalse(branches.search([]))
        self.manager.restaurant_branch_ids = [Command.set(self.branch.ids)]
        self.assertEqual(branches.search([]), self.branch.with_env(branches.env))
        with self.assertRaises(AccessError):
            branches.browse(self.unassigned_branch.id).read(['name'])
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            branches.create({'name': 'Unassigned Airport', 'code': 'AIR'})
        branch = branches.browse(self.branch.id)
        branch.write({'name': 'Airport Terminal'})
        self.assertEqual(branch.name, 'Airport Terminal')
        branch.action_archive()
        self.assertNotIn(branch, branches.search([]))
        self.assertIn(branch, branches.with_context(active_test=False).search([]))
        branch.action_unarchive()
        branch.unlink()
        self.assertFalse(branch.exists())

    def test_company_restriction_all_operations(self):
        branches = self._as_user(self.manager)
        other = branches.browse(self.other_branch.id)
        operations = (
            lambda: other.read(['name']),
            lambda: other.write({'name': 'Forbidden'}),
            lambda: other.unlink(),
            lambda: branches.create({
                'name': 'Forbidden', 'code': 'F', 'company_id': self.other_company.id,
            }),
            lambda: branches.browse(self.unassigned_branch.id).write({
                'company_id': self.other_company.id,
            }),
        )
        for operation in operations:
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                operation()
        with self.assertRaises(AccessError):
            branches.with_context(allowed_company_ids=[self.other_company.id]).search([])

    def test_no_restaurant_group_denied(self):
        for user in (self.outsider, self.portal, self.env.ref('base.public_user')):
            with self.assertRaises(AccessError):
                self._as_user(user).search([])

    def test_cannot_self_assign_or_change_branch_membership(self):
        for user in (self.staff, self.manager):
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                user.with_user(user).write({
                    'restaurant_branch_ids': [Command.link(self.unassigned_branch.id)],
                })
            with self.assertRaises(AccessError), self.env.cr.savepoint():
                self._as_user(user).browse(self.branch.id).write({
                    'user_ids': [Command.link(user.id)],
                })

    def test_assignment_changes_take_effect_immediately(self):
        branches = self._as_user(self.staff)
        self.assertEqual(branches.search([]).ids, self.branch.ids)
        self.staff.write({'restaurant_branch_ids': [Command.set(self.unassigned_branch.ids)]})
        self.assertEqual(branches.search([]).ids, self.unassigned_branch.ids)
        with self.assertRaises(AccessError):
            branches.browse(self.branch.id).read(['name'])
        self.staff.write({'restaurant_branch_ids': [Command.clear()]})
        self.assertFalse(branches.search([]))

    def test_assignment_company_consistency(self):
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.staff.write({'restaurant_branch_ids': [Command.link(self.other_branch.id)]})
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.other_branch.write({'user_ids': [Command.link(self.staff.id)]})
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.branch.write({'company_id': self.other_company.id})
        self.staff.write({
            'company_ids': [Command.link(self.other_company.id)],
            'restaurant_branch_ids': [Command.link(self.other_branch.id)],
        })
        self.other_branch.active = False
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.staff.write({'company_ids': [Command.set(self.company.ids)]})

    def test_selected_companies_limit_assigned_branches(self):
        self.staff.write({
            'company_ids': [Command.link(self.other_company.id)],
            'restaurant_branch_ids': [Command.link(self.other_branch.id)],
        })
        branches = self._as_user(self.staff)
        self.assertEqual(branches.search([]).ids, self.branch.ids)
        self.assertEqual(
            branches.with_context(allowed_company_ids=[self.other_company.id]).search([]).ids,
            self.other_branch.ids,
        )

    @mute_logger('odoo.sql_db')
    def test_branch_required_and_unique_values(self):
        for values in (
            {'name': 'Duplicate', 'code': 'DT'},
            {'name': '   ', 'code': 'BLANK'},
            {'name': 'Blank Code', 'code': '   '},
            {'code': 'MISSING'},
            {'name': 'Missing Code'},
        ):
            with self.assertRaises(IntegrityError), self.env.cr.savepoint():
                self.env['restaurant.branch'].create(dict(values, company_id=self.company.id))
