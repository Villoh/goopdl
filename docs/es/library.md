# Librería de Python

[English](../library.md) · **Español**

goopdl también es un paquete de Python que puedes importar desde un script,
un servicio web o un worker. Esta guía cubre la API pública: la fachada
[`Client`](#client) (`goopdl.client`, también importable desde `goopdl`), las
funciones de `goopdl.api`, `goopdl.auth` y `goopdl.download`, y
`goopdl.errors`. Todo lo demás (`goopdl.cli`, los nombres con guion bajo) es
interno y puede cambiar sin aviso.

## Requisitos

- Python 3.10 o posterior.
- `pip install goopdl` instala cinco dependencias, todas obligatorias hoy:
  `httpx>=0.27`, `typer>=0.16`, `rich>=13`, `websocket-client>=1.8` y
  `keyring>=25`. Solo la CLI usa `keyring`, para guardar el token de
  `goopdl auth login` en el almacén de claves del sistema. El
  mínimo de `typer` ha subido desde 0.9: si fijas tú la versión de `typer`,
  que sea 0.16 o posterior. Para fijar versiones, mira
  [instalación](installation.md#como-librería).

Importar `goopdl`, `goopdl.client`, `goopdl.api`, `goopdl.auth`,
`goopdl.download` o `goopdl.errors` no importa `rich`, `typer`,
`websocket-client` ni `keyring`, no imprime nada y no toca ningún archivo.
Solo los cargan estos:

| Módulo o llamada | Carga |
| ---------------- | ----- |
| `goopdl.cli` | `typer`, `rich` |
| `download.download_batch()` y `api.download_app()` (barras de progreso) | `rich` |
| `goopdl.browser_oauth` | `websocket-client` |
| Comandos de la CLI que leen o guardan una cuenta de Google en el almacén de claves | `keyring` |

## Client

`Client` es el punto de entrada para backends. Toma su configuración de un
objeto `Config` en vez de variables de entorno, así que un mismo proceso puede
atender varias cuentas y directorios de caché a la vez, desde hilos o desde
asyncio. Envuelve las mismas funciones que usa la CLI, que siguen disponibles
para controlar cada paso (ver [inicio rápido](#inicio-rápido) en adelante).

### Para empezar

```python
from pathlib import Path

from goopdl import Client, Config

client = Client(Config(config_dir=Path("goopdl-cache").resolve(), country="US"))

details = client.details("com.whatsapp")
print(details.title, details.version_string, details.version_code)

result = client.download("com.whatsapp", Path("apks"), splits=True)
print(result.path, result.version_string, len(result.files))
```

`Client(config=None, **overrides)`

`Client(config, **overrides)` equivale a `Client(config.replace(**overrides))`,
y `Client(country="US")` te construye el `Config`. `download` devuelve un
`DownloadResult`: `path` es el APK base, `files` todos los archivos escritos
(el APK base primero) y `version_string` el nombre de versión. A diferencia de
`api.download_app`, no pinta barras de progreso.

### Config

`Config(config_dir=None, credentials=None, arch="arm64", country=None, proxy=None, profile=None, dispenser_url=None)`

`Config.replace(**changes) -> Config`

Una dataclass inmutable (`frozen`). `replace` devuelve una copia validada.

| Campo | Significado |
| ----- | ----------- |
| `config_dir` | Directorio de la caché de tokens. Tiene que ser absoluto tras expandir `~` (si no, `ValueError`). Con `None` se resuelve en cada llamada como en la CLI, ver [pool de tokens y caché](#pool-de-tokens-y-caché) |
| `credentials` | Unas `Credentials` para el modo cuenta, o `None` para el dispensador anónimo |
| `arch` | Una de `profiles.VALID_ARCHS`, comprobada al momento (`InvalidArchError`) |
| `country`, `proxy`, `profile`, `dispenser_url` | Como en [conceptos](#conceptos) |

`repr(config)` oculta el proxy (puede llevar una contraseña) y enmascara las
credenciales.

### Credentials

`Credentials(email, aas_token)`

Una cuenta de Google para el [modo cuenta](authentication.md#cuenta-de-google).
Se comprueba igual que las variables de entorno: los dos valores no vacíos (se
les quitan los espacios) y el token empezando por `aas_et/`. Si no, lanza
`DirectAuthConfigurationError`, con un mensaje que nombra la variable de
entorno equivalente. `repr` y `str` no muestran el token ni el email completo.

```python
from goopdl import Credentials

account = Client(
    config_dir=Path("goopdl-cache/me").resolve(),
    credentials=Credentials("me@example.com", "aas_et/AKppINa..."),  # cárgalo de tu almacén de secretos
)
print(account.config.credentials)  # Credentials(email=m***@example.com, aas_token=<hidden>)
print(account.details("com.whatsapp").version_code)
```

**Un Client nunca lee `GOOPDL_ACCOUNT_EMAIL` ni `GOOPDL_AAS_TOKEN`.**
`credentials=None` es modo anónimo aunque esas variables estén definidas, para
que un servidor no cambie de cuenta por culpa de su entorno. Si quieres el
comportamiento de la CLI, pídelo:

`Credentials.from_env() -> Credentials | None`

```python
cli_like = Client(credentials=Credentials.from_env())  # None (anónimo) si no hay ninguna
```

Lanza `DirectAuthConfigurationError` si solo está definida una de las dos.
`GOOPDL_CONFIG_DIR` y `XDG_CONFIG_HOME` siguen contando cuando `config_dir` es
`None`.

La librería nunca lee la cuenta guardada por `goopdl auth login`
(`account.json` y el almacén de claves del sistema), ni en un `Client` ni en
las funciones del módulo. Solo la lee la CLI.

### Métodos

| Método | Devuelve | Hace |
| ------ | -------- | ---- |
| `details` | `AppDetails` | Token del pool + [`get_details`](#get_details) |
| `search` | `list[dict]` | Token del pool + [`search_apps`](#search_apps) |
| `list_splits` | `list[str]` | Token del pool + [`list_splits`](#list_splits) |
| `item` | `dict` | [`fetch_app_item`](#fetch_app_item) |
| `delivery` | `DeliveryResult` | Token del pool, y luego [`purchase`](#purchase) y [`get_delivery`](#get_delivery) con ese mismo token |
| `download` | `DownloadResult` | Como [`download_app`](#download_app), sin barras de progreso |
| `adownload` | `DownloadResult` | `download` para asyncio |
| `clear_cache` | `None` | Borra lo que este cliente guardó en caché, ver [borrar la caché](#borrar-la-caché) |

- `Client.details(package, *, country=None, proxy=None, arch=None, profile=None, dispenser_url=None) -> AppDetails`
- `Client.search(query, *, limit=10, country=None, proxy=None, arch=None, profile=None, dispenser_url=None) -> list[dict]`
- `Client.list_splits(package, *, country=None, proxy=None, arch=None, profile=None, dispenser_url=None) -> list[str]`
- `Client.item(package, *, country=None, proxy=None, arch=None, profile=None, dispenser_url=None) -> dict`
- `Client.delivery(package, version_code=None, *, country=None, proxy=None, arch=None, profile=None, dispenser_url=None) -> DeliveryResult`
- `Client.download(package, output, *, splits=False, extras=False, on_progress=None, max_concurrent=4, country=None, proxy=None, arch=None, profile=None, dispenser_url=None) -> DownloadResult`
- `Client.adownload(package, output, *, splits=False, extras=False, on_progress=None, max_concurrent=4, http_client=None, country=None, proxy=None, arch=None, profile=None, dispenser_url=None) -> DownloadResult`
- `Client.clear_cache() -> None`
- `DownloadResult(path, version_string, files)`

Detalles:

- Los argumentos con nombre sustituyen el valor del `Config` en esa llamada
  cuando no son `None`. Para quitar un valor (por ejemplo `country`), usa un
  cliente creado con `config.replace(country=None)`.
- `details`, `search`, `list_splits` y `delivery` cogen un token del pool (en
  modo cuenta, inician sesión) y reintentan una vez con un token nuevo ante un
  HTTP 401, como la CLI.
- `delivery` sin `version_code` pide antes a `details` la versión actual. Con
  él, se ahorra esa petición.
- `download` y `adownload` solo descargan la última versión, como
  `download_app` (no hay argumento `version_code`). `splits=True` añade los
  split APKs y `extras=True` los OBB y asset packs. `on_progress` y
  `max_concurrent` funcionan como en [`download_files`](#download_files).
- `download` lanza `RuntimeError` dentro de un event loop en marcha: ahí haz
  `await` de `adownload`.
- Los fallos lanzan las mismas subclases de `GoopdlError` que las funciones;
  ningún método devuelve `None` por un fallo. Los tokens válidos del pool
  siguen sirviendo las llamadas mientras el dispensador responde HTTP 429. Los
  errores de token se traducen como en `download_app`: un 429 del dispensador
  sin ningún token válido en el pool es un `RateLimitedError` con
  su `retry_after` (el `DispenserRateLimitedError` queda como `__cause__`), y
  una caída del dispensador o un inicio de sesión fallido, un `PlayAPIError`.
  Un pool ocupado lanza `PoolBusyError`, salvo en `item`, que se comporta como
  `fetch_app_item`.

### Borrar la caché

`clear_cache()` borra lo que el cliente guardó en su `config_dir`: los
archivos de tokens anónimos y, en modo cuenta, el
[registro de dispositivo](authentication.md#qué-guarda-el-modo-cuenta) de esta
cuenta y su sesión en memoria. Las demás cuentas que compartan el directorio
conservan los suyos. Sin credenciales equivale a `auth.clear_auth()` en ese
directorio, que borra también los registros de dispositivo de todas las
cuentas, como `goopdl auth clear-cache`. Los archivos de bloqueo se quedan, y
también la cuenta guardada por `goopdl auth login`, archivo y entrada del
almacén: solo la borra `goopdl auth logout`.

```python
account.clear_cache()  # la siguiente llamada registra un dispositivo nuevo y vuelve a iniciar sesión
```

### Async

`adownload` ejecuta la parte bloqueante (token, detalles, compra, entrega) en
un hilo con `asyncio.to_thread` y luego descarga de forma nativa con
`download_files`. `http_client` se usa para las descargas del CDN y nunca se
cierra. Los demás métodos son bloqueantes: llévalos tú a un hilo.

```python
import asyncio


async def latest(package):
    client = Client(config_dir=Path("goopdl-cache").resolve())
    details = await asyncio.to_thread(client.details, package)
    result = await client.adownload(
        package, Path("apks"),
        on_progress=lambda label, done, total: None,  # se ejecuta en el event loop
    )
    return details.version_code, result.path


print(asyncio.run(latest("com.whatsapp")))
```

Si cancelas `adownload` durante la descarga, borra sus archivos `.part`.
Durante la parte del hilo, el hilo termina antes el paso en curso, como con
cualquier llamada a `asyncio.to_thread`.

### Varios tenants

Un `Client` por tenant, cada uno con su directorio de caché y, si hace falta,
su propia cuenta. Pueden ejecutarse a la vez:

```python
tenants = {
    "acme": Client(config_dir=Path("cache/acme").resolve(), country="US"),
    "globex": Client(config_dir=Path("cache/globex").resolve(), country="DE"),
}


async def download_for_all(package):
    return await asyncio.gather(
        *(client.adownload(package, Path("apks") / name) for name, client in tenants.items())
    )


for result in asyncio.run(download_for_all("com.whatsapp")):
    print(result.path)
```

### Hilos y contexto

Un `Client` no guarda estado mutable: compártelo entre hilos y tareas. Cada
llamada aplica su `Config` mediante una variable de `contextvars` que fija
dentro del hilo que hace el trabajo, así que las llamadas simultáneas nunca
ven el directorio de caché ni la cuenta de otra. Los bloqueos de archivo del
[pool de tokens](#pool-de-tokens-y-caché) siguen serializando a quienes
comparten un directorio de caché.

Las funciones de `goopdl.api` y `goopdl.auth` no cambian: si las llamas
directamente, leen el directorio de caché y la cuenta del entorno en cada
llamada, como antes.

## Inicio rápido

### Una sola llamada

`download_app` consigue un token, busca la última versión y descarga el APK
base en un directorio:

```python
from pathlib import Path

from goopdl import api

apk, version = api.download_app("com.whatsapp", Path("apks"))
print(apk, version)  # apks/com.whatsapp-<código de versión>.apk y el nombre de versión
```

Es síncrona y pinta barras de progreso de Rich en stdout. En un servicio, usa
el [`Client`](#client), o el flujo paso a paso de abajo, y mira
[backends async](#backends-async).

### Paso a paso

Es el mismo flujo que ejecuta la CLI, con una llamada por paso (ver
[cómo funciona](how-it-works.md#el-flujo-de-descarga)):

```python
from pathlib import Path

from goopdl import api, auth
from goopdl.download import DownloadSpec, download_batch

package = "com.whatsapp"
token = auth.pick_pool_token(arch="arm64", raise_on_error=True)
if token is None:
    raise SystemExit("no token: unknown profile or the cache directory is unusable")

details = api.get_details(package, token)
delivery_token = api.purchase(package, details.version_code, token)
delivery = api.get_delivery(
    package, details.version_code, token, delivery_token=delivery_token
)


def base_apk_spec(package, version_code, delivery, output):
    """Turn a DeliveryResult into a verified download of the base APK."""
    return DownloadSpec(
        url=delivery.gzipped_url or delivery.download_url,
        dest=output / f"{package}-{version_code}.apk",
        cookies=delivery.cookies,
        gzipped=bool(delivery.gzipped_url),
        integrity_required=True,
        expected_size=delivery.download_size,
        sha1=delivery.sha1,
        sha256=delivery.sha256,
    )


output = Path("apks")
output.mkdir(exist_ok=True)
spec = base_apk_spec(package, details.version_code, delivery, output)
download_batch([spec], require_integrity=True)
print(spec.dest)
```

El resto de la guía reutiliza `package`, `token`, `details`, `delivery`,
`output` y `base_apk_spec` de este fragmento.

## Conceptos

### Diccionarios auth

Todas las llamadas a Google Play reciben un diccionario `auth`: un bearer
token (`authToken`) más la identidad del dispositivo para el que se emitió
(`gsfId`, tokens del dispositivo, user agent). Trátalo como un secreto opaco:
devuélvelo tal cual, no lo registres en logs y no lo modifiques. goopdl le
añade las claves `_device_profile` y `_cached_at`.

Los tokens salen de uno de dos sitios, elegido automáticamente en cada
llamada:

| Modo | Origen | Caché en disco |
| ---- | ------ | -------------- |
| Anónimo (por defecto) | El dispensador de Aurora, o `dispenser_url` | Sí, ver [pool de tokens](#pool-de-tokens-y-caché) |
| Cuenta de Google | `GOOPDL_ACCOUNT_EMAIL` y `GOOPDL_AAS_TOKEN` en el entorno, o las [`Credentials`](#credentials) de un `Client` | Solo el registro del dispositivo, sin token. El token se queda en memoria |

El modo cuenta se explica en [autenticación](authentication.md#cuenta-de-google).
Las funciones del módulo leen las variables de `os.environ` en cada llamada,
así que para ellas afectan a todo el proceso; un `Client` solo usa sus propias
`credentials`. Si solo defines una, o el token no empieza por `aas_et/`, todas
las funciones de tokens lanzan `DirectAuthConfigurationError`. En modo cuenta
`pick_pool_token`, `replace_pool_token` y `ensure_auth` comparten un inicio de
sesión por directorio de caché, cuenta, arquitectura, país y perfil, guardado
en la memoria del proceso hasta que caduca o Google lo rechaza con HTTP 401.
Las llamadas simultáneas esperan a un único inicio de sesión. `fetch_token` y
`ensure_pool` siguen iniciando sesión con un dispositivo nuevo en cada llamada.

Para conseguir un token AAS desde código hay dos funciones. Las dos hablan con
Google y necesitan a una persona delante, así que van en un script de
configuración, no en un handler de peticiones:

```python
# No lo ejecuta la comprobación de la documentación: necesita una cuenta real y un navegador.
from goopdl.aastoken import fetch_aas_token
from goopdl.browser_oauth import capture_oauth_credentials

email, oauth_token = capture_oauth_credentials(timeout=300)  # abre Chrome/Edge/Chromium/Brave
aas_token = fetch_aas_token(email, oauth_token)              # "aas_et/..."
```

`fetch_aas_token` solo acepta el token `oauth2_4/` de un solo uso: cualquier
otro valor, incluida una contraseña, lanza `ValueError` antes de hacer ninguna
petición. Lanza `AASTokenError` cuando Google rechaza el token, y errores de
`httpx` saneados ante fallos HTTP o de transporte.
`capture_oauth_credentials` lanza `BrowserOAuthError`.

### Pool de tokens y caché

`pick_pool_token` mantiene un pool de tokens anónimos por arquitectura, país y
perfil fijado, y los reparte por turnos (round-robin). Solo pide los tokens que
faltan, y con el pool lleno no hace ninguna llamada de red. Si el dispensador
no puede completarlo (HTTP 429 o una caída), los tokens válidos que quedan
siguen sirviendo la llamada. Los tokens se descartan a los 50 minutos. El pool
se explica en
[cómo funciona](how-it-works.md#pool-de-tokens).

Tras un HTTP 429 del dispensador, el proceso deja de pedirle tokens (al mismo
`dispenser_url` y `proxy`) hasta que pasa el `Retry-After` del servidor, con
un máximo de 15 minutos, o durante 60 segundos si no llega una cabecera
válida. Los 60 segundos son una estimación: el dispensador no dice cuánto dura
su límite. Dentro de esa ventana las funciones de tokens no envían nada y
actúan como si el dispensador hubiera vuelto a responder 429: los tokens
válidos del pool siguen sirviendo las llamadas sin completarlo y, si no queda
ninguno, recibes `DispenserRateLimitedError` (`RateLimitedError` desde
`Client`, `fetch_app_item` y `download_app`) con `retry_after` igual a los
segundos que faltan, redondeados hacia arriba. La ventana solo vive en la
memoria de este proceso: no se guarda y otros procesos no la ven.
`auth.clear_auth()` y `clear_cache()` de un `Client` sin credenciales la
olvidan. El modo cuenta no usa el dispensador, así que no tiene ventana.

`auth.get_config_dir()` resuelve el directorio de caché en cada llamada:

1. Dentro de una llamada de un `Client`, su `config_dir` si lo tiene
2. `$GOOPDL_CONFIG_DIR` (expande `~`)
3. `$XDG_CONFIG_HOME/goopdl`
4. `~/.config/goopdl`

Si alguna de las dos variables tiene una ruta relativa, se ignora
(`GOOPDL_CONFIG_DIR` registra un aviso), para que la caché nunca acabe en el
directorio de trabajo.

```python
from goopdl import auth

print(auth.get_config_dir())
```

| Archivo | Lo escribe |
| ------- | ---------- |
| `token-pool-{arch}[-{country}][-profile-{key}].json` | `pick_pool_token`, `ensure_pool`, `replace_pool_token` |
| `pool-index-{arch}[-{country}][-profile-{key}].json` | `pick_pool_token` (posición del round-robin) |
| `token-pool-…lock` | Archivos de bloqueo, nunca se borran |
| `auth-{arch}[-{country}][-profile-{key}].json` | `ensure_auth` (un único token en caché) |
| `direct-device-{account}-{arch}[-{country}]-profile-{key}.json` | Modo cuenta: el dispositivo registrado, identificado por un hash del email |

En POSIX el directorio se crea con permisos `0700` (si ya existe y es tuyo, se
restringe a `0700`) y los archivos se escriben de forma atómica con `0600`.
`auth.clear_auth()` borra los JSON y conserva los archivos de bloqueo. En modo
cuenta el único archivo es el registro del dispositivo (GSF ID, token de
consistencia del check-in y token de configuración del dispositivo). El email,
el token AAS y el bearer token nunca se escriben; si no se puede escribir en el
directorio, el registro se queda en memoria. Ver
[qué guarda el modo cuenta](authentication.md#qué-guarda-el-modo-cuenta).

### Perfiles de dispositivo y arquitecturas

El perfil de dispositivo decide qué build sirve Google. `profile` acepta la
clave de un perfil (`"Pv"`) o un fragmento de su nombre, sin distinguir
mayúsculas (`"Pixel 9a"`). Sin perfil, goopdl los va rotando por orden de
prioridad.

```python
from goopdl import profiles

print(profiles.VALID_ARCHS)  # ('arm64', 'armv7', 'x86', 'x86_64', 'tv')
for key, props in profiles.get_priority_profiles("arm64")[:3]:
    print(key, props["UserReadableName"])
```

- `arch` tiene que ser uno de `profiles.VALID_ARCHS`. Las funciones que tocan
  la caché lanzan `InvalidArchError` (un `ValueError`) con cualquier otro valor.
- Con un perfil desconocido, `fetch_token` devuelve `None`, incluso con
  `raise_on_error=True`, y registra `Profile not found`.
- Cada perfil tiene su propio archivo de pool, así que fijar un perfil nunca
  reutiliza los tokens de otro.

### País y proxy

`country` es un código de 2 letras, como `"US"` o `"IN"`. Define el parámetro
`gl` de Google y las cabeceras de idioma, ajusta el MCC/MNC del dispositivo en
los países conocidos y forma parte del nombre de los archivos de caché. Por sí
solo solo cambia cabeceras. Para obtener la variante real de una región, envía
además el tráfico desde esa región con `proxy`.

`proxy` es una URL (`http://user:pass@host:8080`). Se usa para el dispensador y
para las llamadas a la API de Google Play. En el dispensador va rotando de
región: pon `{region}` en la URL, o goopdl sustituye el código de país que
encuentre en la contraseña del proxy. Los proxies SOCKS necesitan
`pip install "httpx[socks]"`.

`download_files` no acepta proxy. Para descargar de la CDN a través de uno,
pásale tu propio cliente: `client=httpx.AsyncClient(proxy=..., timeout=...)`.

## Referencia de la API

Todas las funciones de `goopdl.api` y `goopdl.auth` son síncronas y
bloqueantes, igual que los métodos de `Client` salvo `adownload`. Usan `httpx`
síncrono con un timeout de 30 segundos, reutilizan conexiones keep-alive y
pueden hacer `time.sleep` (espera ante límites de peticiones, pausas del
dispensador, espera de bloqueos). `goopdl.download.download_files` y
`Client.adownload` son las únicas corrutinas.

### `goopdl.api`

| Función | Devuelve |
| ------- | -------- |
| [`get_details`](#get_details) | `AppDetails` |
| [`get_details_raw`](#get_details_raw) | La respuesta de detalles en bruto, como dict anidado |
| [`fetch_app_item`](#fetch_app_item) | Dict plano de metadatos; gestiona el token por ti |
| [`search_apps`](#search_apps) | `list[dict]` con los resultados |
| [`list_splits`](#list_splits) | `list[str]` con los nombres de los splits |
| [`purchase`](#purchase) | Token de entrega (`str`, puede estar vacío) |
| [`get_delivery`](#get_delivery) | `DeliveryResult` con las URLs de descarga |
| [`download_app`](#download_app) | `(apk_path, version_string)` |
| [`validate_package`](#validate_package) | El nombre del paquete, o lanza un error |

#### `get_details`

`get_details(package, auth, country=None, proxy=None) -> AppDetails`

```python
details = api.get_details("com.whatsapp", token, country="US")
print(details.title, details.developer, details.version_string, details.version_code)
```

Campos de `AppDetails`: `package`, `title`, `developer`, `version_string`,
`version_code`, `rating`, `downloads`, `play_url`. Lanza
`AppNotAvailableError` ante un HTTP 404 o cuando Play no devuelve documento
para este perfil de dispositivo.

#### `get_details_raw`

`get_details_raw(package, auth, country=None, proxy=None) -> dict`

La respuesta completa, decodificada de forma genérica. Las claves son los
números de campo del protobuf, como strings:

```python
raw = api.get_details_raw("com.whatsapp", token)
doc = raw["1"]["2"]["4"]  # DocV2; no existe si Play no envió documento
print(doc["5"])           # título
```

#### `fetch_app_item`

`fetch_app_item(package, arch="arm64", country=None, proxy=None, profile=None, dispenser_url=None) -> dict`

Coge un token del pool, pide los detalles en bruto, sustituye el token una vez
ante un HTTP 401 y convierte el resultado en un dict plano (`title`,
`version`, `rating`, `screenshots`, `permissions`...). Devuelve `{}` cuando
Play no envía documento.

```python
item = api.fetch_app_item("com.whatsapp", country="US")
print(item.get("title"), item.get("version"))
```

No deja pasar los errores del dispensador. Si el dispensador responde con un
HTTP 429 al sustituir un token caducado y no queda ningún otro token válido en
el pool, recibes un `RateLimitedError` con el
mismo `retry_after`, y su `__cause__` es el `DispenserRateLimitedError`.
Cualquier otro fallo al conseguir un token, incluido un pool ocupado, es un
`PlayAPIError` normal.

```python
from goopdl.errors import RateLimitedError

try:
    item = api.fetch_app_item("com.whatsapp")
except RateLimitedError as exc:
    # El 429 de Play, o el del dispensador sin tokens válidos en el pool.
    print("rate limited, retry after", exc.retry_after)
```

#### `search_apps`

`search_apps(query, auth, limit=10, country=None, proxy=None) -> list[dict]`

```python
for hit in api.search_apps("messenger", token, limit=5):
    print(hit["package"], hit["title"], hit["creator"])
```

#### `list_splits`

`list_splits(package, auth, country=None, proxy=None) -> list[str]`

Los nombres de los splits según los metadatos de detalles, ordenados. Un HTTP
404 lanza un `PlayAPIError` normal con `status_code=404`.

```python
print(api.list_splits("com.whatsapp", token))  # p. ej. ['config.arm64_v8a', ...]
```

#### `purchase`

`purchase(package, version_code, auth, country=None, proxy=None) -> str`

"Compra" una app gratuita y devuelve el token de entrega de Play, o `""`. El
resto de respuestas distintas de 200 no son errores: puede que la app ya sea
tuya, y `get_delivery` informa del problema real. Los HTTP 401 y 429 sí lanzan
error.

```python
delivery_token = api.purchase("com.whatsapp", details.version_code, token)
```

#### `get_delivery`

`get_delivery(package, version_code, auth, country=None, proxy=None, delivery_token="", locales=None) -> DeliveryResult`

```python
delivery = api.get_delivery(
    "com.whatsapp", details.version_code, token,
    delivery_token=delivery_token, locales=["es", "fr"],
)
print(delivery.download_size, [s.name for s in delivery.splits])
```

- `locales` pide splits de idioma adicionales.
- Ante un HTTP 429 reintenta dos veces, esperando 1 s y luego 3 s, o más si
  `Retry-After` lo pide. Después lanza `RateLimitedError`. Si `Retry-After`
  supera los 30 segundos, lanza el error directamente.
- `DeliveryResult` contiene `download_url`, `gzipped_url`, `download_size`,
  `sha1`, `sha256`, `cookies`, `splits` (`SplitInfo`), `additional_files`
  (`AdditionalFile`: OBB y asset packs) y `dex_metadata`.
- Todas las URLs son HTTPS en un dominio de Google, o están vacías. Ver
  [seguridad](#seguridad).
- Las URLs están firmadas y caducan. Descárgalas pronto, y pide una entrega
  nueva tras un 403.

Para descargar también los splits, crea un spec por cada `SplitInfo`. Los
nombres de los splits vienen del servidor, así que limítalos a caracteres
seguros para un nombre de archivo:

```python
import re


def split_specs(package, version_code, delivery, output):
    specs = []
    for split in delivery.splits:
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{package}-{version_code}-{split.name}.apk")
        specs.append(DownloadSpec(
            url=split.gzipped_url or split.url,
            dest=output / name,
            gzipped=bool(split.gzipped_url),
            integrity_required=True,
            expected_size=split.size,
            sha1=split.sha1,
            sha256=split.sha256,
        ))
    return specs


specs = [base_apk_spec(package, details.version_code, delivery, output)]
specs += split_specs(package, details.version_code, delivery, output)
```

Los archivos OBB y los asset packs (`delivery.additional_files`) llegan sin
hashes: descárgalos con `integrity_required=False`.

#### `download_app`

`download_app(package, output, arch="arm64", country=None, proxy=None, dispenser_url=None, profile=None, no_splits=True, no_extras=True) -> tuple[Path, str]`

```python
apk, version = api.download_app("com.whatsapp", Path("apks"), no_splits=False)
```

- Descarga la última versión que ve un único token, igual que
  [`fast-download`](commands.md#fast-download). No tiene parámetro de versión:
  para un código de versión concreto, llama tú a `purchase` y `get_delivery`.
- Sin `profile`, elige uno de los dos perfiles más nuevos, y prefiere el que ya
  tenga el pool caliente.
- Intenta conseguir un token para ese perfil hasta tres veces, con 5 segundos
  entre intentos (no tras un 429).
- Devuelve la ruta del APK base y el nombre de versión, no el código.
- Usa `download_batch`, así que imprime barras de progreso y lanza
  `RuntimeError` dentro de un event loop en marcha.
- Puede lanzar cualquier `GoopdlError`, no solo `PlayAPIError`. Con el pool
  de tokens ocupado lanza `PoolBusyError` (en 1.4.x era `PlayAPIError`), y la
  CDN lanza `DownloadError` o `IntegrityError`. Mira
  [qué lanza qué](#qué-lanza-qué).

#### `validate_package`

`validate_package(package) -> str`

Todas las funciones de `api` que reciben un nombre de paquete la llaman antes
de cualquier petición de red.

```python
from goopdl.errors import InvalidPackageError

api.validate_package("com.whatsapp")  # devuelve "com.whatsapp"
try:
    api.validate_package("whatsapp")
except InvalidPackageError as exc:
    print(exc)  # Invalid package name: 'whatsapp'
```

### `goopdl.auth`

Todas las funciones de tokens aceptan `arch`, `country`, `proxy`,
`dispenser_url` y `profile`, con el significado de [conceptos](#conceptos).
Ante un fallo devuelven `None` (o `[]`), salvo que pases `raise_on_error=True`,
que lanza los errores tipados de [errores](#errores). En un servicio usa
`raise_on_error=True`: con `None` no sabes si es un límite de peticiones o una
caída.

#### `fetch_token`

`fetch_token(dispenser_url=None, arch="arm64", proxy=None, country=None, profile=None, raise_on_error=False, *, cooldown_retry=True) -> dict | None`

Un token nuevo, sin caché. Cada llamada es una petición al dispensador (Aurora
permite unas 20 por hora), así que mejor usa `pick_pool_token`. Dentro de una
[ventana de 429](#pool-de-tokens-y-caché) no envía nada: devuelve `None`, o
lanza `DispenserRateLimitedError` con `raise_on_error=True`.

```python
fresh = auth.fetch_token(arch="arm64", country="US", raise_on_error=True)
```

Sin `profile`, rota perfiles hasta que uno funciona, y para ante un HTTP 429 o
tras dos fallos seguidos de transporte o 5xx. Con `profile`, reintenta una vez
tras 5 segundos ante fallos de transporte o 5xx, salvo con
`cooldown_retry=False`.

#### `pick_pool_token`

`pick_pool_token(arch="arm64", country=None, proxy=None, dispenser_url=None, profile=None, size=5, raise_on_error=False) -> dict | None`

Llena el pool hasta `size` tokens válidos y devuelve el siguiente por turno.
Si el dispensador deja de emitir tokens (HTTP 429 incluido), usa los válidos
que tiene; con `raise_on_error=True` solo lanza el error si no queda ninguno.
Se usa en el [inicio rápido](#paso-a-paso). Puede devolver `None` incluso con
`raise_on_error=True` (perfil desconocido, directorio de caché inutilizable),
así que comprueba el resultado.

#### `ensure_pool`

`ensure_pool(arch="arm64", country=None, proxy=None, dispenser_url=None, profile=None, size=5, raise_on_error=False) -> list[dict]`

Calienta el pool sin coger ningún token, por ejemplo al arrancar un worker.
Puede devolver menos de `size` tokens si el dispensador deja de emitirlos
(HTTP 429 incluido). Con `raise_on_error=True` solo lanza el error si no queda
ningún token válido.

```python
tokens = auth.ensure_pool(arch="arm64", size=5, raise_on_error=True)
print(len(tokens))
```

#### `replace_pool_token`

`replace_pool_token(failed_token, arch="arm64", country=None, proxy=None, dispenser_url=None, profile=None, raise_on_error=False) -> dict | None`

Llámala tras un `AuthExpiredError`, con los mismos `arch`, `country` y
`profile` con los que cogiste el token. Quita el token muerto y devuelve uno
nuevo. Si no consigue sustituirlo, devuelve otro token que siga en el pool (o
`None`). Ante un HTTP 429 del dispensador también devuelve un token que siga en
el pool, incluso con `raise_on_error=True`. Solo si no queda ninguno válido
lanza `DispenserRateLimitedError`, incluso sin `raise_on_error`.

```python
from goopdl.errors import AuthExpiredError


def details_with_refresh(package, token, arch="arm64"):
    try:
        return api.get_details(package, token), token
    except AuthExpiredError:
        token = auth.replace_pool_token(token, arch=arch, raise_on_error=True)
        if token is None:
            raise
        return api.get_details(package, token), token


details, token = details_with_refresh(package, token)
```

#### Otras funciones

| Función | Qué hace |
| ------- | -------- |
| `get_config_dir() -> Path` | El directorio de caché vigente ahora mismo |
| `ensure_auth(arch="arm64", dispenser_url=None, force_refresh=False, proxy=None, country=None, profile=None) -> dict \| None` | Un único token en caché en `auth-*.json`, renovado a los 50 minutos (en modo cuenta, la sesión en memoria). Nunca lanza errores del dispensador |
| `clear_auth() -> None` | Borra todos los tokens en caché y los registros de dispositivo de las cuentas, y olvida las sesiones en memoria de ese directorio |
| `direct_auth_enabled() -> bool` | Si el modo cuenta está configurado. Lanza `DirectAuthConfigurationError` si lo está a medias |

### `goopdl.download`

Un `DownloadSpec` describe un archivo: `url`, `dest`, `cookies`, `label`,
`gzipped`, `integrity_required`, `expected_size`, `sha1`, `sha256`. goopdl lo
descarga en streaming a un archivo `.part` único junto a `dest`, lo comprueba y
lo renombra a su sitio. Si en `dest` ya hay un archivo con el tamaño y el hash
correctos, no se vuelve a descargar.

#### `download_files`

`download_files(specs, *, require_integrity=False, on_progress=None, max_concurrent=4, client=None) -> None`

El descargador async. No escribe nada en stdout.

```python
import asyncio

import httpx

from goopdl.download import download_files


def on_progress(label, done, total):
    print(f"{label}: {done}/{total or '?'} bytes")


async def main():
    timeout = httpx.Timeout(connect=15.0, read=300.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        await download_files(
            specs, require_integrity=True, on_progress=on_progress,
            max_concurrent=2, client=client,
        )


asyncio.run(main())
```

- `on_progress(label, done, total)` se ejecuta en línea en el event loop: una
  vez al empezar cada archivo y luego por cada bloque de hasta 64 KB. `done`
  cuenta los bytes recibidos (comprimidos, en los archivos gzip). `total` es el
  `Content-Length`, o `None`. Un archivo verificado que ya estaba en disco
  notifica `(label, size, size)` una sola vez. Que sea rápida: si lanza una
  excepción, se cancelan todas las descargas y tu excepción se propaga tal
  cual.
- `client` es opcional y goopdl nunca lo cierra. Ponle tú el timeout: el de
  httpx por defecto es de 5 segundos. Cuando goopdl crea su propio cliente usa
  el timeout del ejemplo.
- `max_concurrent` limita los archivos en paralelo dentro de esta llamada.
  Tiene que ser al menos 1 (si no, `ValueError`).
- Ante el primer fallo, o si se cancela la tarea, se cancelan las demás
  descargas y no queda ningún archivo `.part`.

#### `download_batch`

`download_batch(specs, require_integrity=False) -> None`

Envoltorio síncrono de `download_files` con barras de progreso de Rich en
stdout. Lanza `RuntimeError` si se llama con un event loop en marcha; desde
código async, haz `await` de `download_files`.

#### Integridad

- Un spec se verifica cuando tiene `integrity_required=True` o lleva `sha1` o
  `sha256`. La verificación compara el tamaño final con `expected_size` y el
  hash (SHA-256 si lo hay, si no SHA-1). Si no coinciden, lanza
  `IntegrityError`.
- Un spec con `integrity_required=True` pero sin tamaño o sin hash falla con
  `IntegrityError: missing integrity metadata`.
- `require_integrity=True` hace esa comprobación en todos los specs antes de
  descargar el primer byte, en lugar de fallar a mitad.
- Google envía hashes de los APK base, los splits y los `.dm`, no de los OBB ni
  de los asset packs.

#### `write_manifest`

`write_manifest(path, specs) -> None`

Escribe de forma atómica el JSON del
[manifiesto de integridad](commands.md#manifiesto-de-integridad). Todos los
specs necesitan hashes, así que pásale solo archivos verificados:

```python
from goopdl.download import write_manifest

write_manifest(output / "manifest.json", [s for s in specs if s.integrity_required])
```

## Backends async

### Llamadas bloqueantes en hilos

[`Client.adownload`](#async) ya hace esto por ti. Para controlar cada paso,
ten en cuenta que ninguna función de `api` ni de `auth` es async, y pueden
bloquear segundos o
minutos (un pool frío pide hasta cinco tokens, cada uno una petición al
dispensador con un timeout de 30 segundos). Ejecútalas en un hilo con
`asyncio.to_thread` y haz `await` de `download_files` directamente:

```python
import asyncio
from pathlib import Path

from goopdl import api, auth
from goopdl.download import download_files


def resolve_latest(package, arch="arm64"):
    """Blocking: token, details, purchase, and delivery."""
    token = auth.pick_pool_token(arch=arch, raise_on_error=True)
    if token is None:
        raise RuntimeError("no token available")
    details = api.get_details(package, token)
    delivery_token = api.purchase(package, details.version_code, token)
    delivery = api.get_delivery(
        package, details.version_code, token, delivery_token=delivery_token
    )
    return details, delivery


async def download_latest(package, output):
    api.validate_package(package)  # falla pronto, antes de ocupar un hilo
    details, delivery = await asyncio.to_thread(resolve_latest, package)
    output.mkdir(parents=True, exist_ok=True)
    spec = base_apk_spec(package, details.version_code, delivery, output)
    await download_files([spec], require_integrity=True)
    return spec.dest


print(asyncio.run(download_latest("com.whatsapp", Path("apks"))))
```

No llames a `download_app` ni a `download_batch` desde una corrutina: usan
`asyncio.run` y lanzan `RuntimeError`. En un hilo sí funcionan, pero imprimen
barras de progreso en el stdout de tu servidor.

### Ejemplo con FastAPI

```python
# No lo ejecuta la comprobación de la documentación: necesita FastAPI. download_latest está definida arriba.
from pathlib import Path

from fastapi import FastAPI, HTTPException

from goopdl.errors import (
    AppNotAvailableError,
    DispenserRateLimitedError,
    GoopdlError,
    InvalidPackageError,
    RateLimitedError,
)

app = FastAPI()


@app.post("/apps/{package}/download")
async def download(package: str):
    try:
        path = await download_latest(package, Path("/srv/apks"))
    except InvalidPackageError:
        raise HTTPException(422, "invalid package name")
    except AppNotAvailableError:
        raise HTTPException(404, "app not available")
    except (RateLimitedError, DispenserRateLimitedError) as exc:
        headers = {"Retry-After": str(int(exc.retry_after))} if exc.retry_after else None
        raise HTTPException(503, "rate limited", headers=headers)
    except GoopdlError as exc:
        raise HTTPException(502, type(exc).__name__)  # los mensajes no llevan secretos, pero sé escueto
    return {"file": path.name}
```

### Concurrencia

- Puedes llamar a la librería desde varios hilos y procesos a la vez. Cada pool
  (arch + país + perfil) tiene un archivo de bloqueo en el directorio de caché,
  y los cambios en el pool se escriben de forma atómica.
- El bloqueo se mantiene mientras se piden los tokens que faltan. Las demás
  llamadas al mismo pool esperan hasta 180 segundos y después reciben `None`
  (o `PoolBusyError` con `raise_on_error=True`). `download_app` lanza
  `PoolBusyError`; `fetch_app_item`, `PlayAPIError`.
- Son bloqueos de archivo del sistema operativo en disco local. No compartas el
  directorio de caché por NFS ni entre máquinas.
- `max_concurrent` se aplica a cada llamada a `download_files`. Entre
  peticiones, limita las descargas con tu propio `asyncio.Semaphore` y comparte
  un único `AsyncClient` con `client=` para reutilizar conexiones.
- Las llamadas síncronas a Google Play y al dispensador reutilizan conexiones
  keep-alive, compartidas por todos los hilos del proceso. Un proceso hijo
  creado con fork empieza con sus propias conexiones nuevas.
- El pool de hilos de `asyncio.to_thread` es pequeño. Dimensiónalo pensando en
  llamadas que se quedan dormidas esperando por límites de peticiones.

### Varios clientes (tenants)

Usa un [`Client` por cliente](#varios-tenants), cada uno con su `config_dir`
y sus `credentials`: comparten el proceso sin problemas.

Las funciones del módulo sacan el directorio de caché y las credenciales de
cuenta de variables de entorno que afectan a todo el proceso. No las cambies
en cada petición: otros hilos leerían los valores nuevos a mitad de una
llamada. Solo con las funciones, aísla a los clientes con un proceso worker
para cada uno:

```bash
GOOPDL_CONFIG_DIR=/var/lib/goopdl/tenant-a python worker.py
GOOPDL_CONFIG_DIR=/var/lib/goopdl/tenant-b python worker.py
```

## Errores

### Jerarquía

Todas las excepciones de goopdl derivan de `GoopdlError`. Impórtalas desde
`goopdl.errors` (o desde `goopdl`). Importar ese módulo no carga httpx; cada
clase carga su propio módulo la primera vez que la usas. `dir(goopdl)` y el
autocompletado también las muestran.

```text
GoopdlError
├── PlayAPIError                       .status_code
│   ├── NetworkError                   también httpx.TransportError
│   ├── AuthExpiredError
│   ├── RateLimitedError               .retry_after, status_code=429
│   ├── VersionUnavailableError
│   │   └── AppNotSupportedError
│   ├── AppNotAvailableError
│   ├── AppNotPurchasedError
│   └── MalformedResponseError
├── InvalidPackageError                también ValueError
├── InvalidArchError                   también ValueError
├── DirectAuthConfigurationError       también ValueError
├── DirectAuthError                    también RuntimeError; .stage, .status, .retry_after
│   ├── DispenserRateLimitedError      .retry_after, status=429
│   └── DispenserUnavailableError      .status, .reason
├── PoolBusyError                      también TimeoutError
├── AASTokenError                      también ValueError
├── BrowserOAuthError                  también RuntimeError
└── IntegrityError                     también RuntimeError
    └── DownloadError                  .status_code, .retryable, .retry_after
```

`NetworkError` sigue siendo un `httpx.TransportError` (y por tanto un
`httpx.HTTPError`), así que los `except httpx.HTTPError` antiguos siguen
funcionando.

Los errores del dispensador no son `PlayAPIError`: captura `DirectAuthError`
para cualquier fallo al conseguir un token. `download_app` y `fetch_app_item`
traducen algunos, mira abajo.

### Qué lanza qué

| Error | Lo lanza | Cuándo | ¿Reintentar? |
| ----- | -------- | ------ | ------------ |
| `InvalidPackageError` | Funciones de `api` que reciben un paquete | ID de aplicación mal formado, antes de cualquier petición | No |
| `InvalidArchError` | Funciones del pool y la caché, `download_app`, `fetch_app_item`, `Config` | `arch` no está en `VALID_ARCHS` | No |
| `DirectAuthConfigurationError` | Todas las funciones de tokens, `Credentials`, `Credentials.from_env` | Modo cuenta configurado a medias | No, arregla el entorno o los valores |
| `NetworkError` | Llamadas a Google Play de `api` | Timeout, DNS, conexión rechazada o cortada | Sí, con espera creciente |
| `AuthExpiredError` | `get_details*`, `search_apps`, `list_splits`, `purchase`, `get_delivery` | HTTP 401 | Una vez, tras `replace_pool_token` |
| `RateLimitedError` | Las mismas, más `fetch_app_item` y `download_app` | HTTP 429 de Play (`get_delivery` ya reintentó dos veces). En `download_app`, también un 429 del dispensador o del modo cuenta; en `fetch_app_item`, un 429 del dispensador al sustituir un token caducado sin ningún otro token válido en el pool. Si viene del dispensador, `__cause__` es el `DispenserRateLimitedError` | Solo tras `retry_after`; para si es `None` |
| `AppNotAvailableError` | `get_details`, `get_details_raw`, `fetch_app_item` | HTTP 404, o no hay documento para este dispositivo | No con el mismo perfil y país |
| `VersionUnavailableError` | `get_delivery` | HTTP 404, o no hay URL de descarga | No |
| `AppNotSupportedError` | `get_delivery` | El perfil no puede recibir esta versión | Con otro perfil o arch |
| `AppNotPurchasedError` | `get_delivery` | App de pago, o no disponible en la región | No |
| `MalformedResponseError` | Parsers de `api` | Protobuf que no se puede decodificar | No con otro token |
| `PlayAPIError` | Todo `api` | Cualquier otro estado HTTP (`.status_code`); sin token en `fetch_app_item` / `download_app` | 5xx más tarde; el resto no |
| `DispenserRateLimitedError` | Funciones de tokens con `raise_on_error=True`; `replace_pool_token` incluso sin él. Las funciones del pool solo lo lanzan si no queda ningún token válido. `fetch_app_item` y `download_app` lo envuelven en `RateLimitedError` | HTTP 429 del dispensador, o una llamada dentro de la [ventana](#pool-de-tokens-y-caché) que abrió un 429, sin petición; entonces `retry_after` es el tiempo que falta | Solo tras `retry_after` |
| `DispenserUnavailableError` | Funciones de tokens con `raise_on_error=True` | `.reason`: `transport`, `server` (5xx) o `profile_rejected` | `transport`/`server` más tarde; `profile_rejected` con otro perfil |
| `DirectAuthError` | Funciones de tokens en modo cuenta con `raise_on_error=True` | El inicio de sesión falló en `.stage` (`checkin`, `device config upload`, `token exchange`) con `.status`. Un 429 en la llamada opcional `toc` no es un error: el token llega sin `dfeCookie` y se registra un aviso | 429 tras `.retry_after`; transporte/5xx más tarde; 400/401/403 no |
| `PoolBusyError` | Funciones del pool con `raise_on_error=True`; `download_app` siempre | No se obtuvo el bloqueo del pool en 180 s | Más tarde |
| `IntegrityError` | `download_files`, `download_batch`, `write_manifest` | Faltan metadatos, o no coinciden tamaño o hash | No |
| `DownloadError` | `download_files`, `download_batch` | Fallo HTTP o de transporte con la CDN, o un `Location` de redirección mal formado (nunca reintentable) | Si `.retryable`; ante un 403, pide una entrega nueva |

`download_app` y `fetch_app_item` no dejan pasar los errores del dispensador.
Un 429 se convierte en `RateLimitedError`, con el mismo `retry_after` y el
original como `__cause__`. En `download_app`, una caída del dispensador se
convierte en `PlayAPIError` con `status_code`; en `fetch_app_item`, cualquier
otro fallo al conseguir un token es un `PlayAPIError`. Pero no todo lo que
lanzan es `PlayAPIError`: `download_app` también puede lanzar
`PoolBusyError`, `DownloadError`, `IntegrityError`, `InvalidPackageError`,
`InvalidArchError` y `DirectAuthConfigurationError`, y `fetch_app_item`, los
tres últimos. Termina con `except GoopdlError`.

Cuenta también con `ValueError` de `download_files` (`max_concurrent`
incorrecto), `RuntimeError` de `download_batch` dentro de un event loop,
`OSError` de tu directorio de salida y, en `fetch_aas_token`, errores de
`httpx` saneados.

### Reintentos

Reintenta solo lo que el error dice que se puede reintentar, y espera lo que
pida el servidor. No reintentes un 429 sin `Retry-After`: espera a un nivel más
alto, o cambia de red o de cuenta. Cuando `get_delivery` lanza el error, ya ha
reintentado dos veces.

```python
import time

from goopdl.errors import DispenserRateLimitedError, NetworkError, RateLimitedError


def call_with_backoff(fn, *args, attempts=3, max_wait=60.0, **kwargs):
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except (RateLimitedError, DispenserRateLimitedError) as exc:
            wait = exc.retry_after
            if wait is None or wait > max_wait or attempt == attempts - 1:
                raise  # el servidor no da plazo, o es demasiado largo: decide quien llama
        except NetworkError:
            if attempt == attempts - 1:
                raise
            wait = 2 ** attempt
        time.sleep(wait)


details = call_with_backoff(api.get_details, package, token)
```

También sirve para `fetch_app_item` y `download_app`: informan de todos los
429, incluidos los del dispensador, como `RateLimitedError`. Dentro de la
[ventana de 429](#pool-de-tokens-y-caché) del dispensador, `retry_after` es el
tiempo que falta, así que basta con esperar eso.
`DispenserRateLimitedError` solo te llega de las funciones de tokens de
`goopdl.auth`.

En las descargas, `DownloadError` te dice qué hacer. Un 403 significa que las
URLs firmadas caducaron, así que hace falta una entrega nueva. El resto de
errores reintentables pueden reutilizar las mismas URLs:

```python
from goopdl.errors import DownloadError


async def download_with_retries(package, output, attempts=3):
    details, delivery = await asyncio.to_thread(resolve_latest, package)
    for attempt in range(attempts):
        spec = base_apk_spec(package, details.version_code, delivery, output)
        try:
            await download_files([spec], require_integrity=True)
            return spec.dest
        except DownloadError as exc:
            if attempt == attempts - 1:
                raise
            if exc.status_code == 403:
                details, delivery = await asyncio.to_thread(resolve_latest, package)
            elif exc.retryable:
                await asyncio.sleep(exc.retry_after or 2 ** attempt)
            else:
                raise


print(asyncio.run(download_with_retries("com.whatsapp", Path("apks"))))
```

Captura `DownloadError` antes que `IntegrityError`, porque es una subclase. Un
`IntegrityError` que no es `DownloadError` significa que los bytes o los
metadatos están mal: no lo reintentes a ciegas.

## Seguridad

- **Los tokens son secretos.** No registres nunca el diccionario `auth`, su
  `authToken`, ni las URLs y cookies de un `DeliveryResult` (las URLs están
  firmadas).
- **Los errores no llevan secretos, con un límite.** Los mensajes de las
  excepciones públicas no contienen tokens, URLs ni cookies. Cuando fallan
  `download_app` o `fetch_app_item`, el traceback solo conserva su propio
  frame, con el proxy borrado: ni token del pool, ni token de entrega, ni URL
  firmada de la CDN. La única excepción encadenada que se mantiene es un
  `__cause__` de goopdl ya saneado, como `DispenserRateLimitedError`.
  `get_delivery` suelta el token de entrega, el proxy y la respuesta antes de
  lanzar el error, y con el modo cuenta configurado a medias
  `DirectAuthConfigurationError` no deja el email ni el token AAS en ningún
  frame. Los métodos de `Client` se comportan como `download_app`: el email y
  el token AAS no quedan en ningún frame que dejen atrás, salvo dentro de los
  objetos `Client`, `Config` y `Credentials` que creaste tú. Lo que pasas tú
  se queda, y es a propósito: el diccionario `auth`
  sigue en el frame de la función a la que se lo diste (`get_details`,
  `get_delivery`, …), y el `auth` y la URL del proxy siguen en tus propios
  frames. Si tu herramienta de errores captura variables locales
  (`include_local_variables` de Sentry, por ejemplo), desactívalo o filtra
  `authToken` y las credenciales del proxy.
- **Validación de entradas.** Los nombres de paquete pasan por
  `validate_package` y las arquitecturas se comprueban contra `VALID_ARCHS`
  antes de llegar a una URL o a un nombre de archivo. Los códigos de país se
  reducen a letras y dígitos en los nombres de los archivos de caché.
- **Solo hosts de Google.** Las URLs de entrega solo se conservan si son HTTPS
  en el puerto 443 bajo `android.clients.google.com`, `*.google.com`,
  `*.googleapis.com`, `*.ggpht.com`, `*.googleusercontent.com` o
  `*.gvt1.com`. Este último es donde acaba una entrega real:
  `play.googleapis.com/download/by-token` responde 302 y el archivo sale de
  `rr<N>---sn-<id>.gvt1.com`.
  `download_files` sigue como mucho cinco redirecciones, solo a esos hosts, y
  quita las cabeceras `Authorization`, `Proxy-Authorization` y `Cookie` en las
  redirecciones a otro host. No comprueba la `spec.url` inicial, así que pásale
  solo URLs que vengan de `get_delivery`.
- **Rutas de salida.** `dest` se usa tal cual. Si construyes nombres de archivo
  con datos del servidor (nombres de splits), sanéalos como en
  [`get_delivery`](#get_delivery).

### Logging

goopdl escribe en el árbol de loggers `goopdl`, con un `NullHandler` en
`goopdl`, así que no muestra nada hasta que tu aplicación configure el logging.
Nunca registra tokens. Los mensajes usan INFO (recarga del pool, perfil
elegido), WARNING (fallos del dispensador, con el nombre del perfil de
dispositivo) y ERROR. Para verlo todo:

```python
import logging

logging.basicConfig(level=logging.WARNING)
logging.getLogger("goopdl").setLevel(logging.DEBUG)
logging.getLogger("httpx").setLevel(logging.WARNING)  # en INFO registra las URLs completas
```

Deja `httpx` en WARNING o más: en INFO registra la URL de cada petición,
incluidos los tokens de entrega y las URLs firmadas de la CDN.

## Migrar desde 1.4.x

Las llamadas existentes mantienen su firma (los parámetros nuevos son
opcionales). Esto es lo que cambia:

| Cambio | 1.4.x | Ahora | Migración |
| ------ | ----- | ----- | --------- |
| Protobuf corrupto | `ValueError` / `EOFError` sin más | `MalformedResponseError` (un `PlayAPIError`) | `except PlayAPIError` lo cubre |
| Nombres de paquete | Se enviaban a Google tal cual | `InvalidPackageError` (un `ValueError`) si no hay punto o hay un carácter no válido | Llama a `api.validate_package()` al recibir la entrada |
| `arch` desconocida | Se usaba en el nombre de los archivos de caché | `InvalidArchError` (un `ValueError`) | Compruébala contra `profiles.VALID_ARCHS` |
| Directorio de caché | Siempre `~/.config/goopdl` | `auth.get_config_dir()`: `$GOOPDL_CONFIG_DIR`, `$XDG_CONFIG_HOME/goopdl`, `~/.config/goopdl` | `export GOOPDL_CONFIG_DIR=~/.config/goopdl` conserva los tokens antiguos |
| `download_app` con el pool de tokens ocupado | `PlayAPIError` | `PoolBusyError` (un `TimeoutError`, no un `PlayAPIError`) | `except PoolBusyError`, o termina con `except GoopdlError` |
| Dependencia `typer` | `typer>=0.9` | `typer>=0.16` | Sube tu versión fijada si es menor |
| Fallos de la CDN | `IntegrityError("download failed …")` | `DownloadError`, subclase de `IntegrityError` | `except DownloadError` antes de `except IntegrityError` |
| Fallos de transporte en `api` | Excepciones de httpx sin envolver (`httpx.ConnectError`, …) | `NetworkError`, que sigue siendo `httpx.TransportError` | `except NetworkError`; los handlers de subclases concretas de httpx ya no coinciden |
| `purchase` ante HTTP 429 | Devolvía `""` | Lanza `RateLimitedError` (el 401 ya lanzaba `AuthExpiredError`) | Trátalo como cualquier otro 429 |
| Redirecciones de la CDN | Se seguían a cualquier sitio | Como mucho cinco, solo a hosts de Google | Nada, salvo que dependas de otros hosts |
| Salida por consola | `goopdl.auth` imprimía en stderr con Rich | Las funciones de tokens registran en logs en vez de imprimir | Configura el [logging](#logging) |
| `goopdl.auth.console` / `Console` | Objetos de Rich | Obsoletos, con `DeprecationWarning` | `rich.console.Console(stderr=True)` |
| `goopdl.download.Progress`, `BarColumn`, … | Reexportados de Rich | Obsoletos, con `DeprecationWarning` | `from rich.progress import Progress` |

Novedades compatibles con lo anterior: la base `GoopdlError`,
`raise_on_error` en las funciones de tokens, `download_files`, y que
`download_app` y `fetch_app_item` lancen `RateLimitedError` donde 1.4.x
lanzaba un `PlayAPIError` genérico.

## Solución de problemas

| Síntoma | Causa y solución |
| ------- | ---------------- |
| `RuntimeError: download_batch() cannot run inside a running event loop` | Haz `await` de `download_files`, ver [backends async](#backends-async) |
| Barras de progreso en los logs del servidor | Las pintan `download_app` / `download_batch`. Usa `Client.download`, `Client.adownload` o `download_files` |
| `pick_pool_token` devuelve `None` | Pasa `raise_on_error=True` para ver el motivo tipado. Si sigue en `None`: perfil desconocido o directorio de caché inutilizable (mira los logs) |
| `PoolBusyError` | Otro hilo o proceso está llenando el mismo pool. Reintenta más tarde |
| Se vuelven a pedir tokens tras actualizar | La caché ha cambiado de sitio, ver [migración](#migrar-desde-14x) |
| `DirectAuthConfigurationError` | Define `GOOPDL_ACCOUNT_EMAIL` y `GOOPDL_AAS_TOKEN`, o ninguna de las dos. Desde `Credentials`: pasa los dos valores, con el token empezando por `aas_et/` |
| Un `Client` ignora `GOOPDL_ACCOUNT_EMAIL` | Es a propósito, ver [credentials](#credentials). Usa `Credentials.from_env()` |
| `DownloadError` con `status_code == 403` | Las URLs de entrega caducaron. Vuelve a llamar a `get_delivery` |
| `AppNotAvailableError` con una app que existe | Está oculta para este perfil o país. Prueba otro `profile`, `arch` o `country` con un `proxy` de ese país |

## Límites conocidos

- Los fragmentos de esta guía se comprueban contra un Google Play simulado, en
  Linux. Esa comprobación no los ejecuta contra el servicio real, y la librería
  no está probada en Windows ni en macOS.
- Las URLs de entrega en hosts fuera de la lista permitida se descartan. Si
  Google cambia su CDN, `get_delivery` lanzará `VersionUnavailableError` hasta
  que se actualice goopdl.
- No hay API async para tokens y metadatos: `Client.adownload` y
  `download_files` son las únicas corrutinas. Para lo demás, usa
  `asyncio.to_thread`.
- Para las funciones del módulo, el directorio de caché y las credenciales de
  cuenta son por proceso (variables de entorno), no por llamada. Un `Client`
  los fija en cada llamada.
- `Client.download` y `Client.adownload` solo descargan la última versión.
  Para un código de versión concreto, usa `Client.delivery` y
  `download_files`.
- Las llamadas síncronas usan un pool de conexiones propio de goopdl por
  proceso; no se les puede pasar un cliente. Solo `download_files` y
  `Client.adownload` aceptan un cliente compartido, para las descargas del
  CDN.
