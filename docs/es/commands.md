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
| [`auth`](#auth) | Consigue y guarda un token anónimo |
| [`aastoken`](#aastoken) | Imprime un token AAS de una cuenta de Google |

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

Consigue un token anónimo del dispensador y lo guarda en caché. Los dos modos
de autenticación se explican en [autenticación](authentication.md).

```bash
goopdl auth
goopdl auth --arch armv7
goopdl auth --country IN
goopdl auth --profile "Galaxy S25 Ultra"
goopdl auth --clear
```

| Opción | Corta | Por defecto | Descripción |
| ------ | ----- | ----------- | ----------- |
| `--clear` | | no | Borra todos los tokens en caché |
| `--country` | `-c` | — | Registra el dispositivo con el MCC/MNC de ese país, para que las llamadas posteriores con el mismo `--country` vean su catálogo sin proxy |

También acepta las [opciones comunes](#opciones-comunes).

---

## `aastoken`

Imprime un token AAS de una cuenta de Google para el
[modo cuenta de Google](authentication.md#cuenta-de-google). No guarda
credenciales ni tokens.

```bash
goopdl aastoken --browser                 # recomendado
goopdl aastoken --oauth                   # pegas tú el oauth_token
goopdl aastoken [EMAIL] [PASSWORD]        # modo contraseña, suele fallar
```

| Opción | Descripción |
| ------ | ----------- |
| `--browser` | Abre una ventana de navegador aislada y captura el inicio de sesión automáticamente |
| `--oauth` | Pide un `oauth_token` de un solo uso en lugar de una contraseña |

En [conseguir un token AAS](authentication.md#conseguir-un-token-aas) se
explica cada modo paso a paso.

---

## Códigos de salida

| Código | Significado |
| ------ | ----------- |
| `0` | Éxito |
| `1` | Otro error |
| `2` | Entrada o configuración no válida, como definir solo una de las variables de la cuenta de Google |
| `3` | Falló la autenticación |
| `4` | Versión no disponible (HTTP 404 o confirmada como no disponible) |
| `5` | Google Play siguió limitando las peticiones tras los reintentos |
