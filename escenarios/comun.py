"""
Lo que comparten los escenarios: la configuración del .env y la limpieza del throttling.
"""

import itertools
import logging
import os

import gevent
from dotenv import load_dotenv
from locust import events
from locust.runners import WorkerRunner

load_dotenv()

HOST = os.getenv('CARGA_HOST', 'https://reddocapi.uk')
CLAVE = os.getenv('CARGA_CLAVE', '')
CANTIDAD_USUARIOS = int(os.getenv('CARGA_USUARIOS', '50'))
TURNSTILE_TOKEN = os.getenv('TURNSTILE_TOKEN', 'XXXX.DUMMY.TOKEN.XXXX')
DOMINIO = 'carga.test'  # el mismo de usuarios.py

logger = logging.getLogger('cobalto')


def email_carga(numero: int) -> str:
    return f'usuario-{numero:04d}@{DOMINIO}'


# Reparte los usuarios de carga entre los usuarios virtuales de Locust, en rueda. Con
# --processes cada proceso tiene su propia rueda: varios pueden usar el mismo usuario a la
# vez, lo que en el login no cambia nada.
_rueda = itertools.cycle(range(1, CANTIDAD_USUARIOS + 1))


def siguiente_email() -> str:
    return email_carga(next(_rueda))


# ── Limpieza del throttling ─────────────────────────────
#
# Torio limita el login a 5 por minuto por IP y toda la carga sale de la IP del generador.
# Como torio no se toca para las pruebas, mientras corren se borran de Valkey los
# contadores de los scopes de THROTTLE_A_LIMPIAR. Cada request sigue pagando su consulta a
# Valkey (la medición la incluye); solo se evita que el contador llegue al límite.
#
# Las claves las arma torio así: `<schema>::1:throttle_<scope>_<ip o id de usuario>`
# (KEY_FUNCTION de django-tenants + ScopedRateThrottle de DRF). Si eso cambia en torio,
# esto deja de borrar y la prueba empieza a dar 429.

REDIS_URL = os.getenv('REDIS_URL', '')
SCOPES = [s.strip() for s in os.getenv('THROTTLE_A_LIMPIAR', 'login').split(',') if s.strip()]
INTERVALO = float(os.getenv('THROTTLE_INTERVALO', '0.2'))

_limpiador = None


def _limpiar_throttling(cliente) -> None:
    patrones = [f'*::1:throttle_{scope}_*' for scope in SCOPES]
    while True:
        try:
            for patron in patrones:
                claves = list(cliente.scan_iter(match=patron, count=1000))
                if claves:
                    cliente.delete(*claves)
        except Exception as error:  # noqa: BLE001 — un corte de Valkey no debe tumbar la prueba
            logger.warning('No se pudo limpiar el throttling: %s', error)
        gevent.sleep(INTERVALO)


@events.test_start.add_listener
def _iniciar_limpieza(environment, **_kwargs):
    global _limpiador
    # Con --processes solo el proceso principal limpia: una vez basta.
    if isinstance(environment.runner, WorkerRunner) or not SCOPES:
        return
    if not REDIS_URL:
        logger.warning('Sin REDIS_URL: no se limpia el throttling y torio va a responder 429.')
        return
    import redis

    cliente = redis.Redis.from_url(REDIS_URL, socket_timeout=2, socket_connect_timeout=2)
    cliente.ping()  # que falle al empezar, no a mitad de la prueba
    logger.info('Limpiando throttling de %s cada %ss', ', '.join(SCOPES), INTERVALO)
    _limpiador = gevent.spawn(_limpiar_throttling, cliente)


@events.test_stop.add_listener
def _detener_limpieza(environment, **_kwargs):
    global _limpiador
    if _limpiador is not None:
        _limpiador.kill()
        _limpiador = None
