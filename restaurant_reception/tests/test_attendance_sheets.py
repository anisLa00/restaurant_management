from datetime import date, datetime, time, timedelta

from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user
from odoo.tools import mute_logger

from odoo.addons.base.tests.common import BaseCommon


class TestRestaurantAttendanceSheets(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.branch, cls.other_branch = cls.env['restaurant.branch'].create([
            {
                'name': 'Attendance Sheet Branch',
                'code': 'ATT-SHEET',
                'company_id': cls.company.id,
            },
            {
                'name': 'Attendance Other Branch',
                'code': 'ATT-OTHER',
                'company_id': cls.company.id,
            },
        ])
        cls.reception = cls._make_user(
            'sheet_reception', 'restaurant_core.group_restaurant_reception', cls.branch,
        )
        cls.other_reception = cls._make_user(
            'sheet_other_reception', 'restaurant_core.group_restaurant_reception',
            cls.other_branch,
        )
        cls.manager = cls._make_user(
            'sheet_manager', 'restaurant_core.group_restaurant_branch_manager', cls.branch,
        )
        cls.other_manager = cls._make_user(
            'sheet_other_manager', 'restaurant_core.group_restaurant_branch_manager',
            cls.other_branch,
        )
        cls.hr = cls._make_user('sheet_hr', 'restaurant_core.group_restaurant_hr')
        cls.staff_users = [
            cls._make_user(f'sheet_staff_{index}', 'base.group_user', cls.branch)
            for index in range(1, 4)
        ]
        cls.staff = cls.env['hr.employee'].create([
            {
                'name': f'Attendance Staff {index}',
                'company_id': cls.company.id,
                'user_id': user.id,
                'restaurant_branch_id': cls.branch.id,
            }
            for index, user in enumerate(cls.staff_users, 1)
        ])
        cls.other_staff_user = cls._make_user(
            'sheet_other_staff', 'base.group_user', cls.other_branch,
        )
        cls.other_staff = cls.env['hr.employee'].create({
            'name': 'Other Branch Staff',
            'company_id': cls.company.id,
            'user_id': cls.other_staff_user.id,
            'restaurant_branch_id': cls.other_branch.id,
        })
        cls.unlinked_employee = cls.env['hr.employee'].create({
            'name': 'Unlinked Roster Employee',
            'company_id': cls.company.id,
        })
        cls.leave_type = cls.env['hr.work.entry.type'].create({
            'name': 'Sheet Test Leave',
            'code': 'SHEETLEAVE',
            'count_as': 'absence',
            'country_id': cls.company.country_id.id,
            'time_off_selectable': True,
            'requires_allocation': False,
            'leave_validation_type': 'hr',
            'request_unit': 'day',
        })
        cls.company.write({
            'restaurant_hr_official_sync_enabled': True,
            'restaurant_annual_leave_type_id': cls.leave_type.id,
            'restaurant_sick_leave_type_id': cls.leave_type.id,
            'restaurant_emergency_leave_type_id': cls.leave_type.id,
        })
        cls.base_date = date(2098, 8, 10)

    @classmethod
    def _make_user(cls, login, groups, branch=None):
        return new_test_user(
            cls.env,
            login=login,
            groups=groups,
            company_id=cls.company.id,
            company_ids=[Command.set(cls.company.ids)],
            restaurant_branch_ids=[Command.set(branch.ids if branch else [])],
        )

    def _model(self, model, user):
        return self.env[model].with_user(user).with_context(
            allowed_company_ids=[self.company.id],
            tracking_disable=True,
            mail_notrack=True,
        )

    def _sheet(self, day=0):
        return self._model('restaurant.attendance.sheet', self.reception).create({
            'branch_id': self.branch.id,
            'attendance_date': self.base_date + timedelta(days=day),
        })

    def _times(self, attendance_date, start=time(8), end=time(16)):
        return {
            'check_in': datetime.combine(attendance_date, start),
            'check_out': datetime.combine(attendance_date, end),
        }

    def _set_line(self, line, status, **values):
        data = {'status': status, 'check_in': False, 'check_out': False, **values}
        if (
            status in ('present', 'late', 'overtime_day')
            and 'check_in' not in values
            and 'check_out' not in values
        ):
            data.update(self._times(line.attendance_date))
        if status == 'late' and 'late_minutes' not in values:
            data['late_minutes'] = 9
        line.with_user(self.reception).write(data)

    def _prepare_sheet(self, day=0, statuses=None):
        sheet = self._sheet(day)
        sheet.action_populate_roster()
        status_list = statuses or ['present'] * len(sheet.line_ids)
        for line, status in zip(sheet.line_ids.sorted('employee_id'), status_list):
            self._set_line(line, status)
        return sheet

    def _approve_sheet(self, day=0, statuses=None, overtime_employee=None):
        sheet = self._prepare_sheet(day, statuses)
        if overtime_employee:
            sheet.line_ids.filtered(
                lambda line: line.employee_id == overtime_employee
            ).with_user(self.reception).write({'overtime_hours': 1.5})
        sheet.action_submit()
        manager_sheet = sheet.with_user(self.manager)
        manager_sheet.action_manager_accept_clean()
        remaining = sheet.line_ids.filtered(lambda line: line.state == 'manager_review')
        if remaining:
            remaining.with_user(self.manager).action_manager_accept_selected()
        manager_sheet.action_submit_to_hr()
        hr_sheet = sheet.with_user(self.hr)
        hr_sheet.action_hr_approve_clean()
        remaining = sheet.line_ids.filtered(lambda line: line.state == 'submitted_to_hr')
        if remaining:
            remaining.with_user(self.hr).action_hr_approve_selected()
        self.assertEqual(sheet.state, 'approved')
        return sheet

    def test_01_roster_population_uses_employee_branch_and_pending_semantics(self):
        day = self.base_date
        no_account_employee = self.env['hr.employee'].create({
            'name': 'Branch Employee Without Login',
            'company_id': self.company.id,
            'restaurant_branch_id': self.branch.id,
        })
        legacy = self._model('restaurant.attendance.entry', self.reception).create({
            'branch_id': self.branch.id,
            'employee_id': self.staff[0].id,
            'attendance_date': day,
            'status': 'present',
            **self._times(day),
        })
        sheet = self._sheet()
        sheet.action_populate_roster()

        self.assertEqual(
            set(sheet.line_ids.employee_id.ids),
            set((self.staff | no_account_employee).ids),
        )
        self.assertEqual(legacy.sheet_id, sheet)
        new_rows = sheet.line_ids - legacy
        self.assertTrue(new_rows)
        self.assertEqual(set(new_rows.mapped('status')), {'pending'})
        self.assertNotIn(self.other_staff, sheet.line_ids.employee_id)
        self.assertNotIn(self.unlinked_employee, sheet.line_ids.employee_id)
        self.assertGreaterEqual(sheet.roster_unlinked_employee_count, 1)
        self.assertIn('no Restaurant Branch', sheet.roster_warning)

        count_before = len(sheet.line_ids)
        sheet.action_populate_roster()
        self.assertEqual(len(sheet.line_ids), count_before)

        transferred = self.staff[0]
        transferred.restaurant_branch_id = self.other_branch
        self.assertEqual(legacy.branch_id, self.branch)
        future_sheet = self._model(
            'restaurant.attendance.sheet', self.other_reception,
        ).create({
            'branch_id': self.other_branch.id,
            'attendance_date': day + timedelta(days=1),
        })
        future_sheet.action_populate_roster()
        self.assertIn(transferred, future_sheet.line_ids.employee_id)

    def test_01b_hr_can_assign_a_branch_to_multiple_employees(self):
        employees = self.env['hr.employee'].create([
            {'name': 'Bulk Branch One', 'company_id': self.company.id},
            {'name': 'Bulk Branch Two', 'company_id': self.company.id},
        ])
        wizard = self._model(
            'restaurant.employee.branch.assign.wizard', self.hr,
        ).with_context(
            active_model='hr.employee', active_ids=employees.ids,
        ).create({'branch_id': self.branch.id})
        self.assertEqual(set(wizard.employee_ids.ids), set(employees.ids))
        wizard.action_assign_branch()
        self.assertEqual(employees.restaurant_branch_id, self.branch)

    @mute_logger('odoo.sql_db')
    def test_02_sheet_uniqueness_row_uniqueness_and_branch_isolation(self):
        self._sheet()
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._sheet()
        with self.assertRaises(AccessError):
            self._model('restaurant.attendance.sheet', self.reception).create({
                'branch_id': self.other_branch.id,
                'attendance_date': self.base_date,
            })

        sheet = self._sheet(day=1)
        sheet.action_populate_roster()
        line = sheet.line_ids[0]
        with self.assertRaises(IntegrityError), self.env.cr.savepoint():
            self._model('restaurant.attendance.entry', self.reception).create({
                'branch_id': self.branch.id,
                'employee_id': line.employee_id.id,
                'attendance_date': sheet.attendance_date,
                'status': 'pending',
            })
        with self.assertRaises(AccessError):
            sheet.with_user(self.other_manager).check_access('read')

    def test_03_pending_block_and_manager_row_return_requires_reason(self):
        sheet = self._sheet(day=2)
        sheet.action_populate_roster()
        with self.assertRaises(ValidationError):
            sheet.action_submit()

        lines = sheet.line_ids.sorted('employee_id')
        self._set_line(lines[0], 'present')
        self._set_line(lines[1], 'absent')
        self._set_line(lines[2], 'day_off')
        sheet.action_submit()
        sheet.with_user(self.manager).action_manager_accept_clean()
        absent = lines.filtered(lambda line: line.status == 'absent')
        self.assertEqual(absent.state, 'manager_review')
        with self.assertRaises(ValidationError):
            absent.with_user(self.manager).action_manager_return_to_reception()
        absent.with_user(self.manager).manager_return_reason = 'Confirm the missed shift.'
        absent.with_user(self.manager).action_manager_return_to_reception()
        self.assertEqual(absent.state, 'draft')
        self.assertEqual(absent.manager_return_count, 1)
        self.assertEqual(absent.last_manager_return_reason, 'Confirm the missed shift.')

        self._set_line(absent, 'day_off', reception_note='Roster confirmed.')
        absent.with_user(self.reception).action_submit()
        absent.with_user(self.manager).action_manager_accept_selected()
        sheet.with_user(self.manager).action_submit_to_hr()
        self.assertEqual(sheet.state, 'submitted_to_hr')

    def test_04_exception_classification_and_search(self):
        sheet = self._sheet(day=3)
        sheet.action_populate_roster()
        lines = sheet.line_ids.sorted('employee_id')
        self._set_line(lines[0], 'present', check_in=False, check_out=False)
        self._set_line(lines[1], 'late', late_minutes=18, overtime_hours=2)
        self._set_line(lines[2], 'overtime_day', overtime_hours=8)

        duplicate = self.env['restaurant.attendance.entry'].create({
            'branch_id': self.other_branch.id,
            'employee_id': lines[2].employee_id.id,
            'attendance_date': sheet.attendance_date,
            'status': 'absent',
        })
        self.assertTrue(lines[0].has_blocking_exception)
        self.assertIn('Missing', lines[0].exception_summary)
        self.assertIn('Late', lines[1].exception_summary)
        self.assertIn('Overtime', lines[1].exception_summary)
        self.assertIn('Overtime', lines[2].exception_summary)
        self.assertIn('Duplicate', lines[2].exception_summary)
        self.assertTrue(duplicate.is_exception)

        found = self.env['restaurant.attendance.entry'].search([
            ('id', 'in', (lines | duplicate).ids),
            ('is_exception', '=', True),
        ])
        self.assertEqual(set(found.ids), set((lines | duplicate).ids))

    def test_05_manager_and_hr_bulk_actions_close_daily_sheet(self):
        sheet = self._prepare_sheet(
            day=4,
            statuses=['present', 'late', 'absent'],
        )
        late = sheet.line_ids.filtered(lambda line: line.status == 'late')
        late.with_user(self.reception).write({
            'late_minutes': 12,
            'overtime_hours': 2,
        })
        sheet.action_submit()

        manager_sheet = sheet.with_user(self.manager)
        manager_sheet.action_manager_accept_clean()
        exceptions = sheet.line_ids.filtered(lambda line: line.state == 'manager_review')
        self.assertEqual(len(exceptions), 2)
        exceptions.with_user(self.manager).action_manager_accept_selected()
        manager_sheet.action_submit_to_hr()

        hr_sheet = sheet.with_user(self.hr)
        hr_sheet.action_hr_approve_clean()
        exceptions = sheet.line_ids.filtered(lambda line: line.state == 'submitted_to_hr')
        self.assertEqual(len(exceptions), 2)
        exceptions.with_user(self.hr).action_hr_approve_selected()
        self.assertEqual(sheet.state, 'approved')
        self.assertTrue(sheet.approved_by)
        self.assertEqual(set(sheet.line_ids.mapped('state')), {'approved'})
        self.assertEqual(self.env['hr.attendance'].search_count([
            ('restaurant_attendance_entry_id', 'in', sheet.line_ids.ids),
        ]), 2)
        self.assertEqual(self.env['restaurant.overtime.ledger'].search_count([
            ('source_entry_id', 'in', sheet.line_ids.ids),
        ]), 1)

    def test_06_monthly_aggregation_and_close_blocking(self):
        first = self._approve_sheet(
            day=5,
            statuses=['present', 'late', 'absent'],
            overtime_employee=self.staff[1],
        )

        pending_sheet = self._sheet(day=6)
        pending_sheet.action_populate_roster()
        month = self._model('restaurant.attendance.month', self.hr).create({
            'branch_id': self.branch.id,
            'month_start': self.base_date.replace(day=1),
        })
        with self.assertRaises(ValidationError):
            month.action_close()

        for line in pending_sheet.line_ids:
            self._set_line(line, 'day_off')
        pending_sheet.action_submit()
        pending_sheet.with_user(self.manager).action_manager_accept_clean()
        pending_sheet.with_user(self.manager).action_submit_to_hr()
        pending_sheet.with_user(self.hr).action_hr_approve_clean()

        month.action_refresh()
        self.assertEqual(month.employee_count, 3)
        staff_two = month.line_ids.filtered(lambda line: line.employee_id == self.staff[1])
        self.assertEqual(staff_two.late_count, 1)
        self.assertEqual(staff_two.late_minutes, 9)
        self.assertEqual(staff_two.overtime_hours, 1.5)
        self.assertEqual(month.pending_missing_total, 0)
        month.action_close()
        self.assertEqual(month.state, 'closed')
        self.assertTrue(month.closed_by)

        with self.assertRaises(AccessError):
            self._model('restaurant.attendance.entry', self.reception).create({
                'branch_id': self.branch.id,
                'employee_id': self.unlinked_employee.id,
                'attendance_date': self.base_date + timedelta(days=8),
                'status': 'absent',
            })

    def test_07_monthly_correction_is_audited_and_sync_stays_idempotent(self):
        sheet = self._approve_sheet(
            day=7,
            statuses=['present', 'present', 'present'],
            overtime_employee=self.staff[0],
        )
        entry = sheet.line_ids.filtered(lambda line: line.employee_id == self.staff[0])
        attendance = entry.official_attendance_id
        ledger = entry.overtime_ledger_id
        month = self._model('restaurant.attendance.month', self.hr).create({
            'branch_id': self.branch.id,
            'month_start': self.base_date.replace(day=1),
        })
        month.action_close()
        with self.assertRaises(ValidationError):
            month.action_open_correction()
        month.correction_reason = 'Correct source clock and remove duplicate overtime claim.'
        month.action_open_correction()
        self.assertEqual(month.state, 'correction')
        self.assertEqual(month.correction_count, 1)

        entry.with_user(self.hr).action_reopen_for_month_correction()
        self.assertEqual(entry.state, 'draft')
        self.assertEqual(entry.correction_count, 1)
        self.assertEqual(sheet.state, 'manager_review')
        corrected_check_in = entry.check_in + timedelta(minutes=5)
        entry.with_user(self.reception).write({
            'check_in': corrected_check_in,
            'overtime_hours': 0,
            'reception_note': 'Correction applied from audited request.',
        })
        entry.with_user(self.reception).action_submit()
        entry.with_user(self.manager).action_manager_accept_selected()
        sheet.with_user(self.manager).action_submit_to_hr()
        entry.with_user(self.hr).action_hr_approve_selected()

        self.assertEqual(sheet.state, 'approved')
        self.assertEqual(entry.official_attendance_id, attendance)
        self.assertEqual(attendance.check_in, corrected_check_in)
        self.assertEqual(self.env['hr.attendance'].search_count([
            ('restaurant_attendance_entry_id', '=', entry.id),
        ]), 1)
        self.assertEqual(entry.overtime_ledger_id, ledger)
        self.assertFalse(ledger.active)
        self.assertEqual(ledger.hours, 0)
        self.assertEqual(
            self.env['restaurant.overtime.ledger'].with_context(
                active_test=False,
            ).search_count([('source_entry_id', '=', entry.id)]),
            1,
        )

        month.action_close()
        refreshed = month.line_ids.filtered(lambda line: line.employee_id == entry.employee_id)
        self.assertEqual(refreshed.overtime_hours, 0)
        self.assertEqual(month.state, 'closed')

    def test_08_role_separation_is_preserved_for_sheet_bulk_actions(self):
        sheet = self._prepare_sheet(day=8)
        sheet.action_submit()
        with self.assertRaises(AccessError):
            sheet.with_user(self.reception).action_manager_accept_clean()
        sheet.with_user(self.manager).action_manager_accept_clean()
        sheet.with_user(self.manager).action_submit_to_hr()
        with self.assertRaises(AccessError):
            sheet.with_user(self.manager).action_hr_approve_clean()

    def test_09_hr_returns_one_row_without_resetting_the_whole_sheet(self):
        sheet = self._prepare_sheet(
            day=9,
            statuses=['present', 'late', 'absent'],
        )
        sheet.action_submit()
        manager_sheet = sheet.with_user(self.manager)
        manager_sheet.action_manager_accept_clean()
        exceptions = sheet.line_ids.filtered(
            lambda line: line.state == 'manager_review'
        )
        exceptions.with_user(self.manager).action_manager_accept_selected()
        manager_sheet.action_submit_to_hr()

        returned = sheet.line_ids.filtered(lambda line: line.status == 'absent')
        with self.assertRaises(ValidationError):
            returned.with_user(self.hr).action_return_to_reception()
        returned.with_user(self.hr).decision_reason = 'Confirm the absence with Reception.'
        returned.with_user(self.hr).action_return_to_reception()

        self.assertEqual(sheet.state, 'manager_review')
        self.assertEqual(returned.state, 'draft')
        self.assertEqual(
            set((sheet.line_ids - returned).mapped('state')),
            {'submitted_to_hr'},
        )
        self.assertEqual(returned.hr_return_count, 1)

        self._set_line(returned, 'day_off', reception_note='Schedule confirmed.')
        returned.with_user(self.reception).action_submit()
        returned.with_user(self.manager).action_manager_accept_selected()
        manager_sheet.action_submit_to_hr()

        hr_sheet = sheet.with_user(self.hr)
        hr_sheet.action_hr_approve_clean()
        remaining = sheet.line_ids.filtered(
            lambda line: line.state == 'submitted_to_hr'
        )
        remaining.with_user(self.hr).action_hr_approve_selected()
        self.assertEqual(sheet.state, 'approved')
        self.assertEqual(set(sheet.line_ids.mapped('state')), {'approved'})
