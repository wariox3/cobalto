"""
Crea o borra los usuarios de carga en la base de datos de torio (ambiente de pruebas).

    python usuarios.py crear [--cantidad N]
    python usuarios.py borrar

Va por SQL directo y no por la API: el registro de torio exige verificar el correo y
tiene un límite de 5 por hora. Replica lo que hace `SegUsuario.objects.create_user` de
torio (vía tenant_users): la cuenta en `seg_usuario`, su fila de permisos en el
contenedor público y la membresía en `seg_usuario_cliente`.

Todo lo de carga usa el dominio @carga.test —también los correos inexistentes del
escenario de login—, y `borrar` lo encuentra por ahí.
"""

import argparse
import base64
import hashlib
import os
import secrets
import string
import sys

import psycopg
from dotenv import load_dotenv

DOMINIO = 'carga.test'

# El mismo hasher y las mismas iteraciones que Django 5.2 (PBKDF2PasswordHasher). Si no
# coincidieran, torio reescribiría el hash en el primer login de cada usuario y esa
# escritura se colaría en la medición.
ITERACIONES = 1_000_000


def email_carga(numero: int) -> str:
    return f'usuario-{numero:04d}@{DOMINIO}'


def hash_django(clave: str) -> str:
    """`pbkdf2_sha256$<iteraciones>$<salt>$<hash>`, el formato de `make_password`."""
    salt = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(22))
    derivada = hashlib.pbkdf2_hmac('sha256', clave.encode(), salt.encode(), ITERACIONES)
    return f'pbkdf2_sha256${ITERACIONES}${salt}${base64.b64encode(derivada).decode()}'


def confirmar(conexion, forzar: bool) -> None:
    """Muestra a qué base se va a escribir y pide confirmación: nunca debería ser producción."""
    info = conexion.info
    print(f'Base de datos: {info.dbname} en {info.host}:{info.port} (usuario {info.user})')
    if forzar:
        return
    if input('Escriba el nombre de la base para continuar: ').strip() != info.dbname:
        sys.exit('Cancelado.')


def crear(conexion, cantidad: int, clave: str) -> None:
    # Un solo hash para todos: cada uno cuesta ~0,2 s y con cientos de usuarios crearlos
    # tardaría minutos. Que compartan salt no importa en usuarios de prueba.
    hash_clave = hash_django(clave)
    with conexion.transaction(), conexion.cursor() as cursor:
        cursor.execute("SELECT id FROM public.ctn_cliente WHERE schema_name = 'public'")
        fila = cursor.fetchone()
        if fila is None:
            sys.exit('No existe el contenedor público: ¿es la base de torio?')
        cliente_publico = fila[0]

        nuevos = 0
        for numero in range(1, cantidad + 1):
            # Si ya existe se le pone la clave de ahora: una corrida anterior pudo usar otra.
            cursor.execute(
                """
                INSERT INTO public.seg_usuario
                    (email, password, is_active, is_verified, fecha_creacion)
                VALUES (%s, %s, true, true, now())
                ON CONFLICT (email) DO UPDATE
                    SET password = EXCLUDED.password, is_active = true, is_verified = true
                RETURNING id, (xmax = 0)
                """,
                (email_carga(numero), hash_clave),
            )
            usuario_id, es_nuevo = cursor.fetchone()
            nuevos += es_nuevo
            # Lo que hace `CtnCliente.add_user` con el contenedor público: permisos sin
            # staff ni superusuario, y la membresía sin ningún módulo.
            cursor.execute(
                """
                INSERT INTO public.permissions_usertenantpermissions
                    (profile_id, is_staff, is_superuser, created_at, modified_at)
                VALUES (%s, false, false, now(), now())
                ON CONFLICT (profile_id) DO NOTHING
                """,
                (usuario_id,),
            )
            cursor.execute(
                """
                INSERT INTO public.seg_usuario_cliente (usuario_id, cliente_id)
                SELECT %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM public.seg_usuario_cliente WHERE usuario_id = %s AND cliente_id = %s
                )
                """,
                (usuario_id, cliente_publico, usuario_id, cliente_publico),
            )
    print(f'{cantidad} usuarios de carga listos ({nuevos} nuevos): {email_carga(1)} … {email_carga(cantidad)}')


def borrar(conexion) -> None:
    usuarios = f"SELECT id FROM public.seg_usuario WHERE email LIKE '%@{DOMINIO}'"
    permisos = f'SELECT id FROM public.permissions_usertenantpermissions WHERE profile_id IN ({usuarios})'
    tokens = f'SELECT id FROM public.token_blacklist_outstandingtoken WHERE user_id IN ({usuarios})'
    # En orden de dependencias. No se puede borrar con el ORM de torio: hay FKs desde
    # tablas de los tenants (demo.gen_documento, …) hacia seg_usuario, y el borrado en
    # cascada de Django las busca en el schema público y falla. Los usuarios de carga no
    # son miembros de ningún tenant, así que esas tablas no tienen filas suyas.
    sentencias = [
        ('tokens en lista negra', f'DELETE FROM public.token_blacklist_blacklistedtoken WHERE token_id IN ({tokens})'),
        ('tokens emitidos', f'DELETE FROM public.token_blacklist_outstandingtoken WHERE user_id IN ({usuarios})'),
        ('registros de acceso', f"DELETE FROM public.seg_acceso WHERE email LIKE '%@{DOMINIO}'"),
        ('desafíos MFA', f'DELETE FROM public.seg_mfa_desafio WHERE usuario_id IN ({usuarios})'),
        ('membresías', f'DELETE FROM public.seg_usuario_cliente WHERE usuario_id IN ({usuarios})'),
        ('grupos', f'DELETE FROM public.permissions_usertenantpermissions_groups WHERE usertenantpermissions_id IN ({permisos})'),
        ('permisos sueltos', f'DELETE FROM public.permissions_usertenantpermissions_user_permissions WHERE usertenantpermissions_id IN ({permisos})'),
        ('permisos', f'DELETE FROM public.permissions_usertenantpermissions WHERE profile_id IN ({usuarios})'),
        ('usuarios', f"DELETE FROM public.seg_usuario WHERE email LIKE '%@{DOMINIO}'"),
    ]
    with conexion.transaction(), conexion.cursor() as cursor:
        for nombre, sql in sentencias:
            cursor.execute(sql)
            print(f'{cursor.rowcount:>6}  {nombre}')


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    comunes = argparse.ArgumentParser(add_help=False)
    comunes.add_argument('--si', action='store_true', help='No pedir confirmación.')
    acciones = parser.add_subparsers(dest='accion', required=True)
    p_crear = acciones.add_parser('crear', parents=[comunes], help='Deja N usuarios de carga listos (idempotente).')
    p_crear.add_argument('--cantidad', type=int, default=int(os.getenv('CARGA_USUARIOS', '50')))
    acciones.add_parser('borrar', parents=[comunes], help=f'Borra los usuarios de carga y todo lo de @{DOMINIO}.')
    args = parser.parse_args()

    url = os.getenv('DATABASE_URL')
    if not url:
        sys.exit('Falta DATABASE_URL en el .env.')

    with psycopg.connect(url) as conexion:
        confirmar(conexion, args.si)
        if args.accion == 'crear':
            clave = os.getenv('CARGA_CLAVE')
            if not clave:
                sys.exit('Falta CARGA_CLAVE en el .env.')
            if args.cantidad < 1:
                sys.exit('--cantidad tiene que ser mayor que 0.')
            crear(conexion, args.cantidad, clave)
        else:
            borrar(conexion)


if __name__ == '__main__':
    main()
