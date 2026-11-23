#!/bin/sh
# Run in the pinned Linux Node build image. Source is read-only; the
# output directory receives the archive only after both lockfiles install.
set -eu
src=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
out=${1:?supply an output tar.gz path}
test ! -e "$out"
test "$(uname -sm)" = "Linux x86_64"
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
root=$work/factory-worker
mkdir -p "$root/bin" "$root/scripts" "$root/src/Factory.Runtime" "$root/src/Factory.Dispatch"
for project in Factory.Runtime Factory.Dispatch; do
    cp "$src/src/$project/package.json" "$src/src/$project/package-lock.json" "$root/src/$project/"
done
cp "$src/src/Factory.Runtime/native-cli.json" "$root/src/Factory.Runtime/"
cp "$src/src/Factory.Dispatch/bridge.ts" "$root/src/Factory.Dispatch/"
for module in blueprint_tools codex_worker codex_workspace environment_leases leased_fixture linux_worker_service m0_lease_tool run_ci worker_runtime; do
    cp "$src/scripts/$module.py" "$root/scripts/"
done
cp "$src/scripts/m0-account.mjs" "$src/scripts/linux-worker.sh" "$root/scripts/"
chmod 755 "$root/scripts/linux-worker.sh"
npm ci --prefix "$root/src/Factory.Runtime" --ignore-scripts --no-audit --no-fund
npm ci --prefix "$root/src/Factory.Dispatch" --ignore-scripts --no-audit --no-fund
npm run --prefix "$root/src/Factory.Dispatch" check
npm prune --prefix "$root/src/Factory.Dispatch" --omit=dev --ignore-scripts --no-audit --no-fund
cp /usr/local/bin/node /usr/local/LICENSE "$root/bin/"
cli=$(node -p 'require(process.argv[1])["linux-x64"]' "$root/src/Factory.Runtime/native-cli.json")
test "$("$root/src/Factory.Runtime/node_modules/$cli" --version)" = "codex-cli 0.171.0"
test -x "$root/bin/node"
(cd "$work" && tar -czf "$work/worker.tar.gz" factory-worker)
cp "$work/worker.tar.gz" "$out"
