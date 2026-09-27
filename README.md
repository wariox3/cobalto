# cobalto

Pruebas de carga y rendimiento de la API de **torio**, con [Locust](https://locust.io).

Se corre desde un droplet temporal contra el ambiente de pruebas (`jardin`,
`reddocapi.uk`). Nunca contra producción.

```
cobalto/
├── usuarios.py           # crea / borra los usuarios de carga (SQL directo a la base de pruebas)
├── escenarios/
│   ├── comun.py          # configuración del .env y limpieza del throttling
│   └── login.py          # POST /seguridad/login/
├── requirements.txt
└── .env.example
```

## Cómo está armado

```
Droplet (Locust) ──HTTPS──► Nginx de jardin ──► gunicorn de torio (127.0.0.1:9500)
                  directo, sin Cloudflare              │
                                                       ├── PostgreSQL de pruebas
                                                       └── Valkey de pruebas (throttling)
```

- **Sin Cloudflare.** El droplet resuelve `reddocapi.uk` a la IP de jardin por
  `/etc/hosts`: el certificado sigue siendo válido y se mide la API, no a Cloudflare, que
  además suele desafiar el tráfico masivo desde IPs de DigitalOcean.
- **Usuarios de carga.** `usuario-0001@carga.test` … `usuario-NNNN@carga.test`,
  verificados, sin MFA y sin contenedores, todos con la misma clave. Se crean por SQL
  porque el registro de torio exige verificar el correo y admite 5 por hora. Todo lo de
  carga usa el dominio `@carga.test`, también los correos inexistentes del escenario de
  login, y `usuarios.py borrar` lo limpia por ese dominio.
- **Throttling.** Torio limita el login a 5 por minuto por IP y toda la carga sale de la
  IP del droplet. Como torio no se modifica para las pruebas, mientras corre la prueba
  `comun.py` borra de Valkey los contadores de los scopes de `THROTTLE_A_LIMPIAR` cada
  `THROTTLE_INTERVALO` segundos. Cada request sigue pagando su consulta a Valkey; solo se
  evita que el contador llegue al límite. Sin `REDIS_URL` la prueba avisa al arrancar y
  los `429` aparecen como fallas con el motivo.

## 1. Preparar el droplet

1. **Crear** un droplet Ubuntu 24.04 en la **misma región** que jardin. Con 2 vCPU / 2 GB
   alcanza para varios cientos de requests por segundo.
2. **Autorizarlo** en DigitalOcean: agregarlo a los *trusted sources* de la base de
   pruebas (para `usuarios.py`) y del cluster Valkey de pruebas (para la limpieza del
   throttling). Si jardin tiene un firewall que solo acepta Cloudflare, abrir también la
   IP del droplet.
3. **Apuntar el dominio a jardin**, directo:

   ```bash
   echo "<IP pública de jardin>   reddocapi.uk" | sudo tee -a /etc/hosts
   getent hosts reddocapi.uk     # debe mostrar la IP de jardin, no una de Cloudflare
   ```

4. **Instalar:**

   ```bash
   sudo apt update && sudo apt install -y python3-venv git
   git clone https://github.com/wariox3/cobalto.git && cd cobalto
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env && nano .env      # llenar CARGA_CLAVE, DATABASE_URL, REDIS_URL
   ```

   Para `DATABASE_URL` y `REDIS_URL` use las cadenas **públicas** de los clusters de
   pruebas (*Connection details → Public network*): el droplet no está en su VPC.

## 2. Crear los usuarios de carga

```bash
python usuarios.py crear              # CARGA_USUARIOS del .env, o --cantidad N
```

Muestra a qué base se va a conectar y pide escribir su nombre para seguir (`--si` lo
salta). Es idempotente: si los usuarios ya existen, les pone la clave del `.env`.

## 3. Correr

**Con la interfaz web.** Es lo más cómodo para explorar: se ven en vivo los requests por
segundo, las latencias y los errores. Locust escucha en el puerto 8089 del droplet; desde
su máquina abra un túnel en vez de exponer ese puerto:

```bash
# en el droplet
locust -f escenarios/login.py --processes -1
# en su máquina
ssh -L 8089:localhost:8089 root@<IP del droplet>     # y abrir http://localhost:8089
```

**Sin interfaz**, con reporte HTML:

```bash
mkdir -p reportes
locust -f escenarios/login.py --processes -1 --headless \
    -u 20 -r 2 -t 10m --html reportes/login-carga.html
```

`-u` son usuarios simultáneos, `-r` cuántos se suman por segundo y `-t` la duración.
`--processes -1` usa todos los núcleos del droplet.

### Orden sugerido

| Prueba | Comando | Para qué |
|---|---|---|
| Humo | `-u 2 -r 1 -t 1m` | Que todo responda y no haya fallas |
| Carga | `-u <pico esperado> -r 2 -t 15m` | Que aguante lo normal con latencias estables |
| Estrés | interfaz web, subiendo usuarios de a poco | Dónde se rompe y qué se rompe primero |
| Resistencia | `-u <pico> -r 2 -t 2h` | Fugas de memoria o conexiones que se acumulan |

## 4. Qué mirar en jardin mientras corre

- **Log de Nginx** (`/var/log/nginx/access.log`, JSON): `request_time` frente a
  `upstream_time`. Si el primero crece y el segundo no, las peticiones esperan en Nginx
  porque no hay worker libre.
- **Workers de gunicorn** (`htop`): con workers síncronos, cada worker atiende un request a
  la vez. Si todos están al 100% de CPU, ese es el techo.
- **Métricas de DigitalOcean** de la base y de Valkey: CPU, conexiones y memoria.

## 5. Qué esperar del login

Cada login calcula PBKDF2 con 1.000.000 de iteraciones: ~0,2 s de CPU de un worker, a
propósito, para frenar la fuerza bruta. Los tres escenarios cuestan parecido, porque torio
calcula el hash también con correos inexistentes para no delatar por tiempo qué cuentas
existen. Con 2 workers el techo ronda los **5 a 8 logins por segundo**; pasado eso la
latencia sube porque los requests esperan turno.

> ⚠️ Si el servicio de torio en jardin corre con `torioapp.settings.test`, ese archivo
> fija `MD5PasswordHasher`: cada login cuesta microsegundos y la prueba **no** refleja
> producción. Verifíquelo con
> `sudo systemctl cat <servicio de torio> | grep DJANGO_SETTINGS_MODULE`.
> `usuarios.py` guarda las claves en PBKDF2, pero con MD5 como hasher preferido torio las
> reescribe a MD5 en el primer login de cada usuario.

## 6. Limpiar

```bash
python usuarios.py borrar
```

Borra los usuarios de carga, sus tokens, sus membresías y todas las filas de la bitácora
de accesos de `@carga.test`. Después:

- quite el droplet de los *trusted sources* de la base y de Valkey;
- destruya el droplet.

## Agregar un escenario

Un archivo nuevo en `escenarios/` que importe de `comun` lo que necesite (`HOST`, `CLAVE`,
`siguiente_email`…). Importar `comun` ya activa la limpieza del throttling; agregue los
scopes que el escenario use a `THROTTLE_A_LIMPIAR` (por ejemplo `login,refresh,user`).
Los endpoints de un contenedor necesitan el header `X-Tenant: <schema>`, y los usuarios
de carga tendrían que ser miembros de ese contenedor, algo que `usuarios.py` todavía no
hace.
