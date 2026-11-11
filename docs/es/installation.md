# Instalación

[English](../installation.md) · **Español**

## Binario precompilado (sin Python)

Linux y macOS (x86_64 y arm64):

```bash
curl -fsSL https://villoh.github.io/goopdl/install.sh | sh
```

Windows (x86_64 y arm64), en PowerShell:

```powershell
irm https://villoh.github.io/goopdl/install.ps1 | iex
```

El script descarga el binario de la release de GitHub, comprueba su SHA-256 y lo
copia a `~/.local/bin` (Linux/macOS) o `%LOCALAPPDATA%\goopdl\bin` (Windows).
Nunca modifica tu `PATH`: si hace falta, imprime el comando para añadir el
directorio. Define `GOOPDL_VERSION=2.0.1` para fijar una versión y
`GOOPDL_INSTALL_DIR` para cambiar el destino. Para actualizar, vuelve a ejecutarlo.

El resto de esta página da por hecho Python 3.10 o posterior.

## Como herramienta de línea de comandos

Instálalo en un entorno aislado para que `goopdl` quede en tu `PATH`:

```bash
uv tool install goopdl
# o
pipx install goopdl
```

Pruébalo una vez sin instalar nada:

```bash
uvx goopdl --help
```

## Con Nix

El repositorio es un flake:

```bash
nix run github:Villoh/goopdl -- --help
```

Para instalarlo de forma declarativa, añádelo como input y usa `inputs.goopdl.packages.${system}.default`.
`overlays.default` añade `goopdl` a todos los conjuntos de paquetes de Python, para flakes que lo usen como librería.

## Como librería

Instálalo en el entorno actual:

```bash
pip install goopdl
```

## Fijar una versión

En producción, fija una versión exacta. En un Dockerfile:

```dockerfile
RUN pip3 install goopdl==1.2.2
```

En `requirements.txt`:

```text
goopdl==1.2.2
```

Para probar cambios que aún no se han publicado, instala desde git:

```bash
pip install git+https://github.com/Villoh/goopdl.git
```

## Desde el código fuente

Necesitas [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/Villoh/goopdl.git
cd goopdl
uv sync
uv run goopdl --help
```

`uv sync` instala goopdl en `.venv`. Antepón `uv run` a los comandos, o activa
el entorno (`source .venv/bin/activate`) y llama a `goopdl` directamente.
También funciona como módulo:

```bash
uv run python -m goopdl download com.whatsapp
```

Ejecuta los tests:

```bash
uv run python -m unittest discover -s tests
```
