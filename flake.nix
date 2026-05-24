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

        pythonEnv = pkgs.python312.withPackages (
          ps: with ps; [
            pillow
            imagehash
          ]
        );
      in
      {
        devShells = {
          default = pkgs.mkShell {
            buildInputs = [ pythonEnv ];
          };
        };
      }
    );
}
