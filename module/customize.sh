# shellcheck shell=busybox

cd "${MODPATH}" || abort "! Failed to change directory to ${MODPATH}"

# Avoid redefining TMPDIR during module installation to prevent potential problems
NO_TMPDIR=true . ./env.sh

# PyDroid installation paths to handle migration
PYDROID_HOME="/data/local/pydroid"
PYDROID_MODULE="/data/adb/modules/pydroid"

MIGRATION_SCRIPT="
from pathlib import Path
from shutil import move, copy
pydroid = Path('${PYDROID_HOME}')
py2droid = Path('${HOME}')
for e in pydroid.iterdir():
    dst = py2droid / e.name
    if dst.exists():
      raise SystemExit(
          'Migration aborted: Destination already exists. Are both PyDroid and Py2Droid installed?',
      )
    move(e, dst)
copy('env.sh', pydroid)
"

install() {
  local python_tarball
  local python_arch

  python_tarball="$(find . -maxdepth 1 -type f -iname "python*.tar.xz" | head -n 1)"

  if [[ -z ${python_tarball} ]]; then
    abort "! No Python distribution tarball found"
  fi

  python_arch="$(basename -s .tar.xz "${python_tarball}" | cut -d '-' -f 3)"

  if [[ ${python_arch} != "${ARCH}" ]]; then
    abort "! This module for $python_arch architecture, but your device is $ARCH"
  fi

  # Check and perform migration from PyDroid
  if [[ -d ${PYDROID_HOME} ]]; then
    ui_print "- Detected existing PyDroid installation, migrating data..."

    mkdir -p "${HOME}"

    if ! python3 -c "${MIGRATION_SCRIPT}" >&2; then
      ui_print "! Migration from PyDroid failed"

      if [[ -d ${PYDROID_MODULE} ]]; then
        ui_print "! Disabling PyDroid module to prevent conflicts"
        touch "${PYDROID_MODULE}/disable"
      else
        ui_print "! Please manually remove ${PYDROID_HOME} if it is no longer needed"
      fi
    else
      touch "${PYDROID_MODULE}/remove"
    fi
  fi

  ui_print "- Installing into ${HOME}..."

  for directory in usr .tmp .cache .config .local/share .local/state .local/bin; do
    mkdir -p "${HOME}/${directory}"
  done

  if ! tar -xf "${python_tarball}" -C "${HOME}/usr" --strip-components 1; then
    abort "! Failed to unpack Python tarball"
  fi

  mv -f env.sh "${HOME}"

  # Create wrappers for python executables
  python3 system/bin/py2droid-update-bin

  echo "rm -rf \"${HOME}\"" >uninstall.sh
}

set_permissions() {
  set_perm_recursive "system/bin" 0 0 0755 0755
  set_perm_recursive "${HOME}/usr/bin" 0 0 0755 0755
  set_perm_recursive "${HOME}/usr/lib" 0 0 0755 0644
  set_perm_recursive "${HOME}/usr/include" 0 0 0755 0644
}

cleanup() {
  rm -rf LICENSE -- *.tar.xz *.md
}

install
set_permissions
cleanup
