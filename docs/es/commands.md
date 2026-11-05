# Referencia de comandos

[English](../commands.md) · **Español**

`goopdl COMANDO --help` muestra esta misma información en la terminal.

| Comando | Qué hace |
| ------- | -------- |
| [`download`](#download) | Descarga los APKs de una app |
| [`fast-download`](#fast-download) | Descarga la última versión con un solo token |
| [`latest`](#latest) | Encuentra la versión más reciente que Google está desplegando |
| [`inspect-delivery`](#inspect-delivery) | Muestra qué entregaría una versión, sin descargar |
| [`info`](#info) | Muestra los detalles de una app |
| [`search`](#search) | Busca en Google Play |
| [`list-splits`](#list-splits) | Lista los split APKs de una app |
| [`profiles`](#profiles) | Lista los perfiles de dispositivo incluidos |
| [`auth login`](#auth-login) | Inicia sesión con una cuenta de Google y la guarda |
| [`auth logout`](#auth-logout) | Borra la cuenta guardada y todos los tokens en caché |
| [`auth clear-cache`](#auth-clear-cache) | Borra los tokens en caché y los registros de dispositivo, conserva la cuenta guardada |
| [`auth status`](#auth-status) | Muestra qué credenciales usa goopdl |
| [`auth token`](#auth-token) | Consigue y guarda un token anónimo |

## Opciones comunes

Casi todos los comandos que hablan con Google Play aceptan estas:

| Opción | Corta | Descripción |
| ------ | ----- | ----------- |
| `--arch` | | Tipo de dispositivo: `arm64` (por defecto), `armv7`, `x86`, `x86_64` o `tv`. Ver [arquitecturas](how-it-works.md#arquitecturas) |
| `--country` | `-c` | Código de país de 2 letras, como `US` o `IN` |
| `--proxy` | `-p` | URL del proxy, como `socks5://host:1080` o `http://host:8080` |
| `--profile` | | Clave o parte del nombre de un perfil de dispositivo, como `D2` o `Samsung`. Ver [`profiles`](#profiles) |
| `--dispenser` | `-d` | URL de un dispensador de tokens propio. Ver [dispensador propio](authentication.md#dispensador-propio) |

`--country` solo cambia las cabeceras de región. Para obtener la variante real
del APK de una región, enruta además el tráfico por un proxy de esa región con
`--proxy`.

---

## `download`

Descarga el APK base. Añade `--splits` para los split APKs y `--extras` para
los archivos OBB y los paquetes de Play Asset Delivery.

```bash
goopdl download com.whatsapp                            # APK base, última versión
goopdl download com.whatsapp --splits                   # + split APKs
goopdl download com.whatsapp --splits --extras          # + archivos OBB y asset packs
goopdl download com.whatsapp -o ./apks                  # carpeta de salida propia
goopdl download com.whatsapp -a arm64,armv7             # una descarga por arquitectura
goopdl download com.google.android.katniss -a tv        # build de Android TV
goopdl download com.whatsapp -v 231205015               # código de versión exacto
goopdl download com.instagram.android -v 434.0.0.44.74  # nombre de versión exacto
goopdl download com.whatsapp --splits -l de,fr,zh-CN    # splits de idioma extra
goopdl download com.whatsapp --dm                       # metadatos DEX para un primer arranque más rápido
goopdl download com.whatsapp -c IN -p socks5://host:1080  # variante regional
```

| Opción | Corta | Por defecto | Descripción |
| ------ | ----- | ----------- | ----------- |
| `--output` | `-o` | `.` | Carpeta de salida |
| `--arch` | `-a` | `arm64` | Tipo de dispositivo. Separa varios por comas para descargarlos uno tras otro; goopdl para en el primero que falle |
| `--version` | `-v` | la última | Código de versión (`384009971`) o nombre de versión (`434.0.0.44.74`). Con un nombre de versión, goopdl sondea GSF IDs nuevos hasta que uno ve esa versión |
| `--splits` / `--no-splits` | | `--no-splits` | Incluye los split APKs |
| `--extras` / `--no-extras` | | `--no-extras` | Incluye los archivos OBB y los asset packs |
| `--locale` | `-l` | inglés | Splits de idioma extra, separados por comas. Requiere `--splits` |
| `--dm` | | no | Descarga los metadatos DEX verificados (`.dm`) |
| `--integrity-manifest` | | — | Escribe un manifiesto JSON verificado. Ver [manifiesto de integridad](#manifiesto-de-integridad) |

También acepta las [opciones comunes](#opciones-comunes).

### Archivos generados

| Tipo | Nombre | Ejemplo |
| ---- | ------ | ------- |
| APK base | `{package}-{vc}.apk` | `com.whatsapp-231205015.apk` |
| Split APK | `{package}-{vc}-{split}.apk` | `com.whatsapp-231205015-config.arm64_v8a.apk` |
| OBB (main/patch) | `{type}.{vc}.{package}.obb` | `main.20925.com.tencent.ig.obb` |
| Asset pack | `{package}-{vc}-asset.apk` | `com.tencent.ig-20925-asset.apk` |

`{vc}` es el código de versión. Tras descargar una app con splits, goopdl
muestra un comando `adb install-multiple` con exactamente los APK y archivos
`.dm` descargados, entrecomillados para tu shell (PowerShell en Windows):

```bash
adb install-multiple com.whatsapp-231205015.apk com.whatsapp-231205015-config.arm64_v8a.apk
```

### Manifiesto de integridad

`--integrity-manifest RUTA` hace que la descarga falle si Google no envía el
tamaño y el hash de cada APK base y split pedido. goopdl:

1. Verifica cada archivo contra esos metadatos antes de moverlo a su sitio.
   Usa SHA-256 y solo recurre a SHA-1 si falta SHA-256.
2. Escribe el manifiesto de forma atómica: nombre relativo, tamaño, algoritmo
   y hash en Base64url de cada archivo.

Con varias arquitecturas, un solo manifiesto recoge los archivos de todas.
Google no envía hashes de los archivos OBB ni de los asset packs, así que los
archivos de `--extras` se descargan sin verificar y no aparecen en el
manifiesto. El manifiesto nunca contiene URLs de descarga, cookies, cabeceras
ni tokens.

Si una comprobación falla, goopdl muestra el motivo y termina con el código `1`.

### Límites de peticiones

Google Play a veces responde a las peticiones de entrega con HTTP 429. goopdl
reintenta la petición de entrega hasta dos veces, esperando entre intentos. No
reintenta las descargas de archivos. Si el bloqueo continúa, termina con el
código `5`. Espera un rato, o cambia de cuenta, de red o de dispensador.

---

## `fast-download`

Descarga la última versión con un solo token: uno válido del pool para ese
`--country` si existe, o uno nuevo si no. A diferencia de `download`, no llena
el pool de 5 tokens ni prueba varios perfiles de dispositivo. Úsalo en
crawlers y otros usos de alto volumen, donde llamar al dispensador en cada
petición acaba bloqueando tu IP.

```bash
goopdl fast-download com.whatsapp -o ./apks --splits
```

| Opción | Corta | Por defecto | Descripción |
| ------ | ----- | ----------- | ----------- |
| `--output` | `-o` | `.` | Carpeta de salida |
| `--arch` | `-a` | `arm64` | Tipo de dispositivo (solo uno) |
| `--splits` / `--no-splits` | | `--no-splits` | Incluye los split APKs |
| `--extras` / `--no-extras` | | `--no-extras` | Incluye los archivos OBB y los asset packs |
| `--profile` | | aleatorio | Perfil de dispositivo. Por defecto, uno al azar entre los dos más recientes |

También acepta `--country`, `--proxy` y `--dispenser`. `--proxy` solo se usa
en las llamadas a Google Play; las peticiones al dispensador rotan de región
por su cuenta.

---

## `latest`

Encuentra la versión más reciente disponible preguntando a Google Play con
varios GSF IDs y quedándose con el código de versión más alto. En [encontrar la
última versión](how-it-works.md#encontrar-la-última-versión) se explica por qué
no basta con un solo dispositivo.

```bash
goopdl latest com.instagram.android
goopdl latest com.instagram.android --stable 5
goopdl latest com.instagram.android -s 4 -c US -p socks5://host:1080
```

| Opción | Corta | Por defecto | Descripción |
| ------ | ----- | ----------- | ----------- |
| `--stable` | `-s` | `3` | Para cuando el código de versión más alto no cambia durante este número de sondeos seguidos |
| `--profile` | | el mejor clasificado | Perfil de dispositivo para todos los sondeos. Por defecto, el de SDK y versión de Play Store más altos |

También acepta las [opciones comunes](#opciones-comunes). `--proxy` se usa
tanto en las llamadas al dispensador como en las de Google Play.

La salida es una tabla con el prefijo del GSF ID, el nombre y el código de
versión de cada sondeo. La versión más alta aparece resaltada y se imprime al
final.

---

## `inspect-delivery`

Pregunta a Google Play qué entregaría para un código de versión exacto, sin
descargar ningún APK. Úsalo para comprobar que un perfil de dispositivo recibe
la ABI que quieres antes de empezar una descarga grande.

```bash
goopdl inspect-delivery com.whatsapp --arch armv7 --version 231205015 --json
goopdl inspect-delivery com.whatsapp --version 231205015 -o delivery.json
```

```json
{
  "architecture": "armv7",
  "package": "com.whatsapp",
  "splits": ["base", "config.armeabi_v7a", "config.en"],
  "version": {"code": 231205015, "name": "2.23.26.15"}
}
```

| Opción | Corta | Por defecto | Descripción |
| ------ | ----- | ----------- | ----------- |
| `--version` | `-v` | obligatorio | Código de versión exacto |
| `--json` | | no | Imprime JSON |
| `--output` | `-o` | — | Escribe el JSON en un archivo en lugar de la salida estándar |

También acepta las [opciones comunes](#opciones-comunes). El comando se
autentica y "compra" la versión (ver [cómo funciona](how-it-works.md#el-flujo-de-descarga)),
pero no descarga nada.

---

## `info`

Muestra el nombre, la versión, el desarrollador, la valoración, el número de
descargas y la URL de Play Store de una app.

```bash
goopdl info com.whatsapp
goopdl info com.whatsapp -c IN -p socks5://host:1080
goopdl info com.whatsapp --raw    # respuesta protobuf completa decodificada, en JSON
```

| Opción | Corta | Por defecto | Descripción |
| ------ | ----- | ----------- | ----------- |
| `--raw` | `-r` | no | Imprime la respuesta completa decodificada en JSON |

También acepta las [opciones comunes](#opciones-comunes).

---

## `search`

```bash
goopdl search "whatsapp"
goopdl search "file manager" --limit 5
goopdl search "whatsapp" --country IN
```

| Opción | Corta | Por defecto | Descripción |
| ------ | ----- | ----------- | ----------- |
| `--limit` | `-l` | `10` | Número máximo de resultados |

También acepta las [opciones comunes](#opciones-comunes).

---

## `list-splits`

Lista los split APKs de una app (configuraciones de ABI, densidad e idioma)
sin descargar nada.

```bash
goopdl list-splits com.whatsapp
```

Acepta `--arch` y `--dispenser`.

---

## `profiles`

Lista los perfiles de dispositivo incluidos. Pasa un valor de la columna
**Key** a `--profile` en cualquier comando.

```bash
goopdl profiles              # todos los perfiles
goopdl profiles --arch arm64 # una arquitectura
```

`--arch` acepta `arm64`, `armv7`, `x86`, `x86_64`, `tv` o `all` (por defecto).

---

## `auth`

Gestiona las credenciales. `goopdl auth` sin más muestra la lista de
subcomandos. Los dos modos de autenticación se explican en
[autenticación](authentication.md).

### `auth login`

Inicia sesión en una cuenta de Google desde un navegador y la guarda para el
[modo cuenta de Google](authentication.md#cuenta-de-google). Los comandos
siguientes la usan hasta que ejecutes `auth logout`. `GOOPDL_ACCOUNT_EMAIL` y
`GOOPDL_AAS_TOKEN` tienen prioridad sobre ella. El token AAS va al
[almacén de claves del sistema](authentication.md#dónde-se-guarda-la-cuenta),
o a `account.json` si no hay. La última línea dice dónde.

```bash
goopdl auth login                     # ventana de navegador aislada, recomendado
goopdl auth login --manual            # tu navegador habitual, pegas el oauth_token
goopdl auth login --insecure-storage  # deja el token en account.json
TOKEN=$(goopdl auth login --print)    # imprime el token AAS, no guarda nada
```

| Opción | Por defecto | Descripción |
| ------ | ----------- | ----------- |
| `--print` | no | Imprime solo el token AAS por stdout en lugar de guardar la cuenta. Los mensajes van a stderr |
| `--manual` | no | Abre Google EmbeddedSetup en tu navegador habitual y pide el email y la cookie `oauth_token` |
| `--insecure-storage` | no | Guarda el token en `account.json` sin probar el almacén de claves del sistema |

En [conseguir un token AAS](authentication.md#conseguir-un-token-aas) se
explican los dos modos paso a paso. `goopdl aastoken` sigue funcionando como
alias oculto y obsoleto de `auth login --print`; solo acepta `--browser` y
`--oauth` (igual que `--manual`).

### `auth logout`

Borra la cuenta guardada, su token en el almacén de claves del sistema, todos
los tokens en caché y los
[registros de dispositivo del modo cuenta](authentication.md#qué-guarda-el-modo-cuenta).
Si no puede quitar la entrada del almacén, avisa y borra el resto igualmente.
Si `GOOPDL_ACCOUNT_EMAIL` o `GOOPDL_AAS_TOKEN` siguen definidas, avisa de que
goopdl las sigue usando. Para conservar la cuenta y borrar solo la caché, usa
[`auth clear-cache`](#auth-clear-cache).

```bash
goopdl auth logout
```

### `auth clear-cache`

Borra todos los tokens en caché y los
[registros de dispositivo del modo cuenta](authentication.md#qué-guarda-el-modo-cuenta).
La cuenta guardada se queda: no toca `account.json` ni el almacén de claves
del sistema. Úsalo cuando Google deje de aceptar un dispositivo registrado: el
siguiente comando registra uno nuevo sin volver a iniciar sesión. No tiene
opciones.

```bash
goopdl auth clear-cache
```

### `auth status`

Muestra el modo activo (`environment`, `saved account` o `anonymous`), el
email de la cuenta, dónde está el token guardado (`system keyring` y su
backend, o `file`) y un resumen de la caché. Nunca imprime un token. Si el
token está en el almacén, comprueba que siga ahí; si no, el modo pasa a
`saved account (unusable)` y el motivo va a stderr. Con las variables de la
cuenta definidas no abre el almacén. Termina con `2` si solo está definida una
de las variables de la cuenta.

```bash
goopdl auth status
```

### `auth token`

Consigue un token anónimo del dispensador y lo guarda en caché. Con una cuenta
de Google inicia sesión en Google y solo guarda el registro del dispositivo.

```bash
goopdl auth token
goopdl auth token --arch armv7
goopdl auth token --country IN
goopdl auth token --profile "Galaxy S25 Ultra"
```

| Opción | Corta | Por defecto | Descripción |
| ------ | ----- | ----------- | ----------- |
| `--country` | `-c` | — | Registra el dispositivo con el MCC/MNC de ese país, para que las llamadas posteriores con el mismo `--country` vean su catálogo sin proxy |

También acepta las [opciones comunes](#opciones-comunes).

---

## Códigos de salida

| Código | Significado |
| ------ | ----------- |
| `0` | Éxito |
| `1` | Otro error |
| `2` | Entrada o configuración no válida, como definir solo una de las variables de la cuenta de Google o una cuenta guardada inutilizable |
| `3` | Falló la autenticación: el dispensador rechazó todos los perfiles de dispositivo, está caído o no responde, o falló el inicio de sesión con la cuenta de Google. `download` también termina con `3` cuando el pool de tokens está ocupado |
| `4` | Versión no disponible (HTTP 404 o confirmada como no disponible) |
| `5` | Límite de peticiones (HTTP 429): de Google Play tras los reintentos en `download`, `fast-download`, `latest` e `inspect-delivery`, o del dispensador de tokens cuando no queda ningún token válido en el pool (tras un 429 del dispensador, las siguientes peticiones de tokens del mismo comando fallan al momento, [sin volver a preguntarle](how-it-works.md#pool-de-tokens)), o del inicio de sesión con la cuenta de Google |

Si el dispensador responde pero rechaza todos los perfiles, goopdl muestra
`Authentication failed — all profiles rejected (last dispenser response: …)`
con el último estado HTTP, o `no authToken`. Si el dispensador está caído,
muestra en su lugar `… It may be down; retry later.`

Si otro proceso de goopdl mantiene el bloqueo del pool de tokens más de 180
segundos, goopdl muestra
`Token pool is busy (another goopdl process holds the lock); retry later.`
y termina con `3` en `download`, o con `1` en `latest` y `fast-download`.
Vuelve a intentarlo cuando acabe el otro proceso.
