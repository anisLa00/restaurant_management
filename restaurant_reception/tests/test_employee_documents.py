from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon


class TestRestaurantEmployeeDocuments(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.hr = new_test_user(
            cls.env,
            login='employee_document_hr',
            groups='restaurant_core.group_restaurant_hr',
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
        )
        cls.reception = new_test_user(
            cls.env,
            login='employee_document_reception',
            groups='restaurant_core.group_restaurant_reception',
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
        )
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Document Test Employee',
            'company_id': cls.company.id,
            'hr_responsible_id': cls.hr.id,
        })
        cls.document_type = cls.env['restaurant.employee.document.type'].create({
            'name': 'Document Test Type',
            'code': 'document_test_type',
            'alert_days': 30,
        })
        cls.model = cls.env['restaurant.employee.document'].with_user(cls.hr).with_context(
            allowed_company_ids=[cls.company.id],
            tracking_disable=True,
            mail_notrack=True,
        )

    def _document(self, expiry_date, **values):
        document_values = {
            'employee_id': self.employee.id,
            'document_type_id': self.document_type.id,
            'document_number': 'DOC-001',
            'issue_date': fields.Date.today() - timedelta(days=30),
            'expiry_date': expiry_date,
            'document_file': 'dGVzdA==',
            'document_filename': 'document.pdf',
        }
        document_values.update(values)
        return self.model.create(document_values)

    def test_document_status_and_employee_smart_count(self):
        missing_before = self.employee.restaurant_missing_document_count
        self.assertIn(
            self.document_type,
            self.employee.restaurant_missing_document_type_ids,
        )
        valid = self._document(fields.Date.today() + timedelta(days=90))
        expiring = self._document(
            fields.Date.today() + timedelta(days=10),
            document_number='DOC-002',
        )
        expired = self._document(
            fields.Date.today() - timedelta(days=1),
            document_number='DOC-003',
            issue_date=fields.Date.today() - timedelta(days=60),
        )
        self.assertEqual(valid.status, 'valid')
        self.assertEqual(expiring.status, 'expiring')
        self.assertEqual(expired.status, 'expired')
        self.assertEqual(self.employee.restaurant_document_count, 3)
        self.assertEqual(
            self.employee.restaurant_document_compliance_state,
            'in_progress',
        )
        self.assertEqual(
            self.employee.restaurant_missing_document_count,
            missing_before - 1,
        )
        self.assertNotIn(
            self.document_type,
            self.employee.restaurant_missing_document_type_ids,
        )
        action = self.employee.action_open_restaurant_documents()
        self.assertEqual(action['domain'], [('employee_id', '=', self.employee.id)])

    def test_document_date_and_expiry_requirements(self):
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._document(
                fields.Date.today() - timedelta(days=10),
                issue_date=fields.Date.today(),
            )
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self._document(False)

        self.document_type.expiry_required = False
        no_expiry = self._document(False)
        self.assertEqual(no_expiry.status, 'no_expiry')

    def test_expiry_cron_creates_one_hr_activity_and_resets_on_renewal(self):
        document = self._document(fields.Date.today() + timedelta(days=10))
        self.env['restaurant.employee.document']._cron_check_document_expiry()
        self.assertTrue(document.expiry_activity_id)
        self.assertEqual(document.expiry_activity_id.user_id, self.hr)
        activity = document.expiry_activity_id

        self.env['restaurant.employee.document']._cron_check_document_expiry()
        self.assertEqual(document.expiry_activity_id, activity)

        document.write({'expiry_date': fields.Date.today() + timedelta(days=365)})
        self.assertFalse(activity.exists())
        self.assertFalse(document.expiry_activity_id)
        self.assertEqual(document.status, 'valid')

    def test_documents_are_confidential_to_hr(self):
        document = self._document(fields.Date.today() + timedelta(days=90))
        with self.assertRaises(AccessError):
            document.with_user(self.reception).read(['document_number'])
        with self.assertRaises(AccessError), self.env.cr.savepoint():
            self.env['restaurant.employee.document'].with_user(self.reception).create({
                'employee_id': self.employee.id,
                'document_type_id': self.document_type.id,
                'expiry_date': fields.Date.today() + timedelta(days=90),
                'document_file': 'dGVzdA==',
            })
