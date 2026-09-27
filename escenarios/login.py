"""
Carga sobre POST /seguridad/login/ de torio.

    locust -f escenarios/login.py

Tres tipos de intento, en la proporción de un día normal: casi todos entran, unos pocos
se equivocan de clave o de correo. Los tres cuestan parecido en torio —PBKDF2 corre
también con el correo inexistente, para no delatar por tiempo qué cuentas existen— y los
tres dejan una fila en la bitácora de accesos (SegAcceso).
"""

import uuid

from locust import FastHttpUser, between, task

from comun import CLAVE, DOMINIO, HOST, TURNSTILE_TOKEN, siguiente_email

RUTA = '/seguridad/login/'


class Login(FastHttpUser):
    host = HOST
    # Una persona no reintenta el login en milisegundos. Para buscar el techo del
    # servidor se sube la cantidad de usuarios, no se quita la espera.
    wait_time = between(1, 3)

    def _intentar(self, email, clave, esperado, nombre):
        with self.client.post(
            RUTA,
            json={'email': email, 'password': clave, 'turnstile_token': TURNSTILE_TOKEN},
            name=nombre,
            catch_response=True,
        ) as respuesta:
            if respuesta.status_code == esperado:
                respuesta.success()
            elif respuesta.status_code == 429:
                respuesta.failure('429: el throttling frenó la prueba (¿REDIS_URL y THROTTLE_A_LIMPIAR?)')
            elif respuesta.status_code == 200 and 'mfa_requerido' in (respuesta.text or ''):
                respuesta.failure('El usuario de carga tiene MFA: recréelo con usuarios.py')
            else:
                respuesta.failure(f'Esperaba {esperado}, llegó {respuesta.status_code}: {(respuesta.text or "")[:200]}')
        # Sin sesión entre intentos: cada login es el de alguien que llega de cero.
        self.client.cookiejar.clear()

    @task(8)
    def correcto(self):
        self._intentar(siguiente_email(), CLAVE, 200, 'login correcto')

    @task(1)
    def clave_errada(self):
        self._intentar(siguiente_email(), 'no-es-la-clave', 401, 'login clave errada')

    @task(1)
    def correo_inexistente(self):
        self._intentar(f'noexiste-{uuid.uuid4().hex[:8]}@{DOMINIO}', CLAVE, 401, 'login correo inexistente')
