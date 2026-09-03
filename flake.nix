{
  description = "MicroscopeControl - Olympus BH3 rig control";

  inputs = {
    # Pinned to 25.05 to match nix-config, so the app and the system it runs on
    # share one Qt and one libgphoto2 rather than dragging in a second closure.
    nixpkgs.url = "github:nixos/nixpkgs/25.05";
  };

  outputs =
    { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: nixpkgs.legacyPackages.${system};
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        rec {
          microscope-control = pkgs.callPackage ./nix/package.nix {
            src = pkgs.lib.cleanSource ./.;
          };
          default = microscope-control;
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.mkShell {
            # The app is run from the source tree during development
            # (`python -m microscope_control`), so the shell needs the runtime
            # deps rather than the built package.
            packages = [
              (pkgs.python3.withPackages (ps: [
                ps.pyside6
                ps.numpy
                ps.gphoto2
                ps.pytest
              ]))
              # The CLI is the reference implementation for anything the app
              # cannot do yet: `gphoto2 --list-config` is how you find the
              # widget names this body actually exposes.
              pkgs.gphoto2
              pkgs.libgphoto2
            ];

            shellHook = ''
              export PYTHONPATH="$PWD/src:$PYTHONPATH"
              echo "MicroscopeControl dev shell - run: python -m microscope_control"
            '';
          };
        }
      );

      formatter = forAllSystems (system: (pkgsFor system).nixpkgs-fmt);
    };
}
