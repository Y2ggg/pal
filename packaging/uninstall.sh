#!/bin/sh
set -eu
install_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -f "$install_dir/.pal-install" ]; then
  echo '请运行安装目录内的 uninstall.sh；此目录不是已安装的 PAL。' >&2; exit 1
fi
bin_dir=$(cat "$install_dir/.pal-install")
stop_output=$("$install_dir/runtime/pal" web stop 2>&1) || {
    case "$stop_output" in *'没有可关闭的 PAL Web 服务记录'*) ;; *) printf '%s\n' "$stop_output" >&2; exit 1;; esac
  }
if [ "$(readlink "$bin_dir/pal" 2>/dev/null || true)" = "$install_dir/runtime/pal" ]; then
  rm "$bin_dir/pal"
fi
rm -rf -- "$install_dir"
echo 'PAL 程序已卸载。库、配置与 CLI 插件保留；插件可通过 CLI 官方命令卸载。'
