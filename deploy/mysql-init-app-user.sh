#!/bin/sh
# Sourced by the official MySQL entrypoint during initialization of a NEW volume.
# Alphanumeric passwords keep SQL and database URL interpolation unambiguous.
case "${JUYA_MYSQL_APP_PASSWORD:-}" in
  ''|*[!a-zA-Z0-9]*)
    echo 'JUYA_MYSQL_APP_PASSWORD must be a nonempty alphanumeric secret' >&2
    exit 1
    ;;
esac
if [ "${#JUYA_MYSQL_APP_PASSWORD}" -lt 32 ]; then
  echo 'JUYA_MYSQL_APP_PASSWORD must contain at least 32 characters' >&2
  exit 1
fi
MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --protocol=socket -uroot <<SQL
CREATE USER 'juya_admin_app'@'%' IDENTIFIED BY '$JUYA_MYSQL_APP_PASSWORD';
GRANT SELECT, INSERT, UPDATE, DELETE, EXECUTE ON juya.* TO 'juya_admin_app'@'%';
SQL
