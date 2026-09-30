Restaurant Core
===============

Odoo 20 foundation for restaurant branches. No reception functionality is included.

Models and access
-----------------

- ``restaurant.branch``: name, company-scoped unique code, company, archive flag,
  and assigned users.
- ``res.users.restaurant_branch_ids``: explicit branch assignments, editable only
  by Settings administrators on Settings / Users / Access Rights.
- Restaurant / User: read assigned branches in currently selected companies.
  No assignments means no branch access.
- Restaurant / Manager: create, read, update, archive and delete branches in
  currently selected companies; does not grant user administration.
- Settings administrators: branch administration and assignment management,
  subject to company restrictions outside superuser mode.
- Other internal users, portal users and public users have no branch access.

Assignments must belong to the user's allowed companies. Validation also covers
archived branches/users, company removal, and branch company changes.

Odoo 20 uses ``ir.access`` for both ACL permissions and record restrictions. The
company restriction applies to all CRUD operations. The assigned-user domain is
queried through the relation table, so assignment changes do not depend on a
cached list of branch IDs. No restaurant role is assigned automatically.

UI
--

Restaurant / Branches provides list, form and search views, archive filtering,
and company grouping. Assign a Restaurant role on the user form to enable access.

Tests
-----

Run from the Odoo repository, targeting a dedicated test database::

   .venv/bin/python odoo-bin -c odoo.conf -d restaurant_core_test_20260929 \
     -i restaurant_core --test-enable --test-tags /restaurant_core \
     --stop-after-init --no-http --without-demo

Tests cover user isolation, manager CRUD and archiving, all-operation company
restrictions, unauthorized roles, self-assignment prevention, immediate access
revocation, company consistency, selected companies, and branch constraints.
