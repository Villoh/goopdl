# Instalación

[English](../installation.md) · **Español**

goopdl necesita Python 3.10 o posterior.

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
