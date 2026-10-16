from datetime import date

from odoo import Command
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import new_test_user

from odoo.addons.base.tests.common import BaseCommon


class TestTipsDistribution(BaseCommon):
    _test_user_groups = ()

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tips_company = cls.env["res.company"].create({
            "name": "Tips Distribution Test Company",
            "currency_id": cls.env.company.currency_id.id,
        })
        cls.branch_a, cls.branch_b = cls.env["restaurant.branch"].create([
            {
                "name": "Tips Branch A",
                "code": "TIP-A",
                "company_id": cls.tips_company.id,
            },
            {
                "name": "Tips Branch B",
                "code": "TIP-B",
                "company_id": cls.tips_company.id,
            },
        ])
        cls.waiter_job = cls.env["hr.job"].create({
            "name": "Waiter",
            "company_id": cls.tips_company.id,
        })
        cls.reception_a = cls._make_user(
            "tips_reception_a", "restaurant_core.group_restaurant_reception",
            cls.branch_a,
        )
        cls.reception_b = cls._make_user(
            "tips_reception_b", "restaurant_core.group_restaurant_reception",
            cls.branch_b,
        )
        cls.manager_a = cls._make_user(
            "tips_manager_a", "restaurant_core.group_restaurant_branch_manager",
            cls.branch_a,
        )
        cls.manager_b = cls._make_user(
            "tips_manager_b", "restaurant_core.group_restaurant_branch_manager",
            cls.branch_b,
        )
        cls.accountant = cls._make_user(
            "tips_accountant", "restaurant_core.group_restaurant_accountant",
        )
        cls.operations = cls._make_user(
            "tips_operations", "restaurant_core.group_restaurant_operations_manager",
            cls.branch_a | cls.branch_b,
        )
        cls.owner = cls._make_user(
            "tips_owner", "restaurant_core.group_restaurant_owner",
            cls.branch_a | cls.branch_b,
        )
        (
            cls.waiter_a1,
            cls.waiter_a2,
            cls.waiter_a3,
            cls.waiter_b1,
            cls.waiter_b2,
            cls.ordinary_staff,
        ) = cls.env["hr.employee"].create([
            {
                "name": "Tips Waiter A1",
                "company_id": cls.tips_company.id,
                "job_id": cls.waiter_job.id,
                "restaurant_branch_id": cls.branch_a.id,
            },
            {
                "name": "Tips Waiter A2",
                "company_id": cls.tips_company.id,
                "job_id": cls.waiter_job.id,
                "restaurant_branch_id": cls.branch_a.id,
            },
            {
                "name": "Tips Waiter A3",
                "company_id": cls.tips_company.id,
                "job_id": cls.waiter_job.id,
                "restaurant_branch_id": cls.branch_a.id,
            },
            {
                "name": "Tips Waiter B1",
                "company_id": cls.tips_company.id,
                "job_id": cls.waiter_job.id,
                "restaurant_branch_id": cls.branch_b.id,
            },
            {
                "name": "Tips Waiter B2",
                "company_id": cls.tips_company.id,
                "job_id": cls.waiter_job.id,
                "restaurant_branch_id": cls.branch_b.id,
            },
            {
                "name": "Tips Ordinary Staff",
                "company_id": cls.tips_company.id,
                "restaurant_branch_id": cls.branch_a.id,
            },
        ])
        cls.cycle_start = date(2024, 1, 15)
        cls.cycle_end = date(2024, 2, 15)

    @classmethod
    def _make_user(cls, login, groups, branches=None):
        return new_test_user(
            cls.env,
            login=login,
            groups=groups,
            company_id=cls.tips_company.id,
            company_ids=[Command.set(cls.tips_company.ids)],
            restaurant_branch_ids=[Command.set((branches or cls.env["restaurant.branch"]).ids)],
        )

    def _closing(self, branch, reception, manager, business_date, rows):
        closing = self.env["restaurant.waiter.sales.closing"].with_user(
            reception,
        ).with_context(allowed_company_ids=[self.tips_company.id]).create({
            "branch_id": branch.id,
            "business_date": business_date,
        })
        for index, row in enumerate(rows, start=1):
            values = {
                "waiter_closing_id": closing.id,
                "service_type": row.get("service_type", "dine_in"),
                "bill_reference": f"{branch.code}-{business_date}-{index}",
                "bill_amount": row.get("bill_amount", 100),
                "cash_tip_amount": row.get("cash_tip", 0),
                "card_tip_amount": row.get("card_tip", 0),
                "review_count": 0,
            }
            if values["service_type"] == "dine_in":
                values.update({
                    "employee_id": row["employee"].id,
                    "table_reference": str(index),
                    "guest_count": 1,
                })
            self.env["restaurant.waiter.sales.bill"].with_user(
                reception,
            ).with_context(allowed_company_ids=[self.tips_company.id]).create(values)
        closing.with_user(reception).action_submit()
        closing.with_user(manager).action_close()
        return closing.sudo()

    def _attendance(self, employee, days):
        for day in days:
            self.env["restaurant.attendance.entry"].sudo().with_context(
                restaurant_leave_sync=True,
            ).create({
                "employee_id": employee.id,
                "branch_id": employee.restaurant_branch_id.id,
                "attendance_date": day,
                "status": "present",
                "state": "approved",
                "sync_status": "not_required",
                "source_type": "api",
                "entered_by": self.env.user.id,
            })

    def _collection(self, branch, reception):
        return self.env["restaurant.tips.collection"].with_user(
            reception,
        ).with_context(allowed_company_ids=[self.tips_company.id]).create({
            "branch_id": branch.id,
            "cycle_start": self.cycle_start,
        })

    def _seed_distribution_source(self):
        self._closing(
            self.branch_a, self.reception_a, self.manager_a, date(2024, 1, 20),
            [
                {"employee": self.waiter_a1, "cash_tip": 30, "card_tip": 20},
                {"employee": self.waiter_a2, "cash_tip": 50},
                {"employee": self.waiter_a3, "cash_tip": 40},
                {"service_type": "pickup", "cash_tip": 10.01},
            ],
        )
        self._closing(
            self.branch_b, self.reception_b, self.manager_b, date(2024, 1, 21),
            [
                {"employee": self.waiter_b1, "cash_tip": 20},
                {"employee": self.waiter_b2, "cash_tip": 20},
                {"service_type": "delivery", "card_tip": 10},
            ],
        )
        self._attendance(
            self.waiter_a1, [date(2024, 1, 22), date(2024, 1, 23)],
        )
        self._attendance(self.waiter_a2, [date(2024, 1, 22)])
        collection_a = self._collection(self.branch_a, self.reception_a)
        collection_b = self._collection(self.branch_b, self.reception_b)
        collection_a.with_user(self.reception_a).action_submit()
        collection_b.with_user(self.reception_b).action_submit()
        return collection_a, collection_b

    def test_branch_sheet_uses_only_closed_sources_and_next_15th_is_new_cycle(self):
        included = self._closing(
            self.branch_a, self.reception_a, self.manager_a, date(2024, 2, 14),
            [{"employee": self.waiter_a1, "cash_tip": 10, "card_tip": 5}],
        )
        boundary = self._closing(
            self.branch_a, self.reception_a, self.manager_a, date(2024, 2, 15),
            [{"employee": self.waiter_a1, "cash_tip": 99}],
        )
        collection = self._collection(self.branch_a, self.reception_a)

        self.assertEqual(collection.cycle_end, self.cycle_end)
        self.assertEqual(collection.source_closing_ids, included)
        self.assertNotIn(boundary, collection.source_closing_ids)
        self.assertEqual(collection.total_cash_tips, 10)
        self.assertEqual(collection.total_card_tips, 5)
        self.assertEqual(collection.total_tips, 15)
        self.assertEqual(collection.waiter_line_ids.total_tips, 15)

    def test_open_waiter_sheet_blocks_branch_submission(self):
        self._closing(
            self.branch_a, self.reception_a, self.manager_a, date(2024, 1, 20),
            [{"employee": self.waiter_a1, "cash_tip": 10}],
        )
        pending = self.env["restaurant.waiter.sales.closing"].with_user(
            self.reception_a,
        ).with_context(allowed_company_ids=[self.tips_company.id]).create({
            "branch_id": self.branch_a.id,
            "business_date": date(2024, 1, 21),
        })
        collection = self._collection(self.branch_a, self.reception_a)
        with self.assertRaisesRegex(ValidationError, "still open"):
            collection.with_user(self.reception_a).action_submit()
        pending.with_user(self.reception_a).action_cancel()
        collection.with_user(self.reception_a).action_submit()
        self.assertEqual(collection.state, "submitted")

    def test_global_pool_branch_ranking_ties_manual_double_and_exact_total(self):
        collection_a, collection_b = self._seed_distribution_source()
        distribution = self.env["restaurant.tips.distribution"].sudo().search([
            ("company_id", "=", self.tips_company.id),
            ("cycle_start", "=", self.cycle_start),
        ], limit=1)
        self.assertTrue(distribution)
        self.assertEqual(distribution.global_pool, 200.01)
        self.assertEqual(len(distribution.branch_line_ids), 2)
        distribution.with_user(self.accountant).action_refresh()
        branch_lines = {
            line.branch_id: line for line in distribution.branch_line_ids
        }
        branch_lines[self.branch_a].with_user(self.accountant).write({
            "top_waiter_count": 1,
        })
        branch_lines[self.branch_b].with_user(self.accountant).write({
            "top_waiter_count": 1,
        })
        distribution.with_user(self.accountant).write({
            "extra_double_employee_ids": [Command.set(self.ordinary_staff.ids)],
        })
        with self.assertRaisesRegex(ValidationError, "reason"):
            distribution.with_user(self.accountant).action_prepare()
        distribution.with_user(self.accountant).write({
            "extra_double_reason": "Approved management exception for this cycle.",
        })
        distribution.with_user(self.accountant).action_prepare()

        self.assertEqual(distribution.state, "prepared")
        self.assertEqual(distribution.collection_ids, collection_a | collection_b)
        self.assertEqual(distribution.global_pool, 200.01)
        self.assertEqual(distribution.active_staff_count, 6)
        self.assertEqual(distribution.top_waiter_count, 3)
        self.assertEqual(distribution.total_units, 10)
        self.assertEqual(distribution.ordinary_share, 20)
        self.assertEqual(distribution.rounding_adjustment, 0.01)
        self.assertEqual(distribution.allocation_total, 200.01)
        self.assertEqual(sum(distribution.allocation_line_ids.mapped("amount")), 200.01)

        rankings = {
            line.employee_id: line for line in distribution.ranking_line_ids
        }
        self.assertEqual(rankings[self.waiter_a1].rank, 1)
        self.assertTrue(rankings[self.waiter_a1].is_top)
        self.assertEqual(rankings[self.waiter_a2].rank, 2)
        self.assertFalse(rankings[self.waiter_a2].is_top)
        self.assertEqual(rankings[self.waiter_b1].rank, 1)
        self.assertEqual(rankings[self.waiter_b2].rank, 1)
        self.assertTrue(rankings[self.waiter_b1].is_top)
        self.assertTrue(rankings[self.waiter_b2].is_top)

        allocations = {
            line.employee_id: line for line in distribution.allocation_line_ids
        }
        self.assertEqual(allocations[self.waiter_a1].share_weight, 2)
        self.assertEqual(allocations[self.waiter_a1].amount, 40.01)
        self.assertEqual(allocations[self.waiter_b1].amount, 40)
        self.assertEqual(allocations[self.waiter_b2].amount, 40)
        self.assertEqual(allocations[self.ordinary_staff].amount, 40)
        self.assertTrue(allocations[self.ordinary_staff].is_manual_double)
        self.assertEqual(allocations[self.waiter_a3].amount, 20)

    def test_missing_branch_submission_blocks_global_prepare(self):
        self._closing(
            self.branch_a, self.reception_a, self.manager_a, date(2024, 1, 20),
            [{"employee": self.waiter_a1, "cash_tip": 10}],
        )
        collection = self._collection(self.branch_a, self.reception_a)
        collection.with_user(self.reception_a).action_submit()
        distribution = self.env["restaurant.tips.distribution"].sudo().search([
            ("company_id", "=", self.tips_company.id),
            ("cycle_start", "=", self.cycle_start),
        ], limit=1)
        with self.assertRaisesRegex(ValidationError, "Tips Branch B"):
            distribution.with_user(self.accountant).action_refresh()

    def test_permissions_do_not_expand_owner_operations_or_other_branch(self):
        collection_a = self._collection(self.branch_a, self.reception_a)
        collection_b = self._collection(self.branch_b, self.reception_b)
        collection_a.with_user(self.reception_a).check_access("read")
        with self.assertRaises(AccessError):
            collection_b.with_user(self.reception_a).check_access("read")
        collection_a.with_user(self.accountant).check_access("read")
        with self.assertRaises(AccessError):
            collection_a.with_user(self.accountant).write({"reception_note": "No."})
        for user in (self.operations, self.owner, self.manager_a):
            with self.assertRaises(AccessError):
                collection_a.with_user(user).check_access("read")

        distribution = self.env["restaurant.tips.distribution"].with_user(
            self.accountant,
        ).with_context(allowed_company_ids=[self.tips_company.id]).create({
            "company_id": self.tips_company.id,
            "cycle_start": self.cycle_start,
        })
        for user in (self.operations, self.owner, self.manager_a, self.reception_a):
            with self.assertRaises(AccessError):
                distribution.with_user(user).check_access("read")
        self.assertEqual(
            dict(distribution._fields["state"].selection),
            {"draft": "Draft Preparation", "prepared": "Prepared for Review"},
        )
        self.assertFalse(hasattr(distribution, "action_paid"))

    def test_daily_job_creates_current_branch_sheets_once_and_refreshes_them(self):
        Collection = self.env["restaurant.tips.collection"].sudo()
        cycle_start, _cycle_end = Collection._default_cycle()
        domain = [
            ("company_id", "=", self.tips_company.id),
            ("cycle_start", "=", cycle_start),
        ]
        self.assertFalse(Collection.search(domain))

        Collection._cron_prepare_current_cycle()
        sheets = Collection.search(domain)
        self.assertEqual(sheets.mapped("branch_id"), self.branch_a | self.branch_b)
        self.assertEqual(set(sheets.mapped("state")), {"draft"})
        self.assertTrue(all(sheets.mapped("last_refreshed_at")))

        Collection._cron_prepare_current_cycle()
        self.assertEqual(Collection.search_count(domain), 2)
