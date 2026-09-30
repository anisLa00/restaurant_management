from odoo import SUPERUSER_ID


BRANCHES = (
    ('restaurant_core.branch_mirdif', 'MIR'),
    ('restaurant_core.branch_jafilya', 'JAF'),
    ('restaurant_core.branch_global_village', 'GV'),
)

USERS = (
    ('restaurant_core.user_owner_test', 'owner.test@restaurant.local'),
    ('restaurant_core.user_operations_test', 'operations.test@restaurant.local'),
    ('restaurant_core.user_mirdif_manager', 'mirdif.manager@restaurant.local'),
    ('restaurant_core.user_mirdif_reception', 'mirdif.reception@restaurant.local'),
    ('restaurant_core.user_jafilya_manager', 'jafilya.manager@restaurant.local'),
    ('restaurant_core.user_jafilya_reception', 'jafilya.reception@restaurant.local'),
    ('restaurant_core.user_global_village_manager', 'globalvillage.manager@restaurant.local'),
    ('restaurant_core.user_global_village_reception', 'globalvillage.reception@restaurant.local'),
    ('restaurant_core.user_hr_test', 'hr.test@restaurant.local'),
    ('restaurant_core.user_purchasing_test', 'purchasing.test@restaurant.local'),
    ('restaurant_core.user_accountant_test', 'accountant.test@restaurant.local'),
)


def _xmlid(cr, xmlid):
    module, name = xmlid.split('.', 1)
    cr.execute(
        'SELECT model, res_id FROM ir_model_data WHERE module = %s AND name = %s',
        (module, name),
    )
    return cr.fetchone()


def _bind_existing(cr, xmlid, model, table, natural_ids, *, noupdate):
    if len(natural_ids) > 1:
        raise RuntimeError(f'Multiple {model} records match the natural key for {xmlid}.')

    existing = _xmlid(cr, xmlid)
    if existing and existing[0] != model:
        raise RuntimeError(f'{xmlid} points to {existing[0]}, expected {model}.')

    xmlid_id = existing[1] if existing else None
    natural_id = natural_ids[0] if natural_ids else None
    if xmlid_id and natural_id and xmlid_id != natural_id:
        raise RuntimeError(
            f'{xmlid} and its natural key point to different {model} records.'
        )

    record_id = xmlid_id or natural_id
    if xmlid_id:
        cr.execute(f'SELECT 1 FROM {table} WHERE id = %s', (xmlid_id,))
        if not cr.fetchone():
            raise RuntimeError(f'{xmlid} points to a missing {model} record.')
    elif record_id:
        module, name = xmlid.split('.', 1)
        cr.execute(
            '''
            INSERT INTO ir_model_data
                (module, name, model, res_id, noupdate,
                 create_uid, write_uid, create_date, write_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            ''',
            (module, name, model, record_id, noupdate,
             SUPERUSER_ID, SUPERUSER_ID),
        )
    return record_id


def migrate(cr, version):
    main_company = _xmlid(cr, 'base.main_company')
    if not main_company or main_company[0] != 'res.company':
        raise RuntimeError('base.main_company is missing or invalid.')
    company_id = main_company[1]

    for xmlid, code in BRANCHES:
        cr.execute(
            'SELECT id FROM restaurant_branch WHERE company_id = %s AND code = %s',
            (company_id, code),
        )
        record_id = _bind_existing(
            cr, xmlid, 'restaurant.branch', 'restaurant_branch',
            [row[0] for row in cr.fetchall()], noupdate=True,
        )
        if record_id:
            cr.execute(
                'SELECT company_id, code FROM restaurant_branch WHERE id = %s',
                (record_id,),
            )
            if cr.fetchone() != (company_id, code):
                raise RuntimeError(
                    f'{xmlid} does not point to main-company branch {code}.'
                )

    admin = _xmlid(cr, 'base.user_admin')
    protected_ids = {SUPERUSER_ID}
    if admin and admin[0] == 'res.users':
        protected_ids.add(admin[1])
    for xmlid, login in USERS:
        cr.execute('SELECT id FROM res_users WHERE login = %s', (login,))
        record_id = _bind_existing(
            cr, xmlid, 'res.users', 'res_users',
            [row[0] for row in cr.fetchall()], noupdate=False,
        )
        if record_id in protected_ids:
            raise RuntimeError(f'{xmlid} cannot be bound to a protected administrator.')
