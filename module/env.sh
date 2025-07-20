export HOME="/data/adb/py2droid"

export PATH="${HOME}/usr/bin:${HOME}/.local/bin:${PATH}"
export LD_LIBRARY_PATH="${HOME}/usr/lib:${LD_LIBRARY_PATH}"

export XDG_CACHE_HOME="${HOME}/.cache"
export XDG_CONFIG_HOME="${HOME}/.config"
export XDG_DATA_HOME="${HOME}/.local/share"
export XDG_STATE_HOME="${HOME}/.local/state"

[[ -z ${NO_TMPDIR} ]] && export TMPDIR="${HOME}/.tmp"
