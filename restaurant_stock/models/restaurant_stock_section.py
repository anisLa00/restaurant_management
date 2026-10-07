from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError

from .stock_security import (
    OPERATIONS_GROUP,
    OWNER_GROUP,
    STOCKKEEPER_GROUP,
    require_assigned_branches,
)


SYSTEM_SECTION_SYNC_CONTEXT = "restaurant_system_section_sync"

SECTION_CATEGORY_XMLIDS = {
    "kitchen": (
        "restaurant_stock.product_category_raw_meat_sea_food",
        "restaurant_stock.product_category_vegetables",
        "restaurant_stock.product_category_dry_items",
        "restaurant_stock.product_category_dairy_items",
    ),
    "bar": ("restaurant_stock.product_category_bar",),
    "disposable": ("restaurant_stock.product_category_disposable",),
}

SECTION_NAMES = {
    "kitchen": "Kitchen",
    "bar": "Bar",
    "disposable": "Disposable",
}


def _require_section_config_role(env):
    if env.su:
        return
    if env.user.has_group(OPERATIONS_GROUP) or env.user.has_group(OWNER_GROUP):
        return
    if not env.user.has_group(STOCKKEEPER_GROUP):
        raise AccessError(env._("Your role does not allow section configuration."))


def _check_no_active_daily(env, branches):
    if not branches:
        return
    active = env["restaurant.stock.daily"].sudo().search([
        ("branch_id", "in", branches.ids),
        ("state", "in", ["draft", "opened", "closing_review"]),
    ], limit=1)
    if active:
        raise ValidationError(
            env._(
                "Finish or cancel active Daily Stock sheet %s before changing "
                "Kitchen / Bar section configuration.",
                active.display_name,
            )
        )


class RestaurantStockSection(models.Model):
    _name = "restaurant.stock.section"
    _description = "Restaurant Branch Stock Section"
    _order = "branch_id, section_type, id"

    name = fields.Char(required=True)
    section_type = fields.Selection(
        [
            ("kitchen", "Kitchen"),
            ("bar", "Bar / Refreshments"),
            ("disposable", "Disposable"),
        ],
        required=True,
        index=True,
    )
    branch_id = fields.Many2one(
        "restaurant.branch",
        required=True,
        default=lambda self: self.env.user.default_restaurant_branch_id,
        ondelete="restrict",
        index=True,
    )
    company_id = fields.Many2one(
        related="branch_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    required_for_daily_close = fields.Boolean(
        default=True,
        help=(
            "When enabled, the Branch Manager cannot finalize Reception's "
            "Daily Closing until this section's Daily Stock is closed."
        ),
    )
    active = fields.Boolean(default=True)
    can_configure = fields.Boolean(compute="_compute_can_configure")
    product_assignment_ids = fields.One2many(
        "restaurant.stock.section.product",
        "section_id",
        string="Legacy Assigned Stock Products",
        help=(
            "Preserved for historical compatibility. Current Daily Stock "
            "grouping comes automatically from the native Product Category."
        ),
    )

    _branch_type_unique = models.UniqueIndex(
        "(branch_id, section_type)",
        "This stock section already exists for the branch.",
    )

    @api.model
    def _category_ids_by_section_type(self):
        result = {}
        for section_type, xmlids in SECTION_CATEGORY_XMLIDS.items():
            result[section_type] = {
                category.id
                for xmlid in xmlids
                if (category := self.env.ref(xmlid, raise_if_not_found=False))
            }
        return result

    @api.model
    def _section_type_for_product(self, product):
        product.ensure_one()
        category_id = product.categ_id.id
        for section_type, category_ids in (
            self._category_ids_by_section_type().items()
        ):
            if category_id in category_ids:
                return section_type
        return False

    @api.model
    def _products_for_section(self, section):
        section.ensure_one()
        category_ids = self._category_ids_by_section_type().get(
            section.section_type,
            set(),
        )
        products = self.env["product.product"].sudo().search([
            ("active", "=", True),
            ("is_storable", "=", True),
            ("categ_id", "in", list(category_ids)),
            ("company_id", "in", [False, section.company_id.id]),
        ], order="product_tmpl_id, id")
        legacy_products = section.sudo().product_assignment_ids.product_id.filtered(
            lambda product: (
                product.active
                and product.is_storable
                and not self._section_type_for_product(product)
            )
        )
        return (products | legacy_products).sorted("display_name")

    @api.model
    def _catalog_products(self, company):
        category_ids = set().union(
            *self._category_ids_by_section_type().values()
        )
        return self.env["product.product"].sudo().search([
            ("active", "=", True),
            ("is_storable", "=", True),
            ("categ_id", "in", list(category_ids)),
            ("company_id", "in", [False, company.id]),
        ], order="product_tmpl_id, id").sorted("display_name")

    @api.model
    def _section_for_product(self, branch, product):
        section_type = self._section_type_for_product(product)
        if section_type:
            return self.sudo().search([
                ("branch_id", "=", branch.id),
                ("section_type", "=", section_type),
                ("active", "=", True),
            ], limit=1)

        # Historical compatibility only: old non-catalog products retain
        # their explicit assignment. Approved restaurant categories above
        # always win and never require branch-by-branch setup.
        assignment = self.env[
            "restaurant.stock.section.product"
        ].sudo().search([
            ("branch_id", "=", branch.id),
            ("product_id", "=", product.id),
            ("section_id.active", "=", True),
        ], limit=1)
        return assignment.section_id

    @api.model
    def _ensure_category_sections(self, branches):
        result = self.browse()
        for branch in branches.sudo():
            for section_type, name in SECTION_NAMES.items():
                section = self.sudo().search([
                    ("branch_id", "=", branch.id),
                    ("section_type", "=", section_type),
                ], limit=1)
                if not section:
                    section = self.sudo().with_context(**{
                        SYSTEM_SECTION_SYNC_CONTEXT: True,
                    }).create({
                        "name": name,
                        "section_type": section_type,
                        "branch_id": branch.id,
                        "required_for_daily_close": True,
                    })
                elif not section.active or not section.required_for_daily_close:
                    section.sudo().with_context(**{
                        SYSTEM_SECTION_SYNC_CONTEXT: True,
                    }).write({
                        "active": True,
                        "required_for_daily_close": True,
                    })
                result |= section
        return result

    @api.depends_context("uid")
    def _compute_can_configure(self):
        can_configure = (
            self.env.su
            or self.env.user.has_group(STOCKKEEPER_GROUP)
            or self.env.user.has_group(OPERATIONS_GROUP)
            or self.env.user.has_group(OWNER_GROUP)
        )
        for section in self:
            section.can_configure = can_configure

    @api.model_create_multi
    def create(self, vals_list):
        if self.env.context.get(SYSTEM_SECTION_SYNC_CONTEXT):
            return super().create(vals_list)
        _require_section_config_role(self.env)
        branches = self.env["restaurant.branch"].browse(
            [values.get("branch_id") for values in vals_list if values.get("branch_id")]
        )
        require_assigned_branches(branches)
        _check_no_active_daily(self.env, branches)
        return super().create(vals_list)

    def write(self, vals):
        if self.env.context.get(SYSTEM_SECTION_SYNC_CONTEXT):
            return super().write(vals)
        _require_section_config_role(self.env)
        require_assigned_branches(self.branch_id)
        target_branches = self.branch_id
        if vals.get("branch_id"):
            target_branches |= self.env["restaurant.branch"].browse(vals["branch_id"])
            require_assigned_branches(target_branches)
        if set(vals) & {
            "name",
            "section_type",
            "branch_id",
            "required_for_daily_close",
            "active",
            "product_assignment_ids",
        }:
            _check_no_active_daily(self.env, target_branches)
        return super().write(vals)

    def unlink(self):
        if self.env.context.get(SYSTEM_SECTION_SYNC_CONTEXT):
            return super().unlink()
        _require_section_config_role(self.env)
        require_assigned_branches(self.branch_id)
        _check_no_active_daily(self.env, self.branch_id)
        return super().unlink()


class RestaurantStockSectionProduct(models.Model):
    _name = "restaurant.stock.section.product"
    _description = "Restaurant Stock Product Section Assignment"
    _rec_name = "product_id"
    _order = "branch_id, section_id, product_id"

    section_id = fields.Many2one(
        "restaurant.stock.section",
        required=True,
        ondelete="cascade",
        index=True,
    )
    branch_id = fields.Many2one(
        related="section_id.branch_id",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="section_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    product_id = fields.Many2one(
        "product.product",
        required=True,
        ondelete="restrict",
        index=True,
        domain="[('is_storable', '=', True), ('company_id', 'in', [False, company_id])]",
    )
    note = fields.Char()

    _branch_product_unique = models.UniqueIndex(
        "(branch_id, product_id)",
        "A stock product can belong to only one Kitchen / Bar section per branch.",
    )

    @api.constrains("section_id", "product_id")
    def _check_product(self):
        for assignment in self:
            product = assignment.product_id
            if not product.is_storable:
                raise ValidationError(
                    self.env._("Only storable products can be assigned to a section.")
                )
            if product.company_id and product.company_id != assignment.company_id:
                raise ValidationError(
                    self.env._("The stock product belongs to another company.")
                )

    @api.model_create_multi
    def create(self, vals_list):
        _require_section_config_role(self.env)
        sections = self.env["restaurant.stock.section"].browse(
            [values.get("section_id") for values in vals_list if values.get("section_id")]
        )
        require_assigned_branches(sections.branch_id)
        _check_no_active_daily(self.env, sections.branch_id)
        seen = set()
        for values in vals_list:
            section = self.env["restaurant.stock.section"].browse(
                values.get("section_id")
            )
            product_id = values.get("product_id")
            key = (section.branch_id.id, product_id)
            duplicate = self.sudo().search([
                ("branch_id", "=", section.branch_id.id),
                ("product_id", "=", product_id),
            ], limit=1)
            if duplicate or key in seen:
                raise ValidationError(
                    self.env._(
                        "A stock product can belong to only one Kitchen / Bar "
                        "section per branch."
                    )
                )
            seen.add(key)
        return super().create(vals_list)

    def write(self, vals):
        _require_section_config_role(self.env)
        require_assigned_branches(self.branch_id)
        branches = self.branch_id
        if vals.get("section_id"):
            target = self.env["restaurant.stock.section"].browse(vals["section_id"])
            branches |= target.branch_id
            require_assigned_branches(branches)
        _check_no_active_daily(self.env, branches)
        return super().write(vals)

    def unlink(self):
        _require_section_config_role(self.env)
        require_assigned_branches(self.branch_id)
        _check_no_active_daily(self.env, self.branch_id)
        return super().unlink()
