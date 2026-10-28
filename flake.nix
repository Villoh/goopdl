{
  description = "Download APKs from Google Play Store with anonymous authentication";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      forAllSystems =
        f:
        nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ] (
          system:
          f (
            import nixpkgs {
              inherit system;
              overlays = [ self.overlays.default ];
            }
          )
        );
    in
    {
      # Adds `goopdl` to every Python package set, so other flakes can use it as a library.
      overlays.default = final: prev: {
        pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [
          (
            pyFinal: _: {
              goopdl = pyFinal.buildPythonPackage {
                pname = "goopdl";
                # One source of truth for the version: pyproject.toml.
                version = (builtins.fromTOML (builtins.readFile ./pyproject.toml)).project.version;
                pyproject = true;
                src = ./.;
                build-system = [ pyFinal.setuptools ];
                dependencies = with pyFinal; [
                  typer
                  rich
                  httpx
                  websocket-client
                ];
                # Rich decides the sandbox is a terminal; the tests compare plain text.
                checkPhase = "TTY_COMPATIBLE=0 python -m unittest discover -s tests";
                pythonImportsCheck = [ "goopdl" ];
                meta.mainProgram = "goopdl";
              };
            }
          )
        ];
      };

      packages = forAllSystems (pkgs: rec {
        goopdl = pkgs.python3Packages.toPythonApplication pkgs.python3Packages.goopdl;
        default = goopdl;
      });
    };
}
