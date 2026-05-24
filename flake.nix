{
  inputs = {
    nixpkgs = {
      url = "github:nixos/nixpkgs/nixos-unstable";
    };
    flake-utils = {
      url = "github:numtide/flake-utils";
    };
  };

  outputs =
    {
      nixpkgs,
      flake-utils,
      ...
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs {
          inherit system;
        };

        pythonLibs =
          ps: with ps; [
            pillow
            imagehash
          ];

        pythonEnv = pkgs.python3.withPackages pythonLibs;
      in
      {
        packages = {
          default = pkgs.writers.writePython3Bin "auto-screenshotter" {
            libraries = pythonLibs pkgs.python3Packages;
            doCheck = false;
          } (builtins.readFile ./capture-windows.py);
        };

        devShells = {
          default = pkgs.mkShell {
            buildInputs = [ pythonEnv ];
          };
        };
      }
    );
}
