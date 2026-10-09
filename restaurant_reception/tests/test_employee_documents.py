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
        cls.employee.restaurant_immigration_status = False
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

    def test_bulk_upload_wizard_saves_multiple_documents_once(self):
        other_type = self.env['restaurant.employee.document.type'].create({
            'name': 'Second Test Type',
            'code': 'second_document_test_type',
            'alert_days': 30,
        })
        action = self.employee.with_user(self.hr).action_open_document_upload_wizard()
        wizard = self.env[action['res_model']].browse(action['res_id'])
        first_line = wizard.line_ids.filtered(
            lambda line: line.document_type_id == self.document_type
        )
        second_line = wizard.line_ids.filtered(
            lambda line: line.document_type_id == other_type
        )
        expiry_date = fields.Date.today() + timedelta(days=90)
        first_line.write({
            'document_number': 'BULK-001',
            'expiry_date': expiry_date,
            'document_file': 'dGVzdDE=',
            'document_filename': 'first.pdf',
        })
        second_line.write({
            'document_number': 'BULK-002',
            'expiry_date': expiry_date,
            'document_file': 'dGVzdDI=',
            'document_filename': 'second.pdf',
        })

        result = wizard.action_save_documents()
        documents = self.env['restaurant.employee.document'].search([
            ('employee_id', '=', self.employee.id),
            ('document_type_id', 'in', [self.document_type.id, other_type.id]),
        ])
        self.assertEqual(len(documents), 2)
        self.assertEqual(result['res_model'], 'hr.employee')
        self.assertEqual(result['res_id'], self.employee.id)

    def test_sponsorship_status_changes_required_document_checklist(self):
        employee = self.env['hr.employee'].create({
            'name': 'Sponsorship Requirement Employee',
            'company_id': self.company.id,
        })
        self.assertEqual(employee.restaurant_immigration_status, 'new_joiner')
        self.assertEqual(
            set(employee.restaurant_missing_document_type_ids.mapped('code')),
            {'passport', 'signed_job_offer', 'current_visa_entry_permit'},
        )

        employee.restaurant_immigration_status = 'transfer_pending'
        self.assertEqual(
            set(employee.restaurant_missing_document_type_ids.mapped('code')),
            {
                'passport',
                'signed_job_offer',
                'current_visa_entry_permit',
                'previous_emirates_id',
                'residence_cancellation',
            },
        )

        employee.restaurant_immigration_status = 'visa_in_process'
        self.assertIn(
            'visa_processing_receipt',
            employee.restaurant_missing_document_type_ids.mapped('code'),
        )
        self.assertIn(
            'work_entry_permit',
            employee.restaurant_missing_document_type_ids.mapped('code'),
        )

        employee.restaurant_immigration_status = 'company_sponsored'
        self.assertEqual(
            set(employee.restaurant_missing_document_type_ids.mapped('code')),
            {
                'passport',
                'emirates_id',
                'residence_visa',
                'work_permit',
                'employment_contract',
                'medical_fitness',
            },
        )

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env['hr.employee'].create({
                'name': 'Missing Sponsor Employee',
                'company_id': self.company.id,
                'restaurant_immigration_status': 'other_sponsor',
            })
        employee.write({
            'restaurant_immigration_status': 'other_sponsor',
            'restaurant_current_sponsor_name': 'External Sponsor',
        })
        self.assertEqual(
            set(employee.restaurant_missing_document_type_ids.mapped('code')),
            {
                'passport',
                'emirates_id',
                'residence_visa',
                'work_permit',
                'employment_contract',
            },
        )

    def test_completed_company_documents_promote_in_progress_employee(self):
        employee = self.env['hr.employee'].create({
            'name': 'Completed Visa Employee',
            'company_id': self.company.id,
            'restaurant_immigration_status': 'visa_in_process',
        })
        final_codes = employee._RESTAURANT_DOCUMENT_REQUIREMENTS['company_sponsored']
        document_types = self.env['restaurant.employee.document.type'].search([
            ('code', 'in', list(final_codes)),
        ])
        expiry_date = fields.Date.today() + timedelta(days=365)
        for document_type in document_types:
            self.model.create({
                'employee_id': employee.id,
                'document_type_id': document_type.id,
                'expiry_date': expiry_date if document_type.expiry_required else False,
                'document_file': 'dGVzdA==',
            })
        self.assertEqual(
            employee.restaurant_immigration_status,
            'company_sponsored',
        )

    def test_hr_clearance_blocks_visit_visa_and_requires_work_documents(self):
        employee = self.env['hr.employee'].create({
            'name': 'Work Clearance Employee',
            'company_id': self.company.id,
        })
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            employee.with_user(self.hr).write({'restaurant_work_authorized': True})

        employee.restaurant_immigration_status = 'visa_in_process'
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            employee.with_user(self.hr).write({'restaurant_work_authorized': True})

        expiry_date = fields.Date.today() + timedelta(days=90)
        for code in ('work_permit', 'work_entry_permit'):
            document_type = self.env['restaurant.employee.document.type'].search([
                ('code', '=', code),
            ], limit=1)
            self.model.create({
                'employee_id': employee.id,
                'document_type_id': document_type.id,
                'expiry_date': expiry_date,
                'document_file': 'dGVzdA==',
            })
        employee.with_user(self.hr).write({'restaurant_work_authorized': True})
        self.assertTrue(employee.restaurant_work_authorized)
        self.assertEqual(employee.restaurant_work_authorized_by_id, self.hr)
        self.assertTrue(employee.restaurant_work_authorized_on)
