{
  description = "portcullis - per-app network gate: block / add latency to incoming and outgoing traffic, with auto-detected apps";

  inputs = {
    # Overridden by the parent flake's `inputs.nixpkgs.follows = "nixpkgs-unstable"`.
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      forAllSystems = nixpkgs.lib.genAttrs [ "x86_64-linux" "aarch64-linux" ];

      # python-netfilterqueue is not (reliably) in nixpkgs; build it from PyPI.
      mkNetfilterQueue = pkgs: pkgs.python3.pkgs.buildPythonPackage rec {
        pname = "netfilterqueue";
        version = "1.1.0";
        format = "setuptools";
        src = pkgs.python3.pkgs.fetchPypi {
          pname = "NetfilterQueue";
          inherit version;
          hash = "sha256-4w7/mZMlYX9UvZWz2NM1MhTMd7XjGI+Q7DpvYoiIjXI=";
        };
        # The sdist ships a pre-generated _impl.c that doesn't compile on Python 3.14;
        # delete it so Cython regenerates it from _impl.pyx.
        prePatch = ''
          rm -f netfilterqueue/_impl.c
        '';
        nativeBuildInputs = [ pkgs.python3.pkgs.setuptools pkgs.python3.pkgs.cython ];
        buildInputs = [ pkgs.libnetfilter_queue pkgs.libnfnetlink ];
        doCheck = false;
        pythonImportsCheck = [ "netfilterqueue" ];
      };

      mkPackage = pkgs:
        let
          python = pkgs.python3;
          netfilterqueue = mkNetfilterQueue pkgs;
        in
        python.pkgs.buildPythonApplication {
          pname = "portcullis";
          version = "0.1.0";
          format = "pyproject";
          src = ./.;

          nativeBuildInputs = [
            python.pkgs.setuptools
            pkgs.makeWrapper
            pkgs.qt6.wrapQtAppsHook
          ];

          buildInputs = [
            pkgs.qt6.qtbase
            pkgs.qt6.qtwayland
            pkgs.xcb-util-cursor
          ];

          propagatedBuildInputs = [
            python.pkgs.pyside6
            python.pkgs.maxminddb
            python.pkgs.tomli-w
            netfilterqueue
          ];

          pythonImportsCheck = [ "portcullis" ];

          # nft: the firewall rules; systemctl/systemd-run: `portcullis launch`.
          postFixup = ''
            wrapQtApp "$out/bin/portcullis"
            wrapProgram "$out/bin/portcullis" \
              --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.nftables pkgs.systemd pkgs.coreutils ]}

            mkdir -p "$out/share/applications"
            printf '%s\n' \
              '[Desktop Entry]' \
              'Type=Application' \
              'Name=Portcullis' \
              'Comment=Block or delay an app'"'"'s network traffic' \
              "Exec=$out/bin/portcullis gui" \
              'Icon=network-wired' \
              'Categories=Network;Utility;' \
              > "$out/share/applications/portcullis.desktop"
          '';

          doCheck = false;

          meta = {
            description = "Per-app network gate: block or add latency to incoming / outgoing traffic";
            homepage = "https://github.com/mr-tinkle-winkle/portcullis";
            mainProgram = "portcullis";
            platforms = pkgs.lib.platforms.linux;
          };
        };
    in
    {
      packages = forAllSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in { default = mkPackage pkgs; });

      devShells = forAllSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          default = pkgs.mkShell {
            packages = [
              (pkgs.python3.withPackages (ps: [
                ps.pyside6
                ps.maxminddb
                ps.tomli-w
                (mkNetfilterQueue pkgs)
                ps.pytest
                ps.setuptools
              ]))
              pkgs.qt6.qtbase
              pkgs.qt6.qtwayland
              pkgs.nftables
            ];
            shellHook = ''
              export QT_PLUGIN_PATH="${pkgs.qt6.qtbase}/lib/qt-6/plugins:${pkgs.qt6.qtwayland}/lib/qt-6/plugins''${QT_PLUGIN_PATH:+:$QT_PLUGIN_PATH}"
              echo "portcullis dev shell. Try: QT_QPA_PLATFORM=offscreen pytest -q"
            '';
          };
        });

      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.portcullis;
          pkg = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
          nft = "${pkgs.nftables}/bin/nft";
        in
        {
          options.services.portcullis = {
            enable = lib.mkEnableOption "portcullis (per-app network block / latency)";

            user = lib.mkOption {
              type = lib.types.str;
              description = ''
                User allowed to control portcullis (added to the `portcullis` group, which owns
                the control socket). Note: anyone in that group can change the firewall table
                `inet portcullis`.
              '';
            };
          };

          config = lib.mkIf cfg.enable {
            environment.systemPackages = [ pkg ];

            users.groups.portcullis = { };
            users.users.portcullis = {
              isSystemUser = true;
              group = "portcullis";
            };
            users.users.${cfg.user}.extraGroups = [ "portcullis" ];

            # The window lives in your session (tray icon + notifications for new connections).
            systemd.user.services.portcullis-gui = {
              description = "portcullis window / tray";
              wantedBy = [ "graphical-session.target" ];
              partOf = [ "graphical-session.target" ];
              after = [ "graphical-session.target" ];
              environment.PORTCULLIS_SOCKET = "/run/portcullis/control.sock";
              serviceConfig = {
                ExecStart = "${pkg}/bin/portcullis gui --hidden";
                Restart = "on-failure";
                RestartSec = 5;
              };
            };

            systemd.services.portcullis = {
              description = "portcullis: per-app network block / latency";
              wantedBy = [ "multi-user.target" ];
              after = [ "network.target" ];
              environment = {
                PORTCULLIS_STATE_DIR = "/var/lib/portcullis";
                PORTCULLIS_SOCKET = "/run/portcullis/control.sock";
              };
              serviceConfig = {
                ExecStart = "${pkg}/bin/portcullis daemon";
                ExecStopPost = "-${nft} delete table inet portcullis";
                User = "portcullis";
                Group = "portcullis";
                RuntimeDirectory = "portcullis";
                RuntimeDirectoryMode = "0750";
                StateDirectory = "portcullis";
                AmbientCapabilities = [ "CAP_NET_ADMIN" ];
                CapabilityBoundingSet = [ "CAP_NET_ADMIN" ];
                NoNewPrivileges = true;
                ProtectSystem = "strict";
                ProtectHome = true;
                PrivateTmp = true;
                RestrictAddressFamilies = [ "AF_UNIX" "AF_NETLINK" ];
                Restart = "on-failure";
                RestartSec = 3;
              };
            };
          };
        };
    };
}
