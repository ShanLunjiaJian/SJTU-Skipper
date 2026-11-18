{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    treefmt-nix = {
      url = "github:numtide/treefmt-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    { nixpkgs, treefmt-nix, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      formatter = treefmt-nix.lib.mkWrapper pkgs {
        projectRootFile = "flake.nix";
        programs = {
          ruff-format.enable = true;
          nixfmt.enable = true;
          taplo.enable = true;
          shfmt.enable = true;
        };
        settings.global.excludes = [
          ".git/**"
          ".venv/**"
          "data/**"
          "node_modules/**"
          "bun.lock"
          "src/ui/static/vendor/**"
          "**/*.min.js"
          "**/*.min.css"
          "**/__pycache__/**"
          ".pytest_cache/**"
          ".ruff_cache/**"
          "**/*.egg-info/**"
          "uv.lock"
          "flake.lock"
          "*.woff"
          "*.woff2"
          "*.ttf"
          "*.otf"
          "*.mp4"
          "*.wav"
          "*.mp3"
          "*.jpg"
          "*.jpeg"
          "*.png"
          "*.gif"
          "*.ico"
        ];
        settings.formatter.biome = {
          command = "${pkgs.biome}/bin/biome";
          options = [
            "format"
            "--write"
            "--no-errors-on-unmatched"
          ];
          includes = [
            "*.js"
            "*.jsx"
            "*.mjs"
            "*.cjs"
            "*.ts"
            "*.tsx"
            "*.mts"
            "*.cts"
            "*.css"
            "*.scss"
            "*.html"
            "*.htm"
            "*.json"
            "*.jsonc"
            "*.md"
            "*.yaml"
            "*.yml"
          ];
        };
        settings.formatter.shfmt.options = [
          "-i"
          "2"
        ];
        settings.formatter.taplo.includes = [
          "*.toml"
          "*.toml.example"
        ];
      };
    in
    {
      formatter.${system} = formatter;
      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          python313
          uv
          ffmpeg
          pkg-config
          ty
          typescript
          ruff
          bun
          biome
          formatter
        ];
        UV_PYTHON = "${pkgs.python313}/bin/python3";
        UV_PYTHON_DOWNLOADS = "never";
        LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath [
          pkgs.stdenv.cc.cc.lib
          pkgs.zlib
        ];
      };
    };
}
