"""
Varios usuarios inician sesión y crean un contenedor al mismo tiempo.

    locust -f escenarios/contenedor.py --headless -u 5 -r 5 --html reportes/contenedor.html

Sin `--processes`: los usuarios se esperan entre sí en este proceso para mandar el POST
a la vez, y con varios procesos cada uno esperaría solo a los suyos.

Cada usuario virtual hace una sola pasada y termina; la prueba se cierra sola cuando
terminan todos:

1. `POST /seguridad/login/` con su usuario de carga. La sesión queda en la cookie
   `access_token`, como en el front.
2. Espera a que los demás hayan entrado y manda `POST /contenedor/cliente/`. Torio
   responde 202 enseguida y deja el contenedor en `creando`: el schema, las migraciones
   y los catálogos los construye la tarea `crear_contenedor` en el worker de Celery,
   que tiene dos procesos, así que de cinco contenedores van dos a la vez y el resto
   espera en la cola.
3. Consulta `GET /contenedor/cliente/<id>/estado/` hasta que llegue a `listo` o a
   `error`. El tiempo desde el POST hasta ahí sale en el reporte como
   `CONTENEDOR creación hasta listo`.
4. Si `CONTENEDOR_BORRAR` está activo (por defecto), lo elimina con
   `DELETE /contenedor/cliente/<id>/`, que borra también su schema.

Torio admite un solo contenedor en creación por usuario, así que cada usuario virtual
necesita su propio usuario de carga: `CARGA_USUARIOS` tiene que ser al menos `-u`.
"""

import os
import time
import uuid

import gevent
from gevent.event import Event
from locust import FastHttpUser, constant, task
from locust.exception import StopUser

from comun import CANTIDAD_USUARIOS, CLAVE, HOST, TURNSTILE_TOKEN, limpiar_tambien, logger, siguiente_email

# El límite propio de la creación es de 5 por hora por usuario: sin limpiarlo, repetir la
# prueba dentro de la hora da 429.
limpiar_tambien('login', 'crear_contenedor')

RUTA_LOGIN = '/seguridad/login/'
RUTA_CLIENTE = '/contenedor/cliente/'

# Un contenedor tarda unos 20 s solo, pero con la cola de Celery el último de varios
# espera a los anteriores. El límite duro de la tarea en torio es de 11 minutos.
ESPERA_MAXIMA = float(os.getenv('CONTENEDOR_ESPERA_MAXIMA', '900'))
INTERVALO_ESTADO = float(os.getenv('CONTENEDOR_INTERVALO_ESTADO', '2'))
BORRAR = os.getenv('CONTENEDOR_BORRAR', 'si').lower() in ('si', 'sí', '1', 'true')
# Cuánto espera un usuario que ya entró a que entren los demás antes de crear igual.
ESPERA_SALIDA = 60

# Todos los usuarios crean a la vez: el que completa el grupo suelta a los demás.
_salida = Event()
_en_linea = 0
_terminados = 0


def _objetivo(environment) -> int:
    return environment.runner.target_user_count or 1


class CrearContenedor(FastHttpUser):
    host = HOST
    wait_time = constant(0)

    def on_start(self):
        if _objetivo(self.environment) > CANTIDAD_USUARIOS:
            logger.error(
                'Hay %s usuarios virtuales y solo %s de carga: torio admite un contenedor en '
                'creación por usuario. Suba CARGA_USUARIOS y corra usuarios.py crear.',
                _objetivo(self.environment), CANTIDAD_USUARIOS,
            )

    @task
    def crear(self):
        email = siguiente_email()
        try:
            if self._entrar(email):
                self._esperar_a_los_demas()
                cliente_id = self._crear(email)
                if cliente_id is not None:
                    estado = self._esperar_listo(cliente_id)
                    if BORRAR and estado in ('listo', 'error'):
                        self._borrar(cliente_id)
            else:
                # Uno que no entró no puede dejar a los demás esperando su turno.
                self._esperar_a_los_demas()
        finally:
            self._terminar()
        raise StopUser()

    def _entrar(self, email) -> bool:
        with self.client.post(
            RUTA_LOGIN,
            json={'email': email, 'password': CLAVE, 'turnstile_token': TURNSTILE_TOKEN},
            name='login',
            catch_response=True,
        ) as respuesta:
            if respuesta.status_code == 200 and 'mfa_requerido' not in (respuesta.text or ''):
                respuesta.success()
                return True
            if respuesta.status_code == 429:
                respuesta.failure('429: el throttling frenó la prueba (¿REDIS_URL?)')
            else:
                respuesta.failure(f'{email}: llegó {respuesta.status_code}: {(respuesta.text or "")[:200]}')
            return False

    def _esperar_a_los_demas(self):
        global _en_linea
        _en_linea += 1
        if _en_linea >= _objetivo(self.environment):
            _salida.set()
        elif not _salida.wait(timeout=ESPERA_SALIDA):
            logger.warning('No entraron todos en %ss: se crea sin esperar al resto', ESPERA_SALIDA)

    def _crear(self, email):
        schema = f'carga_{uuid.uuid4().hex[:12]}'
        with self.client.post(
            RUTA_CLIENTE,
            json={
                'schema_name': schema,
                'nombre': f'Carga {schema}',
                'celular': '+573001234567',
                'correo': email,
            },
            name='crear contenedor',
            catch_response=True,
        ) as respuesta:
            if respuesta.status_code == 202:
                respuesta.success()
                self._inicio = time.monotonic()
                return respuesta.json()['id']
            if respuesta.status_code == 409:
                respuesta.failure(f'{email} ya tiene un contenedor en creación: ¿una corrida anterior quedó a medias?')
            elif respuesta.status_code == 429:
                respuesta.failure('429: límite de crear_contenedor (¿REDIS_URL?)')
            else:
                respuesta.failure(f'Llegó {respuesta.status_code}: {(respuesta.text or "")[:200]}')
            return None

    def _esperar_listo(self, cliente_id):
        estado = 'creando'
        while time.monotonic() - self._inicio < ESPERA_MAXIMA:
            gevent.sleep(INTERVALO_ESTADO)
            with self.client.get(
                f'{RUTA_CLIENTE}{cliente_id}/estado/', name='estado contenedor', catch_response=True,
            ) as respuesta:
                if respuesta.status_code != 200:
                    respuesta.failure(f'Llegó {respuesta.status_code}: {(respuesta.text or "")[:200]}')
                    continue
                estado = respuesta.json()['estado']
            if estado != 'creando':
                break

        duracion = (time.monotonic() - self._inicio) * 1000
        if estado == 'listo':
            error = None
        elif estado == 'error':
            error = Exception('La tarea terminó en error: ver el log de torio-celery')
        else:
            error = Exception(f'Sigue en creando pasados {ESPERA_MAXIMA:.0f} s')
        self.environment.events.request.fire(
            request_type='CONTENEDOR',
            name='creación hasta listo',
            response_time=duracion,
            response_length=0,
            exception=error,
            context={},
        )
        if estado == 'creando':
            logger.warning('El contenedor %s no terminó: bórrelo cuando termine', cliente_id)
        return estado

    def _borrar(self, cliente_id):
        with self.client.delete(
            f'{RUTA_CLIENTE}{cliente_id}/', name='borrar contenedor', catch_response=True,
        ) as respuesta:
            if respuesta.status_code == 204:
                respuesta.success()
            else:
                respuesta.failure(f'Llegó {respuesta.status_code}: {(respuesta.text or "")[:200]}')

    def _terminar(self):
        global _terminados
        _terminados += 1
        if _terminados >= _objetivo(self.environment):
            # Después de que este usuario suelte su StopUser.
            gevent.spawn_later(1, self.environment.runner.quit)
