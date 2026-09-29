#!/bin/sh
set -eu
install_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -f "$install_dir/.pal-install" ]; then
  echo '请运行安装目录内的 uninstall.sh；此目录不是已安装的 PAL。' >&2; exit 1
fi
bin_dir=$(cat "$install_dir/.pal-install")
"$install_dir/runtime/pal" web stop
if [ "$(readlink "$bin_dir/pal" 2>/dev/null || true)" = "$install_dir/runtime/pal" ]; then
  rm "$bin_dir/pal"
fi
rm -rf -- "$install_dir"
echo 'PAL 程序已卸载。库、配置与 CLI 插件保留；插件可通过 CLI 官方命令卸载。'
