#!/bin/sh
set -eu
bundle=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install_dir=${PAL_INSTALL_DIR:-"$HOME/.local/share/pal-program"}
bin_dir=${PAL_BIN_DIR:-"$HOME/.local/bin"}
case "$install_dir" in /*) ;; *) echo 'PAL_INSTALL_DIR 必须为绝对路径' >&2; exit 1;; esac
case "$bin_dir" in /*) ;; *) echo 'PAL_BIN_DIR 必须为绝对路径' >&2; exit 1;; esac
mkdir -p -- "$(dirname -- "$install_dir")" "$bin_dir"
install_dir=$(CDPATH= cd -- "$(dirname -- "$install_dir")" && printf '%s/%s\n' "$PWD" "$(basename -- "$install_dir")")
bin_dir=$(CDPATH= cd -- "$bin_dir" && pwd)
if [ "$(printf '%s\n' "$install_dir" | wc -l | tr -d ' ')" -ne 1 ] ||
   [ "$(printf '%s\n' "$bin_dir" | wc -l | tr -d ' ')" -ne 1 ]; then
  echo '安装目录和命令目录不能包含换行符。' >&2; exit 1
fi
validate_install() {
  marker=$1
  expected_install=$2
  expected_bin=$3
  [ -f "$marker" ] && [ "$(wc -l < "$marker" | tr -d ' ')" -eq 3 ] &&
    [ "$(sed -n '1p' "$marker")" = 'PAL-INSTALL-V1' ] &&
    [ "$(sed -n '2p' "$marker")" = "$expected_install" ] &&
    [ "$(sed -n '3p' "$marker")" = "$expected_bin" ] &&
    [ -x "$expected_install/runtime/pal" ] &&
    [ -f "$expected_install/BUILD.json" ] &&
    [ -f "$expected_install/uninstall.sh" ]
}
if [ -L "$install_dir" ]; then
  echo '安装目录不能是链接。' >&2; exit 1
fi
if [ -e "$install_dir" ] && ! validate_install "$install_dir/.pal-install" "$install_dir" "$(sed -n '3p' "$install_dir/.pal-install" 2>/dev/null || true)"; then
  echo '安装目录不属于此版本安装器或安装记录已损坏；请另选目录。' >&2; exit 1
fi
if [ -d "$install_dir" ]; then
  old_bin_dir=$(sed -n '3p' "$install_dir/.pal-install")
  if [ "$old_bin_dir" != "$bin_dir" ]; then
    echo "命令目录与原安装不一致；请先从原安装目录运行 uninstall.sh，再重新安装。" >&2; exit 1
  fi
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
stage=$(mktemp -d "${install_dir}.new.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT HUP INT TERM
cp -R "$bundle/runtime" "$bundle/licenses" "$stage/"
cp "$bundle/LICENSE" "$bundle/THIRD-PARTY-NOTICES.md" "$bundle/BUILD.json" "$bundle/uninstall.sh" "$stage/"
printf 'PAL-INSTALL-V1\n%s\n%s\n' "$install_dir" "$bin_dir" > "$stage/.pal-install"
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
