# Cómo funciona

[English](../how-it-works.md) · **Español**

## El flujo de descarga

1. **Autenticar.** Consigue un token, ya sea anónimo desde el dispensador
   (rotando perfiles de dispositivo hasta que uno funcione) o desde tu cuenta
   de Google. Ver [autenticación](authentication.md).
2. **Obtener detalles.** Lee los metadatos de la app (versión, tamaño, splits)
   de la API protobuf de Google Play.
3. **Comprar.** "Compra" la app gratuita para obtener autorización de descarga.
4. **Obtener la entrega.** Pide las URLs de descarga del APK base, los split
   APKs, los archivos OBB y los asset packs.
5. **Descargar.** Descarga los archivos en paralelo desde la CDN de Google,
   con barras de progreso. Los paquetes de Play Asset Delivery se descomprimen
   (gzip) sobre la marcha.

## Pool de tokens

`download` y `latest` sacan los tokens de un pool de 5 tokens anónimos por
arquitectura, país y perfil fijado, guardado en
`~/.config/goopdl/token-pool-{arch}[-{country}][-profile-{key}].json`.

- Antes de cada llamada, goopdl cuenta los tokens sin caducar y pide solo los
  que faltan. Con el pool lleno no se hace ninguna llamada al dispensador.
- Los tokens caducan a los 50 minutos.
- Si Google rechaza un token a mitad de una petición (HTTP 401), goopdl lo
  sustituye.
- Los tokens se reparten por turnos (round-robin) y la posición se conserva
  entre ejecuciones.
- Varios procesos de goopdl pueden compartir un pool: las actualizaciones se
  hacen con bloqueo y se escriben de forma atómica.
- Si el dispensador responde HTTP 429, goopdl deja de probar otros perfiles en
  lugar de insistir.

El pool mantiene a goopdl por debajo del límite de Aurora, de unas 20
peticiones por hora. En el [modo cuenta de Google](authentication.md#cuenta-de-google)
no hay pool.

## Encontrar la última versión

Google publica las actualizaciones por fases, por grupos (cohortes) de
dispositivos. La cohorte depende del GSF ID (Google Services Framework ID) del
dispositivo, que se asigna al emitir el token. Dos dispositivos pueden ver el
mismo día versiones distintas de la misma app, por ejemplo la 434 y la 435.

Por eso `latest` pregunta con varios GSF IDs y se queda con el código de
versión más alto:

- Cada sondeo usa un GSF ID distinto, así que puede caer en otra cohorte.
- El sondeo para cuando el código de versión más alto se mantiene durante
  `--stable` sondeos seguidos.
- El perfil de dispositivo fija el nivel de SDK que ve Google. Los perfiles con
  SDK más reciente suelen recibir antes las versiones nuevas, y por eso
  `latest` usa por defecto el perfil mejor clasificado.

`download --version 434.0.0.44.74` funciona igual: sondea GSF IDs hasta que uno
ve la versión pedida.

## Perfiles de dispositivo

goopdl incluye perfiles de dispositivo como archivos `.properties` en
[`goopdl/profiles/`](../../goopdl/profiles). Describen un dispositivo real
(modelo, versión de Android, ABI, versión de Play Store) para que Google sirva
la build correcta.

Durante la autenticación anónima, goopdl va rotando perfiles hasta que el
dispensador acepta uno. Lista los perfiles con `goopdl profiles` y fija uno
con `--profile`.

## Arquitecturas

| Opción | ABI | Dispositivos |
| ------ | --- | ------------ |
| `arm64` (por defecto) | arm64-v8a | Móviles de 2017 en adelante, aproximadamente |
| `armv7` | armeabi-v7a | Móviles antiguos de 32 bits |
| `x86` | x86 | Emuladores y dispositivos Intel/AMD de 32 bits |
| `x86_64` | x86_64 | Emuladores y dispositivos Intel/AMD de 64 bits |
| `tv` | arm64-v8a / armeabi-v7a | Android TV y dispositivos de streaming |

`download` acepta varias separadas por comas (`-a arm64,armv7`) y las descarga
una tras otra.
