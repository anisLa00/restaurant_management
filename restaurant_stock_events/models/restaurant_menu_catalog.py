from odoo import Command, api, fields, models
from odoo.exceptions import ValidationError


def _default_restaurant_branch(record):
    return record.env.user.default_restaurant_branch_id


class RestaurantMenuCategory(models.Model):
    _name = "restaurant.menu.category"
    _description = "Restaurant Menu Category"
    _order = "sequence, name, id"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    branch_id = fields.Many2one(
        "restaurant.branch",
        string="Branch",
        default=_default_restaurant_branch,
        ondelete="restrict",
        index=True,
        help=(
            "Branch that owns this menu category. Empty is retained only for "
            "legacy catalog records and cannot be used for new service events."
        ),
    )
    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
        index=True,
    )
    item_ids = fields.One2many(
        "restaurant.menu.item",
        "category_id",
        string="Menu Items",
    )

    _company_name_unique = models.UniqueIndex(
        "(company_id, branch_id, name)",
        "A menu category with this name already exists for the branch.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for vals in vals_list:
            vals = dict(vals)
            branch_id = (
                vals.get("branch_id")
                or self.env.context.get("default_branch_id")
                or self.env.user.default_restaurant_branch_id.id
            )
            branch = self.env["restaurant.branch"].browse(branch_id)
            if not branch:
                raise ValidationError(
                    self.env._("Choose the branch that owns this menu category.")
                )
            vals["branch_id"] = branch.id
            vals["company_id"] = branch.company_id.id
            prepared.append(vals)
        return super().create(prepared)

    @api.constrains("branch_id", "company_id")
    def _check_branch_company(self):
        for category in self.filtered("branch_id"):
            if category.branch_id.company_id != category.company_id:
                raise ValidationError(
                    self.env._("The menu category branch belongs to another company.")
                )


class RestaurantMenuFlavor(models.Model):
    _name = "restaurant.menu.flavor"
    _description = "Restaurant Menu Flavor"
    _order = "name, id"

    name = fields.Char(
        required=True,
        translate=True,
        help=(
            "Renaming this catalog entry does not rewrite existing "
            "product-specific Menu Item names or historical sales."
        ),
    )
    branch_id = fields.Many2one(
        "restaurant.branch",
        required=True,
        default=_default_restaurant_branch,
        ondelete="restrict",
        index=True,
    )
    company_id = fields.Many2one(
        related="branch_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    active = fields.Boolean(default=True)

    _branch_name_unique = models.UniqueIndex(
        "(company_id, branch_id, name) WHERE active IS TRUE",
        "This flavor already exists for the branch.",
    )

    @api.model
    def _name_conflict(self, branch, name, exclude_ids=()):
        if not branch or not name:
            return self.browse()
        return self.with_context(active_test=False).search([
            ("id", "not in", list(exclude_ids)),
            ("company_id", "=", branch.company_id.id),
            ("branch_id", "=", branch.id),
            ("name", "=ilike", name.strip()),
        ], limit=1)

    @api.model
    def _raise_name_conflict(self, conflict):
        if conflict.active:
            raise ValidationError(
                self.env._("This flavor already exists for the branch.")
            )
        raise ValidationError(
            self.env._(
                "This flavor already exists in Archived Flavors. Restore the "
                "archived flavor instead of creating a duplicate."
            )
        )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for values in vals_list:
            values = dict(values)
            if values.get("name"):
                values["name"] = values["name"].strip()
            branch_id = (
                values.get("branch_id")
                or self.env.context.get("default_branch_id")
                or self.env.user.default_restaurant_branch_id.id
            )
            branch = self.env["restaurant.branch"].browse(branch_id).exists()
            if not branch:
                raise ValidationError(
                    self.env._("Choose the branch that owns this flavor.")
                )
            values["branch_id"] = branch.id
            conflict = self._name_conflict(branch, values.get("name"))
            if conflict:
                self._raise_name_conflict(conflict)
            prepared.append(values)
        return super().create(prepared)

    def write(self, vals):
        if "branch_id" in vals:
            used = self.env["restaurant.menu.item"].with_context(
                active_test=False,
            ).search_count([("flavor_id", "in", self.ids)])
            if used:
                raise ValidationError(
                    self.env._(
                        "A flavor already used by Menu Setup cannot move to "
                        "another branch. Create it in the other branch instead."
                    )
                )
        values = dict(vals)
        if values.get("name"):
            values["name"] = values["name"].strip()
        for flavor in self:
            branch = (
                self.env["restaurant.branch"].browse(values["branch_id"])
                if values.get("branch_id")
                else flavor.branch_id
            )
            name = values.get("name", flavor.name)
            conflict = self._name_conflict(branch, name, exclude_ids=self.ids)
            if conflict:
                self._raise_name_conflict(conflict)
        return super().write(values)

    def unlink(self):
        self.write({"active": False})
        return True


class RestaurantMenuMappingProfile(models.Model):
    _name = "restaurant.menu.mapping.profile"
    _description = "Restaurant Menu Setup"
    _order = "branch_id, stock_product_id, id"

    name = fields.Char(required=True, translate=True)
    branch_id = fields.Many2one(
        "restaurant.branch",
        required=True,
        default=_default_restaurant_branch,
        ondelete="restrict",
        index=True,
    )
    company_id = fields.Many2one(
        related="branch_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    category_id = fields.Many2one(
        "restaurant.menu.category",
        string="Menu Category",
        ondelete="restrict",
        domain="[('branch_id', '=', branch_id)]",
        help="Category shared by the flavors in this Menu Setup.",
    )
    stock_product_id = fields.Many2one(
        "product.product",
        string="Stock Product",
        required=True,
        ondelete="restrict",
        domain="[('is_storable', '=', True)]",
        index=True,
        help=(
            "Native stock product shared by the selected menu flavors. "
            "Quantities remain explicit per Standard, Half, or Full size."
        ),
    )
    size_applicable = fields.Boolean(
        string="Uses Half / Full Sizes",
        help=(
            "Enable when this menu item is sold as Half and Full. Leave it "
            "disabled for a single Standard size."
        ),
    )
    stock_uom_id = fields.Many2one(
        "uom.uom",
        string="Stock UoM",
        help=(
            "Unit used for the verified quantity consumed by one menu sale. "
            "No quantity is inferred from the size label."
        ),
    )
    standard_stock_qty = fields.Float(
        string="Standard Quantity",
        default=1.0,
        help="Verified stock quantity consumed by one Standard sale.",
    )
    half_stock_qty = fields.Float(
        string="Half Quantity",
        default=0.5,
        help="Verified stock quantity consumed by one Half sale.",
    )
    full_stock_qty = fields.Float(
        string="Full Quantity",
        default=1.0,
        help="Verified stock quantity consumed by one Full sale.",
    )
    allow_additional_consumption = fields.Boolean(
        string="Allow Additional Consumption",
        default=False,
        help=(
            "Allow a separate manual Consumption quantity on this product's "
            "Menu / Sold Daily Stock row. Use it only for preparation that is "
            "not already included in mapped Sold quantities. The setting is "
            "branch-specific and each new daily quantity starts at zero."
        ),
    )
    line_ids = fields.One2many(
        "restaurant.menu.mapping.profile.line",
        "profile_id",
        string="Legacy Size Quantities",
        readonly=True,
        copy=False,
        help="Preserved only for compatibility with earlier Menu Setup data.",
    )
    item_ids = fields.One2many(
        "restaurant.menu.item",
        "mapping_profile_id",
        string="Flavors",
    )
    flavor_ids = fields.Many2many(
        "restaurant.menu.flavor",
        "restaurant_menu_setup_flavor_rel",
        "setup_id",
        "flavor_id",
        string="Flavor Catalog",
        domain="[('branch_id', '=', branch_id), ('active', '=', True)]",
        help=(
            "Reusable branch flavor definitions selected for this Stock Product. "
            "Each selected flavor keeps its own product-specific variants and "
            "stock mappings."
        ),
    )
    configuration_complete = fields.Boolean(
        compute="_compute_configuration_complete",
        string="Ready",
    )
    active = fields.Boolean(default=True)

    _branch_name_unique = models.UniqueIndex(
        "(company_id, branch_id, name) WHERE active IS TRUE",
        "A Menu Setup with this name is already active for the branch. "
        "Open the existing Menu Setup instead of creating a duplicate.",
    )

    @api.depends(
        "category_id", "stock_product_id", "stock_uom_id", "size_applicable",
        "standard_stock_qty", "half_stock_qty", "full_stock_qty",
        "flavor_ids", "item_ids.active",
    )
    def _compute_configuration_complete(self):
        for profile in self:
            quantities = (
                (profile.half_stock_qty, profile.full_stock_qty)
                if profile.size_applicable
                else (profile.standard_stock_qty,)
            )
            profile.configuration_complete = bool(
                profile.stock_product_id
                and profile.stock_uom_id
                and profile.category_id
                and all(quantity > 0 for quantity in quantities)
                and profile.flavor_ids
                and any(profile.item_ids.mapped("active"))
            )

    @api.onchange("stock_product_id")
    def _onchange_stock_product_id(self):
        if self.stock_product_id:
            self.stock_uom_id = self.stock_product_id.uom_id
            if not self.name:
                self.name = self.stock_product_id.display_name

    def action_manage_flavors(self):
        self.ensure_one()
        action = self.env["ir.actions.actions"]._for_xml_id(
            "restaurant_stock_events.restaurant_menu_flavor_action"
        )
        action.update({
            "domain": [("branch_id", "=", self.branch_id.id)],
            "context": {
                "default_branch_id": self.branch_id.id,
                "search_default_branch_id": self.branch_id.id,
            },
        })
        return action

    @api.model
    def _find_or_create_branch_flavor(self, branch, name):
        Flavor = self.env["restaurant.menu.flavor"].with_context(
            active_test=False,
        )
        flavor = Flavor.search([
            ("company_id", "=", branch.company_id.id),
            ("branch_id", "=", branch.id),
            ("name", "=", name),
        ], order="active desc, id", limit=1)
        if flavor:
            if not flavor.active:
                flavor.write({"active": True})
            return flavor
        return Flavor.create({
            "name": name,
            "branch_id": branch.id,
        })

    @api.model
    def _plain_flavor(self, branch):
        Flavor = self.env["restaurant.menu.flavor"].with_context(
            active_test=False,
        )
        flavor = Flavor.search([
            ("company_id", "=", branch.company_id.id),
            ("branch_id", "=", branch.id),
            ("name", "=ilike", "Plain"),
        ], order="active desc, id", limit=1)
        if flavor:
            if not flavor.active:
                flavor.write({"active": True})
            return flavor
        return Flavor.create({
            "name": "Plain",
            "branch_id": branch.id,
        })

    def _adopt_item_flavors(self):
        Item = self.env["restaurant.menu.item"].with_context(active_test=False)
        for setup in self:
            flavors = Item.search([
                ("mapping_profile_id", "=", setup.id),
                ("flavor_id", "!=", False),
            ]).flavor_id
            if flavors:
                selected = setup.flavor_ids.with_context(active_test=False) | flavors
                super(RestaurantMenuMappingProfile, setup).write({
                    "flavor_ids": [Command.set(selected.ids)],
                })

    def _ensure_flavor_selection(self):
        for setup in self.filtered("active"):
            if not setup.flavor_ids:
                plain = self._plain_flavor(setup.branch_id)
                super(RestaurantMenuMappingProfile, setup).write({
                    "flavor_ids": [Command.set(plain.ids)],
                })

    @api.model
    def _guard_open_daily_mode_change(self, branch_product_pairs):
        Line = self.env["restaurant.stock.daily.line"].sudo()
        blocking = Line.browse()
        for branch_id, product_id in set(branch_product_pairs):
            if not branch_id or not product_id:
                continue
            blocking |= Line.search([
                ("branch_id", "=", branch_id),
                ("product_id", "=", product_id),
                ("section_id", "!=", False),
                ("daily_state", "in", ("opened", "closing_review")),
            ])
        if blocking:
            sheets = blocking.mapped("daily_id")
            raise ValidationError(
                self.env._(
                    "Menu mapping or Consumption mode cannot change while "
                    "these Daily Stock sheets are open: %s. Finish or return "
                    "the sheets before changing setup.",
                    ", ".join(sheets.mapped("display_name")),
                )
            )
        return True

    @api.model
    def _refresh_draft_daily_modes(self, branch_product_pairs):
        Line = self.env["restaurant.stock.daily.line"].sudo()
        lines = Line.browse()
        for branch_id, product_id in set(branch_product_pairs):
            if not branch_id or not product_id:
                continue
            lines |= Line.search([
                ("branch_id", "=", branch_id),
                ("product_id", "=", product_id),
                ("daily_state", "=", "draft"),
            ])
        lines._refresh_operation_mode_from_setup()
        return True

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        affected_pairs = set()
        for values in vals_list:
            values = dict(values)
            product = self.env["product.product"].browse(
                values.get("stock_product_id")
            )
            if product:
                values.setdefault("name", product.display_name)
                values.setdefault("stock_uom_id", product.uom_id.id)
            branch = self.env["restaurant.branch"].browse(
                values.get("branch_id")
                or self.env.user.default_restaurant_branch_id.id
            )
            if values.get("active", True) and values.get("name") and self.search([
                ("company_id", "=", branch.company_id.id),
                ("branch_id", "=", branch.id),
                ("name", "=", values["name"]),
                ("active", "=", True),
            ], limit=1):
                raise ValidationError(
                    self.env._(
                        "A Menu Setup with this name is already active for the "
                        "branch. Open the existing Menu Setup instead of "
                        "creating a duplicate."
                    )
                )
            if values.get("active", True) and branch and product:
                affected_pairs.add((branch.id, product.id))
            prepared.append(values)
        self._guard_open_daily_mode_change(affected_pairs)
        records = super(
            RestaurantMenuMappingProfile,
            self.with_context(menu_base_sync_deferred=True),
        ).create(prepared)
        if not self.env.context.get("menu_setup_migration"):
            records._adopt_item_flavors()
            records._ensure_flavor_selection()
            records._sync_menu_items()
        self._refresh_draft_daily_modes(affected_pairs)
        return records

    def write(self, vals):
        classification_fields = {
            "active",
            "branch_id",
            "category_id",
            "stock_product_id",
            "stock_uom_id",
            "size_applicable",
            "standard_stock_qty",
            "half_stock_qty",
            "full_stock_qty",
            "flavor_ids",
            "item_ids",
            "allow_additional_consumption",
        }
        affected_pairs = {
            (setup.branch_id.id, setup.stock_product_id.id)
            for setup in self
        }
        if set(vals) & classification_fields:
            future_branch_id = vals.get("branch_id")
            future_product_id = vals.get("stock_product_id")
            affected_pairs |= {
                (
                    future_branch_id or setup.branch_id.id,
                    future_product_id or setup.stock_product_id.id,
                )
                for setup in self
            }
            self._guard_open_daily_mode_change(affected_pairs)

        if vals.get("active") is True:
            for setup in self:
                duplicate = self.search([
                    ("id", "!=", setup.id),
                    ("company_id", "=", setup.company_id.id),
                    ("branch_id", "=", setup.branch_id.id),
                    ("name", "=", setup.name),
                    ("active", "=", True),
                ], limit=1)
                if duplicate:
                    raise ValidationError(
                        self.env._(
                            "A Menu Setup with this name is already active for "
                            "the branch. Open the existing Menu Setup instead "
                            "of creating a duplicate."
                        )
                    )
        if "branch_id" in vals and any(
            setup.item_ids or setup.flavor_ids for setup in self
        ):
            raise ValidationError(
                self.env._(
                    "Archive this Menu Setup and create a new one to change "
                    "its branch."
                )
            )
        if (
            "stock_product_id" in vals
            and not self.env.context.get("menu_setup_migration")
        ):
            for profile in self:
                if (
                    profile.stock_product_id.id != vals.get("stock_product_id")
                    and profile.item_ids.variant_ids.mapping_ids
                ):
                    raise ValidationError(
                        self.env._(
                            "The Stock Product is already used by configured "
                            "flavors. Create a new Menu Setup instead so existing "
                            "sales conversions and history stay unchanged."
                        )
                    )

        setup_fields = {
            "stock_product_id", "stock_uom_id", "size_applicable",
            "standard_stock_qty", "half_stock_qty", "full_stock_qty",
        }
        result = super(
            RestaurantMenuMappingProfile,
            self.with_context(menu_base_sync_deferred=True),
        ).write(vals)
        if vals.get("active") is False:
            items = self.env["restaurant.menu.item"].with_context(
                active_test=False,
            ).search([("mapping_profile_id", "in", self.ids)])
            items.with_context(menu_base_sync_deferred=True).write({"active": False})
            items.variant_ids.with_context(active_test=False).write({"active": False})
        elif not self.env.context.get("menu_setup_migration"):
            if "item_ids" in vals and "flavor_ids" not in vals:
                self._adopt_item_flavors()
            if set(vals) & {"flavor_ids", "item_ids", "active"}:
                self._ensure_flavor_selection()
            if set(vals) & (
                setup_fields | {"category_id", "flavor_ids", "item_ids", "active"}
            ):
                self._sync_menu_items()
        if set(vals) & classification_fields:
            self._refresh_draft_daily_modes(affected_pairs)
        return result

    def _adopt_integrated_values_from_existing_lines(self):
        """Expose legacy quantities in Menu Setup without inventing values."""
        field_by_size = {
            "standard": "standard_stock_qty",
            "half": "half_stock_qty",
            "full": "full_stock_qty",
        }
        for profile in self:
            if not profile.line_ids:
                continue
            stock_uom = profile.stock_uom_id or profile.stock_product_id.uom_id
            values = {}
            if not profile.stock_uom_id:
                values["stock_uom_id"] = stock_uom.id
            if (
                not profile.size_applicable
                and profile.line_ids.filtered(lambda line: line.size in ("half", "full"))
            ):
                values["size_applicable"] = True
            for line in profile.line_ids:
                field_name = field_by_size[line.size]
                if not profile[field_name]:
                    values[field_name] = line.stock_uom_id._compute_quantity(
                        line.stock_qty,
                        stock_uom,
                        round=False,
                    )
            if values:
                super(RestaurantMenuMappingProfile, profile).write(values)

    def _validate_menu_setup(self):
        for setup in self:
            if not setup.category_id:
                raise ValidationError(
                    self.env._(
                        "Choose or quickly create one Menu Category in this form."
                    )
                )
            if setup.category_id.branch_id != setup.branch_id:
                raise ValidationError(
                    self.env._("The Menu Category must belong to this branch.")
                )
            if not setup.flavor_ids:
                raise ValidationError(
                    self.env._("Select at least one flavor for this Menu Setup.")
                )
            if any(flavor.branch_id != setup.branch_id for flavor in setup.flavor_ids):
                raise ValidationError(
                    self.env._("Every selected flavor must belong to this branch.")
                )
            quantities = (
                (setup.half_stock_qty, setup.full_stock_qty)
                if setup.size_applicable
                else (setup.standard_stock_qty,)
            )
            if not setup.stock_product_id or not setup.stock_uom_id or any(
                quantity <= 0 for quantity in quantities
            ):
                required = (
                    self.env._("Half and Full quantities")
                    if setup.size_applicable
                    else self.env._("the Standard quantity")
                )
                raise ValidationError(
                    self.env._(
                        "Enter a verified Stock UoM and %(quantities)s. "
                        "Nothing is inferred from Half, Full, or the item name.",
                        quantities=required,
                    )
                )

    def _sync_menu_items(self):
        Item = self.env["restaurant.menu.item"].with_context(active_test=False)
        Variant = self.env["restaurant.menu.variant"]
        Mapping = self.env["restaurant.menu.stock.mapping"]
        for setup in self.filtered("active"):
            setup._validate_menu_setup()
            all_items = Item.search([("mapping_profile_id", "=", setup.id)])
            selected_items = Item.browse()
            for flavor in setup.flavor_ids:
                candidates = all_items.filtered(
                    lambda current, selected=flavor: current.flavor_id == selected
                )
                item = candidates.filtered("active")[:1] or candidates[:1]
                if not item:
                    legacy = all_items.filtered(
                        lambda current, selected=flavor: (
                            not current.flavor_id and current.name == selected.name
                        )
                    )[:1]
                    if legacy:
                        legacy.with_context(menu_base_sync_deferred=True).write({
                            "flavor_id": flavor.id,
                        })
                        item = legacy
                    else:
                        item = Item.with_context(
                            menu_base_sync_deferred=True,
                        ).create({
                            "name": flavor.name,
                            "flavor_id": flavor.id,
                            "category_id": setup.category_id.id,
                            "mapping_profile_id": setup.id,
                            "size_applicable": setup.size_applicable,
                        })
                        all_items |= item
                item_values = {}
                if not item.active:
                    item_values["active"] = True
                if item.category_id != setup.category_id:
                    item_values["category_id"] = setup.category_id.id
                if item.size_applicable != setup.size_applicable:
                    item_values["size_applicable"] = setup.size_applicable
                if item_values:
                    item.with_context(menu_base_sync_deferred=True).write(item_values)
                selected_items |= item

            deselected = all_items - selected_items
            active_deselected = deselected.filtered("active")
            if active_deselected:
                active_deselected.with_context(
                    menu_base_sync_deferred=True,
                ).write({"active": False})
                active_deselected.variant_ids.with_context(
                    active_test=False,
                ).write({"active": False})

            desired_sizes = (
                ("half", "full") if setup.size_applicable else ("standard",)
            )
            quantities = {
                "standard": setup.standard_stock_qty,
                "half": setup.half_stock_qty,
                "full": setup.full_stock_qty,
            }
            for item in selected_items:
                variants = Variant.with_context(active_test=False).search([
                    ("item_id", "=", item.id),
                ])
                obsolete = variants.filtered(
                    lambda variant: variant.size not in desired_sizes and variant.active
                )
                if obsolete:
                    obsolete.write({"active": False})

                for size in desired_sizes:
                    variant = variants.filtered(
                        lambda current, wanted=size: current.size == wanted
                    )[:1]
                    if variant and not variant.active:
                        variant.write({"active": True})
                    elif not variant:
                        variant = Variant.create({
                            "item_id": item.id,
                            "size": size,
                        })

                    other_mappings = variant.mapping_ids.filtered(
                        lambda mapping: mapping.stock_product_id != setup.stock_product_id
                    )
                    if other_mappings:
                        raise ValidationError(
                            self.env._(
                                "%s already has a preserved mapping to another "
                                "stock product. Archive it and add a new flavor; "
                                "Menu Setup never replaces history silently.",
                                variant.display_name,
                            )
                        )

                    mapping = variant.mapping_ids.filtered(
                        lambda current: current.stock_product_id == setup.stock_product_id
                    )[:1]
                    mapping_values = {
                        "stock_qty": quantities[size],
                        "stock_uom_id": setup.stock_uom_id.id,
                        "setup_id": setup.id,
                    }
                    if not mapping:
                        Mapping.create({
                            "variant_id": variant.id,
                            "stock_product_id": setup.stock_product_id.id,
                            **mapping_values,
                        })
                        continue

                    legacy_line = mapping.source_profile_line_id
                    legacy_managed = bool(
                        legacy_line
                        and legacy_line.profile_id == setup
                        and mapping.stock_uom_id == legacy_line.stock_uom_id
                        and mapping.stock_qty == legacy_line.stock_qty
                    )
                    if mapping.setup_id == setup or legacy_managed:
                        mapping.write(mapping_values)
                    elif (
                        mapping.stock_uom_id == setup.stock_uom_id
                        and mapping.stock_qty == quantities[size]
                    ):
                        mapping.write({"setup_id": setup.id})

    @api.constrains(
        "branch_id", "category_id", "stock_product_id", "stock_uom_id",
        "flavor_ids",
    )
    def _check_stock_product(self):
        for profile in self:
            product = profile.stock_product_id
            if not product.is_storable:
                raise ValidationError(
                    self.env._("Menu Setup requires a storable Stock Product.")
                )
            if product.company_id and product.company_id != profile.company_id:
                raise ValidationError(
                    self.env._(
                        "The Menu Setup Stock Product belongs to another company."
                    )
                )
            if profile.category_id and profile.category_id.branch_id != profile.branch_id:
                raise ValidationError(
                    self.env._(
                        "The Menu Category must belong to the Menu Item branch."
                    )
                )
            if any(
                flavor.branch_id != profile.branch_id
                for flavor in profile.flavor_ids
            ):
                raise ValidationError(
                    self.env._("Every selected flavor must belong to this branch.")
                )
            if (
                profile.stock_uom_id
                and not profile.stock_uom_id._has_common_reference(product.uom_id)
            ):
                raise ValidationError(
                    self.env._(
                        "The Stock UoM must be convertible with the selected "
                        "Stock Product UoM."
                    )
                )
            incompatible = profile.line_ids.filtered(
                lambda line: not line.stock_uom_id._has_common_reference(
                    product.uom_id
                )
            )
            if incompatible:
                raise ValidationError(
                    self.env._(
                        "Every legacy setup UoM must be convertible with the Stock "
                        "Product UoM."
                    )
                )

    def unlink(self):
        self.write({"active": False})
        return True


class RestaurantMenuMappingProfileLine(models.Model):
    _name = "restaurant.menu.mapping.profile.line"
    _description = "Legacy Restaurant Menu Setup Quantity"
    _order = "profile_id, size, id"

    profile_id = fields.Many2one(
        "restaurant.menu.mapping.profile",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(
        related="profile_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    branch_id = fields.Many2one(
        related="profile_id.branch_id",
        store=True,
        readonly=True,
        index=True,
    )
    size = fields.Selection(
        [
            ("standard", "Standard"),
            ("half", "Half"),
            ("full", "Full"),
        ],
        required=True,
        index=True,
    )
    stock_qty = fields.Float(
        string="Stock Quantity per Menu Unit",
        required=True,
        help="Enter the verified recipe quantity. No quantity is inferred.",
    )
    stock_uom_id = fields.Many2one(
        "uom.uom",
        string="Stock Mapping UoM",
        required=True,
    )
    note = fields.Char()

    _profile_size_unique = models.UniqueIndex(
        "(profile_id, size)",
        "Each size can only be preserved once per legacy Menu Setup.",
    )

    @api.onchange("profile_id")
    def _onchange_profile_id(self):
        if self.profile_id.stock_product_id and not self.stock_uom_id:
            self.stock_uom_id = self.profile_id.stock_product_id.uom_id

    @api.constrains("profile_id", "stock_qty", "stock_uom_id")
    def _check_quantity_and_uom(self):
        for line in self:
            if line.stock_qty <= 0:
                raise ValidationError(
                    self.env._("Legacy setup quantity must be greater than zero.")
                )
            if not line.stock_uom_id._has_common_reference(
                line.profile_id.stock_product_id.uom_id
            ):
                raise ValidationError(
                    self.env._(
                        "The profile UoM must be convertible with the stock product UoM."
                    )
                )


class RestaurantMenuItem(models.Model):
    _name = "restaurant.menu.item"
    _description = "Restaurant Menu Item"
    _order = "category_id, name, id"

    name = fields.Char(required=True, translate=True)
    flavor_id = fields.Many2one(
        "restaurant.menu.flavor",
        string="Flavor Definition",
        ondelete="restrict",
        index=True,
        help=(
            "Reusable branch flavor definition. The Menu Item remains the "
            "product-specific historical identity used by sales and stock."
        ),
    )
    category_id = fields.Many2one(
        "restaurant.menu.category",
        required=True,
        ondelete="restrict",
        index=True,
    )
    company_id = fields.Many2one(
        related="category_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    branch_id = fields.Many2one(
        related="category_id.branch_id",
        store=True,
        readonly=True,
        index=True,
    )
    size_applicable = fields.Boolean(
        string="Uses Half / Full Sizes",
        help=(
            "Enable when Reception must choose Half or Full. Leave disabled "
            "for drinks, refreshments, and other standard-size items."
        ),
    )
    active = fields.Boolean(default=True)
    variant_ids = fields.One2many(
        "restaurant.menu.variant",
        "item_id",
        string="Sizes / Variants",
    )
    mapping_profile_id = fields.Many2one(
        "restaurant.menu.mapping.profile",
        string="Menu Setup",
        ondelete="set null",
        domain="[('branch_id', '=', branch_id), ('active', '=', True)]",
        help="Menu Setup that owns this flavor and its deterministic sizes.",
    )
    stock_product_id = fields.Many2one(
        "product.product",
        related="mapping_profile_id.stock_product_id",
        string="Native Stock Product",
        store=True,
        readonly=True,
        index=True,
    )

    _company_category_name_unique = models.UniqueIndex(
        "(company_id, branch_id, category_id, name) "
        "WHERE active IS TRUE AND stock_product_id IS NULL",
        "This legacy flavor is already active in the Menu Category.",
    )
    _company_product_name_unique = models.UniqueIndex(
        "(company_id, branch_id, stock_product_id, name) "
        "WHERE active IS TRUE AND stock_product_id IS NOT NULL",
        "This flavor is already active for the selected Stock Product. Open "
        "its existing Menu Setup instead of creating a duplicate.",
    )
    _company_category_flavor_unique = models.UniqueIndex(
        "(company_id, branch_id, category_id, flavor_id) "
        "WHERE active IS TRUE AND stock_product_id IS NULL "
        "AND flavor_id IS NOT NULL",
        "This flavor is already active in the Menu Category.",
    )
    _company_product_flavor_unique = models.UniqueIndex(
        "(company_id, branch_id, stock_product_id, flavor_id) "
        "WHERE active IS TRUE AND stock_product_id IS NOT NULL "
        "AND flavor_id IS NOT NULL",
        "This flavor is already active for the selected Stock Product. Open "
        "its existing Menu Setup instead of creating a duplicate.",
    )

    @api.depends("name", "mapping_profile_id.stock_product_id")
    def _compute_display_name(self):
        for item in self:
            product = item.mapping_profile_id.stock_product_id
            item.display_name = (
                "%s — %s" % (product.display_name, item.name)
                if product
                else item.name
            )

    @api.model_create_multi
    def create(self, vals_list):
        prepared = []
        for vals in vals_list:
            vals = dict(vals)
            profile = self.env["restaurant.menu.mapping.profile"].browse(
                vals.get("mapping_profile_id")
            )
            if profile and profile.category_id:
                vals.setdefault("category_id", profile.category_id.id)
                vals["size_applicable"] = profile.size_applicable
            category = self.env["restaurant.menu.category"].browse(
                vals.get("category_id")
            )
            flavor = self.env["restaurant.menu.flavor"].browse(
                vals.get("flavor_id")
            ).exists()
            if flavor:
                if category and flavor.branch_id != category.branch_id:
                    raise ValidationError(
                        self.env._("The flavor definition must belong to this branch.")
                    )
                vals.setdefault("name", flavor.name)
            elif category and category.branch_id and vals.get("name"):
                flavor = self.env[
                    "restaurant.menu.mapping.profile"
                ]._find_or_create_branch_flavor(category.branch_id, vals["name"])
                vals["flavor_id"] = flavor.id

            if vals.get("active", True) and category and vals.get("name"):
                duplicate_domain = [
                    ("company_id", "=", category.company_id.id),
                    ("branch_id", "=", category.branch_id.id),
                    ("active", "=", True),
                ]
                if flavor:
                    duplicate_domain.append(("flavor_id", "=", flavor.id))
                else:
                    duplicate_domain.append(("name", "=", vals["name"]))
                if profile:
                    duplicate_domain.append((
                        "stock_product_id", "=", profile.stock_product_id.id,
                    ))
                else:
                    duplicate_domain.extend([
                        ("stock_product_id", "=", False),
                        ("category_id", "=", category.id),
                    ])
                if self.search(duplicate_domain, limit=1):
                    raise ValidationError(
                        self.env._(
                            "This flavor is already active for the selected "
                            "Stock Product. Open its existing Menu Setup "
                            "instead of creating a duplicate."
                        )
                    )
            prepared.append(vals)
        records = super().create(prepared)
        if not self.env.context.get("menu_base_sync_deferred"):
            for setup in records.mapping_profile_id.filtered("category_id"):
                setup._sync_menu_items()
        return records

    def write(self, vals):
        if not self.env.context.get("menu_base_sync_deferred"):
            for item in self.filtered(lambda current: current.mapping_profile_id.category_id):
                setup = item.mapping_profile_id
                if (
                    "category_id" in vals
                    and vals["category_id"] != setup.category_id.id
                ):
                    raise ValidationError(
                        self.env._("Flavor category is controlled by Menu Setup.")
                    )
                if (
                    "size_applicable" in vals
                    and vals["size_applicable"] != setup.size_applicable
                ):
                    raise ValidationError(
                        self.env._("Flavor portions are controlled by Menu Setup.")
                    )
        result = super().write(vals)
        if (
            not self.env.context.get("menu_base_sync_deferred")
            and "mapping_profile_id" in vals
        ):
            for setup in self.mapping_profile_id.filtered("category_id"):
                setup._sync_menu_items()
        return result

    @api.constrains("mapping_profile_id", "category_id", "flavor_id")
    def _check_mapping_profile(self):
        for item in self:
            if item.flavor_id and item.flavor_id.branch_id != item.branch_id:
                raise ValidationError(
                    self.env._("The flavor definition must belong to this branch.")
                )
            if not item.mapping_profile_id:
                continue
            if item.mapping_profile_id.branch_id != item.branch_id:
                raise ValidationError(
                    self.env._("Menu Setup must belong to the flavor branch.")
                )
            if item.mapping_profile_id.company_id != item.company_id:
                raise ValidationError(
                    self.env._("Menu Setup must belong to the flavor company.")
                )
            if (
                item.mapping_profile_id.category_id
                and item.mapping_profile_id.category_id != item.category_id
            ):
                raise ValidationError(
                    self.env._("The flavor must use its Menu Setup category.")
                )

    def unlink(self):
        self.write({"active": False})
        self.with_context(active_test=False).variant_ids.write({"active": False})
        return True


class RestaurantMenuVariant(models.Model):
    _name = "restaurant.menu.variant"
    _description = "Restaurant Menu Variant"
    _order = "item_id, size, id"

    item_id = fields.Many2one(
        "restaurant.menu.item",
        required=True,
        ondelete="cascade",
        index=True,
    )
    category_id = fields.Many2one(
        related="item_id.category_id",
        store=True,
        readonly=True,
        index=True,
    )
    company_id = fields.Many2one(
        related="item_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    branch_id = fields.Many2one(
        related="item_id.branch_id",
        store=True,
        readonly=True,
        index=True,
    )
    size = fields.Selection(
        [
            ("standard", "Standard"),
            ("half", "Half"),
            ("full", "Full"),
        ],
        required=True,
        default="standard",
        index=True,
    )
    active = fields.Boolean(default=True)
    mapping_ids = fields.One2many(
        "restaurant.menu.stock.mapping",
        "variant_id",
        string="Stock Mapping",
    )

    _item_size_unique = models.UniqueIndex(
        "(item_id, size)",
        "This size already exists for the menu item.",
    )

    @api.depends("item_id.name", "size")
    def _compute_display_name(self):
        labels = dict(self._fields["size"].selection)
        for variant in self:
            variant.display_name = "%s — %s" % (
                variant.item_id.display_name,
                labels.get(variant.size, variant.size),
            )

    @api.constrains("item_id", "size")
    def _check_size_policy(self):
        for variant in self:
            if variant.item_id.size_applicable and variant.size == "standard":
                raise ValidationError(
                    self.env._(
                        "%s uses portion sizes. Configure Half and/or Full "
                        "instead of Standard.",
                        variant.item_id.display_name,
                    )
                )

            if not variant.item_id.size_applicable and variant.size != "standard":
                raise ValidationError(
                    self.env._(
                        "%s does not use portion sizes. Configure the Standard "
                        "variant.",
                        variant.item_id.display_name,
                    )
                )

    def unlink(self):
        self.write({"active": False})
        return True


class RestaurantMenuStockMapping(models.Model):
    _name = "restaurant.menu.stock.mapping"
    _description = "Restaurant Menu Variant Stock Mapping"
    _order = "variant_id, stock_product_id, id"

    variant_id = fields.Many2one(
        "restaurant.menu.variant",
        required=True,
        ondelete="cascade",
        index=True,
    )
    company_id = fields.Many2one(
        related="variant_id.company_id",
        store=True,
        readonly=True,
        index=True,
    )
    branch_id = fields.Many2one(
        related="variant_id.branch_id",
        store=True,
        readonly=True,
        index=True,
    )
    stock_product_id = fields.Many2one(
        "product.product",
        string="Parent Stock Product",
        required=True,
        ondelete="restrict",
        domain="[('is_storable', '=', True)]",
        index=True,
        help=(
            "Select the exact native stock product consumed by this menu "
            "flavor and size. Different flavors may explicitly reference "
            "the same parent stock product. No mapping is inferred by name."
        ),
    )
    stock_qty = fields.Float(
        string="Stock Quantity per Menu Unit",
        required=True,
        help="Explicit verified conversion; no 1:1 quantity is assumed.",
    )
    stock_uom_id = fields.Many2one(
        "uom.uom",
        string="Stock Mapping UoM",
        required=True,
    )
    note = fields.Char()
    setup_id = fields.Many2one(
        "restaurant.menu.mapping.profile",
        string="Menu Setup",
        ondelete="set null",
        readonly=True,
        index=True,
        help="Menu Setup that deterministically maintains this conversion.",
    )
    source_profile_line_id = fields.Many2one(
        "restaurant.menu.mapping.profile.line",
        string="Legacy Setup Source",
        ondelete="set null",
        readonly=True,
        help=(
            "Preserved reference to the earlier profile-based configuration."
        ),
    )

    _variant_product_unique = models.UniqueIndex(
        "(variant_id, stock_product_id)",
        "A stock product can only be mapped once per menu variant.",
    )

    @api.onchange("stock_product_id")
    def _onchange_stock_product_id(self):
        if self.stock_product_id:
            self.stock_uom_id = self.stock_product_id.uom_id

    @api.constrains(
        "variant_id",
        "stock_product_id",
        "stock_qty",
        "stock_uom_id",
    )
    def _check_mapping(self):
        for mapping in self:
            if mapping.stock_qty <= 0:
                raise ValidationError(
                    self.env._("Mapped stock quantity must be greater than zero.")
                )
            if not mapping.stock_product_id.is_storable:
                raise ValidationError(
                    self.env._("Menu variants can only map to storable products.")
                )
            if (
                mapping.stock_product_id.company_id
                and mapping.stock_product_id.company_id != mapping.company_id
            ):
                raise ValidationError(
                    self.env._(
                        "The mapped stock product belongs to another company."
                    )
                )
            if not mapping.stock_uom_id._has_common_reference(
                mapping.stock_product_id.uom_id
            ):
                raise ValidationError(
                    self.env._(
                        "The mapping UoM must be convertible with the stock "
                        "product UoM."
                    )
                )
