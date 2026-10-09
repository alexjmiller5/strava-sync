{
  description = "strava-sync: import Strava bulk exports into a soma hub";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, uv2nix, pyproject-nix, pyproject-build-systems }:
    let
      inherit (nixpkgs) lib;
      systems = [ "aarch64-darwin" "x86_64-darwin" "aarch64-linux" "x86_64-linux" ];
      forAllSystems = f: lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});

      # The uv workspace (pyproject.toml + uv.lock) as an overlay of locked wheels.
      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

      mkPythonSet = pkgs:
        (pkgs.callPackage pyproject-nix.build.packages { python = pkgs.python313; }).overrideScope (
          lib.composeManyExtensions [ pyproject-build-systems.overlays.default overlay ]
        );
    in
    {
      # `nix run github:alexjmiller5/strava-sync -- import <zip>`: no checkout needed.
      packages = forAllSystems (pkgs: {
        default = (mkPythonSet pkgs).mkVirtualEnv "strava-sync-env" workspace.deps.default;
      });

      apps = forAllSystems (pkgs: {
        default = {
          type = "app";
          program = "${self.packages.${pkgs.stdenv.hostPlatform.system}.default}/bin/strava-sync";
        };
      });

      checks = forAllSystems (pkgs: {
        cli = pkgs.runCommand "strava-sync-cli" { } ''
          ${self.packages.${pkgs.stdenv.hostPlatform.system}.default}/bin/strava-sync import --help
          touch $out
        '';
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell { packages = [ pkgs.uv pkgs.ruff pkgs.just pkgs.python313 ]; };
      });

      formatter = forAllSystems (pkgs: pkgs.nixpkgs-fmt);
    };
}
