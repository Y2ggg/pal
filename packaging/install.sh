#!/bin/sh
set -eu
bundle=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir=${PAL_INSTALL_DIR:-"$HOME/.local/share/pal-program"}
bin_dir=${PAL_BIN_DIR:-"$HOME/.local/bin"}
case "$install_dir" in /*) ;; *) echo 'PAL_INSTALL_DIR 必须为绝对路径' >&2; exit 1;; esac
case "$bin_dir" in /*) ;; *) echo 'PAL_BIN_DIR 必须为绝对路径' >&2; exit 1;; esac
if [ -L "$install_dir" ] || { [ -e "$install_dir" ] && [ ! -f "$install_dir/.pal-install" ]; }; then
  echo '安装目录已存在且不属于此安装器；请另选目录。' >&2; exit 1
fi
if [ -e "$bin_dir/pal" ] || [ -L "$bin_dir/pal" ]; then
  if [ "$(readlink "$bin_dir/pal" 2>/dev/null || true)" != "$install_dir/runtime/pal" ]; then
    echo '目标命令已被其他安装占用；请先通过原安装工具卸载 PAL。' >&2; exit 1
  fi
fi
if [ -x "$install_dir/runtime/pal" ]; then
  stop_output=$("$install_dir/runtime/pal" web stop 2>&1) || {
    case "$stop_output" in *'没有可关闭的 PAL Web 服务记录'*) ;; *) printf '%s\n' "$stop_output" >&2; exit 1;; esac
  }
fi
mkdir -p -- "$(dirname -- "$install_dir")" "$bin_dir"
stage=$(mktemp -d "${install_dir}.new.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT HUP INT TERM
cp -R "$bundle/runtime" "$bundle/licenses" "$stage/"
cp "$bundle/LICENSE" "$bundle/THIRD-PARTY-NOTICES.md" "$bundle/BUILD.json" "$bundle/uninstall.sh" "$stage/"
printf '%s\n' "$bin_dir" > "$stage/.pal-install"
"$stage/runtime/pal" --version
backup="${stage}.previous"
if [ -d "$install_dir" ]; then mv "$install_dir" "$backup"; fi
if ! mv "$stage" "$install_dir"; then
  if [ -d "$backup" ]; then mv "$backup" "$install_dir"; fi
  exit 1
fi
ln -sfn "$install_dir/runtime/pal" "$bin_dir/pal"
if [ -d "$backup" ]; then rm -rf -- "$backup"; fi
printf 'PAL 已安装。请将 %s 加入 PATH，然后运行 pal quickstart。\n' "$bin_dir"
