#!/bin/sh
# Entrypoint for the Linux release bundle. The operator chooses the profile
# and credentials outside this bundle, using the normal bridge configuration.
set -eu
umask 077
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
PATH="$root/bin:$PATH"
export PATH
case "${1:-}" in
    allowance|login)
        case "${CODEX_HOME:-}" in /*) ;; *) echo 'Set the deployment CODEX_HOME to an absolute directory.' >&2; exit 2;; esac
        if [ "$1" = allowance ]; then exec "$root/bin/node" "$root/scripts/m0-account.mjs"; fi
        # Only the deployment's local user session may perform subscription sign-in.
        if [ ! -d "$CODEX_HOME" ] || [ "$(stat -c %u:%a -- "$CODEX_HOME")" != "$(id -u):700" ]; then
            echo "Sign-in needs a private CODEX_HOME owned by the worker account (mode 0700)." >&2; exit 2
        fi
        shift
        case "$#:${1:-}" in 0:|1:--device-auth) ;; *) echo 'Supported: login [--device-auth]' >&2; exit 2;; esac
        cli=$("$root/bin/node" --input-type=commonjs -e 'process.stdout.write(require(process.argv[1])["linux-x64"])' "$root/src/Factory.Runtime/native-cli.json")
        exec env -i HOME="$HOME" CODEX_HOME="$CODEX_HOME" PATH="$PATH" CODEX_EXEC_SERVER_URL=none \
            "$root/src/Factory.Runtime/node_modules/$cli" -c 'forced_login_method="chatgpt"' login "$@"
        ;;
    register|worker|dispatch)
        : "${FACTORY_CONTROLLER_RELEASE:?set the existing supervised controller release directory}"
        test -x "$FACTORY_CONTROLLER_RELEASE/Factory.Api"
        if [ "$1" = worker ]; then
            : "${FACTORY_LEASE_DB:?set the deployment lease journal path}"
            case "${CODEX_HOME:-}" in /*) ;; *) echo 'Set an absolute deployment CODEX_HOME.' >&2; exit 2;; esac
        fi
        exec "$root/bin/node" --experimental-strip-types "$root/src/Factory.Dispatch/bridge.ts" "$1"
        ;;
    ci)
        : "${FACTORY_CONTROLLER_RELEASE:?set the existing supervised controller release directory}"
        : "${FACTORY_LEASE_DB:?set the deployment lease journal path}"
        shift
        exec python3 "$root/scripts/run_ci.py" "$@"
        ;;
    *) echo 'Choose login, allowance, register, worker, dispatch or ci.' >&2; exit 2;;
esac
