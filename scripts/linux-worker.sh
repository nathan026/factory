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
    usage)
        if [ "$#" -ne 2 ]; then echo 'Usage: linux-worker.sh usage TASK_UUID' >&2; exit 2; fi
        exec node --experimental-sqlite --input-type=commonjs - "$2" <<'NODE'
const task = process.argv[2];
if (task.length !== 36 || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(task)) {
    console.error('Expected a task UUID.');
    process.exit(2);
}

const journal = process.env.FACTORY_LEASE_DB;
if (!journal) {
    console.error('Set FACTORY_LEASE_DB to the deployment lease journal path.');
    process.exit(2);
}

const { DatabaseSync } = require('node:sqlite');
const taskId = task.toLowerCase();
const tokenKeys = ['input_tokens', 'cached_input_tokens', 'output_tokens'];
const phases = new Map();
try {
    const db = new DatabaseSync(journal, { readOnly: true });
    try {
        const attempts = db.prepare('SELECT action, usage FROM codex_attempts WHERE task = ? ORDER BY action');
        for (const { action, usage } of attempts.iterate(taskId)) {
            if (!phases.has(action)) {
                phases.set(action, tokenKeys.map(() => ({ sum: 0n, known: 0, missing: 0 })));
            }
            let counts;
            try {
                counts = JSON.parse(usage, (key, value, context) => {
                    if (typeof value === 'number' && tokenKeys.includes(key)) {
                        // Use the original integer literal before Number rounding.
                        if (!/^-?(?:0|[1-9][0-9]{0,18})$/.test(context.source)) return null;
                        const count = BigInt(context.source);
                        return count >= 0n && count <= 9223372036854775807n ? count : null;
                    }
                    return value;
                });
            } catch { counts = null; }
            phases.get(action).forEach((total, index) => {
                const count = counts?.[tokenKeys[index]];
                if (typeof count === 'bigint') {
                    total.sum += count;
                    total.known++;
                } else {
                    total.missing++;
                }
            });
        }
    } finally {
        db.close();
    }
} catch {
    console.error('Could not read the Codex attempts from the lease journal.');
    process.exit(1);
}

function formatTokens({ sum, known, missing }) {
    if (!known) return 'unknown';
    return missing ? `${sum} known; ${missing} unknown` : String(sum);
}

console.log(`# Codex usage for task ${taskId}\n`);
console.log('Funding mode: subscription. API billing is outside this journal.');
console.log('The current journal records token counts. Executed model and attributable cost are unknown.\n');
if (!phases.size) console.log('No subscription attempts recorded for this task.\n');
console.log('| Phase | Input tokens | Cached input tokens | Output tokens | Executed model | Attributable cost |');
console.log('| --- | ---: | ---: | ---: | --- | --- |');
for (const [action, totals] of phases) {
    const phase = typeof action === 'string' && action && !/[^A-Za-z0-9_-]/.test(action) ? action : 'unknown';
    console.log(`| ${phase} | ${totals.map(formatTokens).join(' | ')} | unknown | unknown |`);
}
NODE
        ;;
    *) echo 'Choose login, allowance, register, worker, dispatch, ci or usage TASK_UUID.' >&2; exit 2;;
esac
