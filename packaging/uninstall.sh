#!/bin/sh
set -eu
install_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
marker="$install_dir/.pal-install"
if [ ! -f "$marker" ] || [ "$(wc -l < "$marker" | tr -d ' ')" -ne 3 ] ||
   [ "$(sed -n '1p' "$marker")" != 'PAL-INSTALL-V1' ] ||
   [ "$(sed -n '2p' "$marker")" != "$install_dir" ] ||
   [ ! -x "$install_dir/runtime/pal" ] || [ ! -f "$install_dir/BUILD.json" ] ||
   [ ! -f "$install_dir/uninstall.sh" ]; then
  echo '安装记录未知或已损坏，拒绝卸载；请核对安装目录。' >&2; exit 1
fi
bin_dir=$(sed -n '3p' "$marker")
case "$bin_dir" in /*) ;; *) echo '安装记录中的命令目录无效，拒绝卸载。' >&2; exit 1;; esac
stop_output=$("$install_dir/runtime/pal" web stop 2>&1) || {
    case "$stop_output" in *'没有可关闭的 PAL Web 服务记录'*) ;; *) printf '%s\n' "$stop_output" >&2; exit 1;; esac
  }
if [ "$(readlink "$bin_dir/pal" 2>/dev/null || true)" = "$install_dir/runtime/pal" ]; then
  rm "$bin_dir/pal"
fi
rm -rf -- "$install_dir"
echo 'PAL 程序已卸载。库、配置与 CLI 插件保留；插件可通过 CLI 官方命令卸载。'
