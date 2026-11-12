#!/usr/bin/env bash
# Cut a release: bump the version (semver), test, build the signed DMG, update CHANGELOG.md, commit, tag.
#   scripts/release.sh patch   bug fixes only            0.2.0 -> 0.2.1
#   scripts/release.sh minor   new features              0.2.1 -> 0.3.0
#   scripts/release.sh major   breaking (data, API)      0.3.0 -> 1.0.0
# One version for the whole app: tauri.conf.json is the source of truth; package.json, Cargo.toml and the
# engine's pyproject.toml follow it. Nothing is pushed.
set -euo pipefail
cd "$(dirname "$0")/.."
part=${1:-}
case "$part" in patch|minor|major) ;; *) echo "usage: scripts/release.sh patch|minor|major" >&2; exit 2 ;; esac
[ "$(git branch --show-current)" = main ] || { echo "release from main" >&2; exit 1; }
git diff --quiet && git diff --cached --quiet || { echo "commit or stash your changes first" >&2; exit 1; }

cur=$(python3 -c 'import json; print(json.load(open("app/src-tauri/tauri.conf.json"))["version"])')
new=$(python3 - "$cur" "$part" <<'EOF'
import sys
ma, mi, pa = map(int, sys.argv[1].split("."))
print({"major": f"{ma + 1}.0.0", "minor": f"{ma}.{mi + 1}.0", "patch": f"{ma}.{mi}.{pa + 1}"}[sys.argv[2]])
EOF
)
git rev-parse -q --verify "refs/tags/v$new" >/dev/null && { echo "tag v$new already exists" >&2; exit 1; }
last=$(git describe --tags --abbrev=0 2>/dev/null || true)
echo "Kioku $cur -> $new"

# 1. bump every version field (exact "x.y.z" of the current version only)
python3 - "$cur" "$new" <<'EOF'
import json, re, sys, pathlib
cur, new = sys.argv[1:]
for f in ["app/src-tauri/tauri.conf.json", "app/package.json"]:
    p = pathlib.Path(f); s = p.read_text()
    s2 = s.replace(f'"version": "{cur}"', f'"version": "{new}"', 1)
    assert s2 != s, f"no version {cur} in {f}"; p.write_text(s2)
p = pathlib.Path("app/package-lock.json"); s = p.read_text()  # its first two "version"s are the app's own
s2 = s.replace(f'"version": "{cur}"', f'"version": "{new}"', 2)
assert s2.count(f'"version": "{new}"') >= 2, "package-lock.json root version not found"; p.write_text(s2)
for f in ["app/src-tauri/Cargo.toml", "engine/pyproject.toml"]:
    p = pathlib.Path(f); s = p.read_text()
    s2 = re.sub(rf'^version = "{re.escape(cur)}"$', f'version = "{new}"', s, count=1, flags=re.M)
    assert s2 != s, f"no version {cur} in {f}"; p.write_text(s2)
EOF
(cd engine && uv lock -q)  # the engine's own entry in uv.lock carries the version

# 2. tests and typecheck must pass before anything is built or tagged
tmp=$(mktemp -d)
(cd engine && for t in test_*.py selfcheck.py; do KIOKU_DATA="$tmp" .venv/bin/python "$t" >/dev/null || { echo "FAIL $t" >&2; exit 1; }; done)
(cd app && npx tsc --noEmit)
(cd app && node --experimental-strip-types --no-warnings src/song.check.mjs >/dev/null) || { echo "FAIL src/song.check.mjs" >&2; exit 1; }

# 3. the signed DMG (Kioku_<version>_aarch64.dmg; older versions stay next to it)
(cd app && npx tauri build --bundles dmg)
dmg="app/src-tauri/target/release/bundle/dmg/Kioku_${new}_aarch64.dmg"
[ -f "$dmg" ] || { echo "build did not produce $dmg" >&2; exit 1; }

# 4. changelog: commit subjects since the last tag, newest first (tracker-only commits left out)
python3 - "$new" "${last:-}" <<'EOF'
import datetime, pathlib, subprocess, sys
new, last = sys.argv[1], sys.argv[2]
rng = f"{last}..HEAD" if last else "HEAD"
subs = subprocess.run(["git", "log", "--no-merges", "--pretty=%s", rng], capture_output=True, text=True, check=True).stdout.splitlines()
subs = [s for s in subs if not s.startswith("requests.md")]
entry = f"## v{new} ({datetime.date.today()})\n\n" + "".join(f"- {s}\n" for s in subs) + "\n"
p = pathlib.Path("CHANGELOG.md")
head, _, rest = (p.read_text() if p.exists() else "# Changelog\n\n").partition("\n\n")
p.write_text(head + "\n\n" + entry + rest)
EOF

# 5. commit and tag
git add app/src-tauri/tauri.conf.json app/package.json app/package-lock.json app/src-tauri/Cargo.toml app/src-tauri/Cargo.lock engine/pyproject.toml engine/uv.lock CHANGELOG.md
git commit -q -m "Release v$new"
git tag -a "v$new" -m "Kioku v$new"
echo "Released v$new: $(pwd)/$dmg"
