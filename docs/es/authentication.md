# Autenticación

[English](../authentication.md) · **Español**

goopdl habla con Google Play como si fuera un dispositivo Android, así que
cada petición necesita un token. Hay dos formas de conseguirlo:

| Modo | Qué necesitas | Ideal para |
| ---- | ------------- | ---------- |
| [Anónimo](#anónimo-por-defecto) (por defecto) | Nada | La mayoría de descargas |
| [Cuenta de Google](#cuenta-de-google) | Una cuenta desechable, con la sesión iniciada mediante `goopdl auth login` | Apps a las que no llegan las cuentas anónimas, o cuando el dispensador te limita |

`goopdl auth status` muestra qué modo está activo.

## Anónimo (por defecto)

Por defecto, goopdl pide un token anónimo al dispensador público de tokens de
[Aurora Store](https://auroraoss.com/) (`https://auroraoss.com/api/auth`). No
necesitas cuenta de Google.

```bash
goopdl auth token                    # token arm64
goopdl auth token --arch armv7       # token para dispositivos antiguos de 32 bits
goopdl auth token --country IN       # registra el dispositivo con el MCC/MNC de India
goopdl auth logout                   # borra todos los tokens y registros de dispositivo en caché
```

No hace falta ejecutar `auth token` primero: el resto de comandos consiguen un
token cuando lo necesitan. Todas las opciones están en
[`auth token`](commands.md#auth-token).

### Dónde se guardan los tokens

Los tokens se guardan en el directorio de configuración de goopdl, que es el
primero de estos que aplique:

1. `$GOOPDL_CONFIG_DIR`, si es una ruta absoluta. Se expande `~`.
2. `$XDG_CONFIG_HOME/goopdl`, si `XDG_CONFIG_HOME` es una ruta absoluta.
3. `~/.config/goopdl`.

Si alguna de las dos variables tiene una ruta relativa, se ignora, para que la
caché nunca acabe en el directorio actual. En Linux y macOS el directorio se
crea con permisos `0700` y los archivos de tokens se escriben con `0600`.

> [!NOTE]
> Hasta la 1.4.x goopdl usaba siempre `~/.config/goopdl`. Si tienes
> `XDG_CONFIG_HOME` apuntando a otro sitio, goopdl ahora usa
> `$XDG_CONFIG_HOME/goopdl` y la primera vez pide tokens nuevos. Para seguir
> usando la ubicación antigua, define `GOOPDL_CONFIG_DIR=~/.config/goopdl`.

| Archivo dentro de ese directorio | Contenido |
| -------------------------------- | --------- |
| `auth-{arch}[-{country}][-profile-{key}].json` | Token de la última ejecución de `auth token` |
| `token-pool-{arch}[-{country}][-profile-{key}].json` | Pool de hasta 5 tokens que usan `download`, `latest` y `fast-download` |
| `direct-device-{account}-{arch}[-{country}]-profile-{key}.json` | Solo en el modo [cuenta de Google](#qué-guarda-el-modo-cuenta): el dispositivo registrado, sin token |
| `account.json` | La [cuenta de Google guardada](#dónde-se-guarda-la-cuenta): email y token AAS |

La parte `-profile-{key}` aparece cuando fijas un dispositivo con `--profile`,
así cada perfil tiene sus propios tokens. `goopdl auth logout` borra todos
estos archivos. `goopdl auth token` muestra la ruta donde ha guardado el token,
y `goopdl auth status` muestra el directorio y cuántos archivos tiene.

Los tokens caducan a los 50 minutos. En [cómo funciona](how-it-works.md#pool-de-tokens)
se explica el pool.

### Dispensador propio

Usa un dispensador autoalojado en cualquier comando con `--dispenser` / `-d`:

```bash
goopdl auth token -d https://my-dispenser.example.com/api/auth
goopdl download com.whatsapp -d https://my-dispenser.example.com/api/auth
```

## Cuenta de Google

Inicia sesión una vez y goopdl se salta el dispensador e inicia sesión
directamente en Google:

```bash
goopdl auth login              # abre un navegador y guarda la cuenta
goopdl download com.whatsapp   # la usa
goopdl auth status             # muestra la cuenta en uso, nunca el token
goopdl auth logout             # la borra junto con todos los tokens en caché
```

También puedes pasar la cuenta en dos variables, por ejemplo en CI o en un
contenedor:

```bash
export GOOPDL_ACCOUNT_EMAIL="account@example.com"
export GOOPDL_AAS_TOKEN="aas_et/..."
goopdl download com.whatsapp
```

- Las variables siempre tienen prioridad sobre la cuenta guardada.
- Si defines solo una de las dos variables, goopdl termina con error. No vuelve
  a la cuenta guardada ni al modo anónimo.
- El token AAS tiene que empezar por `aas_et/`.
- Los bearer tokens temporales de Google Play se quedan en memoria y nunca se
  escriben en disco. Cada proceso inicia sesión una vez por cuenta,
  arquitectura, país y perfil de dispositivo, y reutiliza el token hasta que
  caduca (la caducidad que indica Google menos 5 minutos, o 50 minutos) o
  Google lo rechaza.
- Aparte de `account.json`, en disco goopdl solo escribe el registro del
  dispositivo, ver abajo.
- Solo la CLI lee `account.json`. Desde Python, un
  [`Client`](library.md#credentials) recibe la cuenta como `Credentials` e
  ignora tanto las variables como el archivo.

> [!WARNING]
> Un token AAS es una credencial de larga duración con acceso completo a Play
> Store en esa cuenta. Usa una cuenta de Google desechable, nunca la principal.
> `goopdl auth login` guarda el token en disco; usa `--print` si prefieres
> tenerlo en un gestor de secretos. Cambiar la contraseña de la cuenta revoca
> el token.

### Dónde se guarda la cuenta

`goopdl auth login` escribe `account.json` en el
[directorio de configuración](#dónde-se-guardan-los-tokens): el email y el
token AAS, en JSON.

- El archivo se escribe de forma atómica con permisos `0600`, dentro del
  directorio `0700`.
- En Linux y macOS goopdl rechaza el archivo si otros usuarios pueden leerlo o
  si no es un archivo normal tuyo. Ejecuta `chmod 600` sobre él, o vuelve a
  iniciar sesión. En Windows goopdl no comprueba los permisos: el archivo
  depende de los de tu perfil de usuario.
- Un archivo mal formado también da error. Vuelve a ejecutar
  `goopdl auth login`, o `goopdl auth logout`.
- `goopdl auth logout` lo borra, junto con todos los tokens en caché.
  `auth.clear_auth()` y `Client.clear_cache()` no lo tocan.

### Qué guarda el modo cuenta

Iniciar sesión registra un dispositivo Android en la cuenta. Para no añadir uno
más con cada comando, goopdl guarda el dispositivo que registró y lo reutiliza
en las ejecuciones siguientes y en otros procesos:

| Archivo en el [directorio de configuración](#dónde-se-guardan-los-tokens) | Contenido |
| ------------------------------------------------------------------------- | --------- |
| `direct-device-{account}-{arch}[-{country}]-profile-{key}.json` | El registro del dispositivo: GSF ID, token de consistencia del check-in y token de configuración del dispositivo |

- `{account}` es un hash del email. El email, el token AAS y el bearer token
  nunca se escriben en él, ni en el nombre ni en el archivo.
- El archivo se escribe de forma atómica con permisos `0600`, dentro del
  directorio `0700`. Un archivo `.lock` al lado permite que varios procesos
  compartan un mismo registro, así que registran un solo dispositivo.
- Si Google rechaza dos tokens seguidos en ese dispositivo (HTTP 401), goopdl
  registra otro.
- Si no se puede escribir en el directorio, goopdl guarda el registro en
  memoria para el proceso actual y registra un aviso.
- `goopdl auth logout` borra estos archivos. Desde Python, usa
  `auth.clear_auth()` o [`Client.clear_cache()`](library.md#borrar-la-caché).
- `latest`, `download --version` y los reintentos con otros perfiles de
  dispositivo siguen iniciando sesión con un dispositivo nuevo en cada sondeo:
  necesitan GSF IDs distintos.

### Conseguir un token AAS

Elige la opción que te resulte más cómoda:

- **`goopdl auth login`** (recomendado en escritorio). Ver abajo.
- **`goopdl auth login --manual`**, si la automatización del navegador no
  funciona.
- **[goopdl-auth](https://github.com/Villoh/goopdl-auth)**: una pequeña app de
  Android. Inicias sesión en el móvil y te muestra el token AAS. No guarda
  credenciales ni tokens. Pon el token en las variables de arriba.

`goopdl auth login` guarda la cuenta. Añade `--print` para que solo imprima el
token por stdout, para las variables o un gestor de secretos:

```bash
TOKEN=$(goopdl auth login --print)
```

Las preguntas y los mensajes van a stderr, así que en `TOKEN` solo queda el
token. goopdl ya no inicia sesión con contraseña: Google rechaza tanto la
contraseña de la cuenta como las contraseñas de aplicación con
`BadAuthentication`.

#### Inicio de sesión con navegador (recomendado)

```bash
goopdl auth login
```

goopdl abre Chrome, Edge, Chromium o Brave con un perfil temporal nuevo. Inicia
sesión y selecciona **Acepto** (**I agree**). goopdl captura el email y el
`oauth_token` de un solo uso, cierra el navegador, borra el perfil y canjea el
`oauth_token` por un token AAS.

Si goopdl no encuentra tu navegador, define `GOOPDL_BROWSER` con la ruta a su
ejecutable o usa `--manual`.

#### Inicio de sesión manual

```bash
goopdl auth login --manual
```

goopdl abre [Google EmbeddedSetup](https://accounts.google.com/EmbeddedSetup)
en tu navegador habitual. Usa una ventana privada si tiene abierta la sesión de
otra cuenta de Google.

1. Inicia sesión y selecciona **Acepto** (**I agree**).
2. Abre las herramientas de desarrollo (`F12`).
3. En Chrome o Edge, ve a **Aplicación → Cookies →
   `https://accounts.google.com`**.
4. Copia la cookie `oauth_token`. Empieza por `oauth2_4/`.
5. Vuelve a la terminal, introduce el mismo email y pega el token. El token no
   se muestra al pegarlo.

El `oauth_token` solo sirve una vez y caduca enseguida, así que pégalo cuanto
antes.
