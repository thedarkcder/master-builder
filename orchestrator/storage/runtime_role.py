"""Reject migration/owner credentials at the runtime connection boundary."""

_ROLE_QUERY = """
SELECT r.rolsuper, r.rolbypassrls,
       pg_has_role(current_user, d.datdba, 'MEMBER'),
       EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'public' AND c.relkind IN ('r','p')
                 AND c.relname <> 'alembic_version'
                 AND pg_has_role(current_user, c.relowner, 'MEMBER')),
       EXISTS (SELECT 1 FROM pg_roles p WHERE p.oid <> r.oid
               AND (p.rolsuper OR p.rolbypassrls)
               AND pg_has_role(current_user, p.oid, 'MEMBER'))
FROM pg_roles r JOIN pg_database d ON d.datname = current_database()
WHERE r.rolname = current_user
"""


def validate_runtime_role_flags(
    *,
    superuser: bool,
    bypass_rls: bool,
    owns_database: bool,
    owns_protected_tables: bool,
    privileged_membership: bool,
) -> None:
    if any(
        (
            superuser,
            bypass_rls,
            owns_database,
            owns_protected_tables,
            privileged_membership,
        )
    ):
        raise RuntimeError(
            "Database runtime requires a non-owning runtime role with NOSUPERUSER, "
            "NOBYPASSRLS and no membership in owner/privileged roles. "
            "Use a separate migration credential and restart runtime consumers."
        )


def validate_runtime_connection(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
    try:
        with dbapi_connection.cursor() as cursor:
            cursor.execute(_ROLE_QUERY)
            row = cursor.fetchone()
    finally:
        dbapi_connection.rollback()
    if row is None:
        raise RuntimeError("Unable to validate PostgreSQL runtime role")
    validate_runtime_role_flags(
        superuser=row[0],
        bypass_rls=row[1],
        owns_database=row[2],
        owns_protected_tables=row[3],
        privileged_membership=row[4],
    )
