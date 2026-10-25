<p align="center">
  <img src="https://raw.githubusercontent.com/Villoh/goopdl/main/docs/assets/logo.svg" alt="goopdl" width="720">
</p>

<p align="center">
  Descarga APKs de Google Play desde la terminal.<br>
  Sin cuenta de Google. Con split APKs, archivos OBB y asset packs.
</p>

<p align="center">
  <a href="https://pypi.org/project/goopdl/"><img src="https://img.shields.io/pypi/v/goopdl?style=flat-square&logo=pypi&logoColor=white&color=00b0ff" alt="Versión en PyPI"></a>
  <a href="https://pypi.org/project/goopdl/"><img src="https://img.shields.io/badge/python-3.10%2B-7c4dff?style=flat-square&logo=python&logoColor=white" alt="Python 3.10+"></a>
  <a href="https://github.com/Villoh/goopdl/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/Villoh/goopdl/ci.yml?branch=main&style=flat-square&logo=github&label=CI" alt="Estado de CI"></a>
  <a href="https://pypistats.org/packages/goopdl"><img src="https://img.shields.io/pypi/dm/goopdl?style=flat-square&color=00e5a0" alt="Descargas mensuales"></a>
  <a href="https://github.com/Villoh/goopdl/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-00e5a0?style=flat-square" alt="Licencia MIT"></a>
</p>

<p align="center">
  <a href="https://github.com/Villoh/goopdl/blob/main/README.md">English</a> · <b>Español</b>
</p>

---

## Características

- **Sin cuenta.** Los tokens anónimos salen del dispensador público de Aurora
  Store. Si lo prefieres, puedes
  [usar una cuenta de Google](https://github.com/Villoh/goopdl/blob/main/docs/es/authentication.md#cuenta-de-google).
- **La app completa.** APK base, split APKs, archivos de expansión OBB y
  paquetes de Play Asset Delivery.
- **Cualquier versión.** Descarga por código o nombre de versión, o averigua
  con `goopdl latest` cuál es la más reciente que Google está desplegando.
- **Cualquier dispositivo.** ARM64, ARMv7, x86, x86_64 y Android TV, con
  perfiles de dispositivo incluidos que rotan solos.
- **Builds regionales.** Combina `--country` y `--proxy` para obtener la
  variante de una región concreta.
- **Descargas verificadas.** Si quieres, comprueba cada archivo contra los
  hashes de Google y genera un manifiesto de integridad.

## Instalación

```bash
uv tool install goopdl    # o: pipx install goopdl
```

O ejecútalo una vez sin instalar: `uvx goopdl --help`.
[Más opciones de instalación](https://github.com/Villoh/goopdl/blob/main/docs/es/installation.md),
como fijar versiones o compilar desde el código fuente.

## Primeros pasos

```bash
# Descarga el APK base de la última versión
goopdl download com.whatsapp

# Descarga la app completa: base, split APKs, OBB y asset packs
goopdl download com.whatsapp --splits --extras -o ./apks
```

Tras descargar una app con splits, goopdl muestra el comando
`adb install-multiple` exacto para instalarla en un dispositivo conectado.

goopdl consigue un token anónimo la primera vez que lo necesita. No hace falta
ejecutar `goopdl auth`.

Más ejemplos:

```bash
goopdl search "file manager"                     # busca el nombre del paquete
goopdl info com.whatsapp                         # detalles de la app
goopdl latest com.instagram.android              # versión más reciente en despliegue
goopdl download com.whatsapp -v 231205015        # una versión concreta
goopdl download com.whatsapp -a armv7            # build para ARM de 32 bits
```

## Documentación

| Guía | Contenido |
| ---- | --------- |
| [Instalación](https://github.com/Villoh/goopdl/blob/main/docs/es/installation.md) | uv, pipx, pip, Docker y compilación desde el código fuente |
| [Referencia de comandos](https://github.com/Villoh/goopdl/blob/main/docs/es/commands.md) | Todos los comandos y opciones, archivos generados y códigos de salida |
| [Autenticación](https://github.com/Villoh/goopdl/blob/main/docs/es/authentication.md) | Tokens anónimos, dispensadores propios, modo cuenta de Google y tokens AAS |
| [Cómo funciona](https://github.com/Villoh/goopdl/blob/main/docs/es/how-it-works.md) | Flujo de descarga, pool de tokens, despliegues por fases y perfiles de dispositivo |

## Créditos

goopdl es un hard fork independiente de
[appknox/gplaydl](https://github.com/appknox/gplaydl), que a su vez es un fork
de [rehmatworks/gplaydl](https://github.com/rehmatworks/gplaydl). La
autenticación anónima usa el dispensador de tokens de
[Aurora Store](https://auroraoss.com/).

## Licencia

[MIT](https://github.com/Villoh/goopdl/blob/main/LICENSE). Se conserva el
copyright original.
