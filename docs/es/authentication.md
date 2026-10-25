# Autenticación

[English](../authentication.md) · **Español**

goopdl habla con Google Play como si fuera un dispositivo Android, así que
cada petición necesita un token. Hay dos formas de conseguirlo:

| Modo | Qué necesitas | Ideal para |
| ---- | ------------- | ---------- |
| [Anónimo](#anónimo-por-defecto) (por defecto) | Nada | La mayoría de descargas |
| [Cuenta de Google](#cuenta-de-google) | Una cuenta desechable y su token AAS | Apps a las que no llegan las cuentas anónimas, o cuando el dispensador te limita |

## Anónimo (por defecto)

Por defecto, goopdl pide un token anónimo al dispensador público de tokens de
[Aurora Store](https://auroraoss.com/) (`https://auroraoss.com/api/auth`). No
necesitas cuenta de Google.

```bash
goopdl auth                    # token arm64
goopdl auth --arch armv7       # token para dispositivos antiguos de 32 bits
goopdl auth --country IN       # registra el dispositivo con el MCC/MNC de India
goopdl auth --clear            # borra todos los tokens en caché
```

No hace falta ejecutar `auth` primero: el resto de comandos consiguen un token
cuando lo necesitan. Todas las opciones están en [`auth`](commands.md#auth).

### Dónde se guardan los tokens

| Archivo | Contenido |
| ------- | --------- |
| `~/.config/goopdl/auth-{arch}[-{country}][-profile-{key}].json` | Token de la última ejecución de `auth` |
| `~/.config/goopdl/token-pool-{arch}[-{country}][-profile-{key}].json` | Pool de hasta 5 tokens que usan `download`, `latest` y `fast-download` |

La parte `-profile-{key}` aparece cuando fijas un dispositivo con `--profile`,
así cada perfil tiene sus propios tokens. `goopdl auth --clear` los borra todos.

Los tokens caducan a los 50 minutos. En [cómo funciona](how-it-works.md#pool-de-tokens)
se explica el pool.

### Dispensador propio

Usa un dispensador autoalojado en cualquier comando con `--dispenser` / `-d`:

```bash
goopdl auth -d https://my-dispenser.example.com/api/auth
goopdl download com.whatsapp -d https://my-dispenser.example.com/api/auth
```

## Cuenta de Google

Define las dos variables y goopdl se salta el dispensador e inicia sesión
directamente en Google:

```bash
export GOOPDL_ACCOUNT_EMAIL="account@example.com"
export GOOPDL_AAS_TOKEN="aas_et/..."
goopdl download com.whatsapp
```

- El token AAS tiene que empezar por `aas_et/`.
- Si defines solo una de las dos variables, goopdl termina con error. No vuelve
  al modo anónimo.
- En este modo goopdl no lee archivos de configuración, archivos de secretos ni
  opciones de línea de comandos.
- Los bearer tokens temporales de Google Play se quedan en memoria y nunca se
  guardan en caché.

> [!WARNING]
> Un token AAS es una credencial de larga duración con acceso completo a Play
> Store en esa cuenta. Usa una cuenta de Google desechable, nunca la principal.
> Cambiar la contraseña de la cuenta revoca el token.

### Conseguir un token AAS

Elige la opción que te resulte más cómoda:

- **[goopdl-auth](https://github.com/Villoh/goopdl-auth)**: una pequeña app de
  Android. Inicias sesión en el móvil y te muestra el token AAS. No guarda
  credenciales ni tokens.
- **`goopdl aastoken --browser`** (recomendado en escritorio). Ver abajo.
- **`goopdl aastoken --oauth`**, si la automatización del navegador no funciona.
- **`goopdl aastoken`** con contraseña. Google suele rechazarla.

Todos los modos imprimen el token en la consola. goopdl nunca crea archivos
`credentials.txt` ni `token.txt`.

#### OAuth con navegador (recomendado)

```bash
goopdl aastoken --browser
```

goopdl abre Chrome, Edge, Chromium o Brave con un perfil temporal nuevo. Inicia
sesión y selecciona **Acepto** (**I agree**). goopdl captura el email y el
`oauth_token` de un solo uso, cierra el navegador, borra el perfil y canjea el
`oauth_token` por un token AAS.

Si goopdl no encuentra tu navegador, define `GOOPDL_BROWSER` con la ruta a su
ejecutable.

#### OAuth manual

```bash
goopdl aastoken --oauth
```

1. Abre [Google EmbeddedSetup](https://accounts.google.com/EmbeddedSetup).
2. Inicia sesión y selecciona **Acepto** (**I agree**).
3. Abre las herramientas de desarrollo (`F12`).
4. En Chrome o Edge, ve a **Aplicación → Cookies →
   `https://accounts.google.com`**.
5. Copia la cookie `oauth_token`. Empieza por `oauth2_4/`.
6. Ejecuta `goopdl aastoken --oauth`, introduce el mismo email y pega el token.

El `oauth_token` solo sirve una vez y caduca enseguida, así que pégalo cuanto
antes.

#### Contraseña o contraseña de aplicación

```bash
goopdl aastoken                               # pide el email y oculta la contraseña
goopdl aastoken user@example.com password     # la contraseña queda en el historial de la shell
```

Google suele rechazar tanto la contraseña de la cuenta como las contraseñas de
aplicación de 16 caracteres con `BadAuthentication`. Mantén activada la
verificación en dos pasos y usa uno de los modos OAuth.
