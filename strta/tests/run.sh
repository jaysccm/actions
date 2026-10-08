#!/bin/sh
# Tests for the standard scripts: hook discovery, idempotency, failure propagation, rollback, locking.
# Plain sh, no dependencies: sh strta/tests/run.sh
set -u
here=$(cd "$(dirname "$0")" && pwd)
src=$here/../script
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
pass=0; fail=0

ok() { pass=$((pass + 1)); printf 'ok   %s\n' "$1"; }
bad() { fail=$((fail + 1)); printf 'FAIL %s\n' "$1"; }
expect() { # expect <name> <expected-exit> <command...>
  name=$1; want=$2; shift 2
  out=$("$@" 2>&1); got=$?
  if [ "$got" = "$want" ]; then ok "$name"; else bad "$name (exit $got, wanted $want)"; printf '%s\n' "$out" | sed 's/^/     | /'; fi
  LAST_OUT=$out
}
contains() { case $LAST_OUT in *"$1"*) ok "$2" ;; *) bad "$2 (missing '$1')"; printf '%s\n' "$LAST_OUT" | sed 's/^/     | /' ;; esac; }

# A fresh project: a git repo with two commits, the scripts vendored in, and a hooks dir.
new_project() {
  p=$work/$1; rm -rf "$p"; mkdir -p "$p"
  git -C "$p" init -q . && git -C "$p" config user.email t@t && git -C "$p" config user.name t
  cp -R "$src" "$p/script"; mkdir -p "$p/script/ext"
  echo one > "$p/version"; git -C "$p" add -A && git -C "$p" commit -qm one
  echo two > "$p/version"; git -C "$p" commit -qam two
  echo "$p"
}
hook() { printf '#!/bin/sh\n%s\n' "$3" > "$1/script/ext/$2"; chmod +x "$1/script/ext/$2"; }
export STRTA_LOCK_DIR=$work
export TMPDIR=$work

# --- hook discovery -----------------------------------------------------------
p=$(new_project discovery)
expect "bootstrap with no hooks succeeds silently" 0 "$p/script/bootstrap"
printf '#!/bin/sh\necho ran\n' > "$p/script/ext/pre-bootstrap"        # present but not executable
expect "non-executable hook is skipped" 0 "$p/script/bootstrap"
case $LAST_OUT in *ran*) bad "non-executable hook ran" ;; *) ok "non-executable hook did not run" ;; esac
hook "$p" pre-bootstrap 'echo "ctx:$PROJECT:$ENV_FILE"'
expect "executable hook runs" 0 "$p/script/bootstrap"
contains "ctx:discovery:$p/.env" "hook receives PROJECT and ENV_FILE"
hook "$p" bootstrap 'exit 7'
expect "failing hook fails the step" 1 "$p/script/bootstrap"

# --- setup: idempotent, env template, no overwrite ----------------------------
p=$(new_project setup)
echo 'KEY=' > "$p/.env.example"
hook "$p" setup 'echo built >> "$STRTA_ROOT_LOG"'
export STRTA_ROOT_LOG=$p/build.log
expect "setup creates env from template" 0 "$p/script/setup"
[ -f "$p/.env" ] && ok ".env created" || bad ".env created"
[ "$(stat -c %a "$p/.env")" = 600 ] && ok ".env is mode 600" || bad ".env is mode 600"
echo 'KEY=secret' > "$p/.env"
expect "setup is safe to re-run" 0 "$p/script/setup"
[ "$(cat "$p/.env")" = 'KEY=secret' ] && ok "re-run keeps existing .env" || bad "re-run keeps existing .env"
[ "$(wc -l < "$p/build.log")" = 2 ] && ok "setup hook ran each time" || bad "setup hook ran each time"

# --- update -------------------------------------------------------------------
# Hooks live in the checkout, so every ref under test must already contain them: commit hooks first, then
# make two commits on top that differ only in the version file.
versioned() { # versioned <project> <version>: commit a new version and print its sha
  echo "$2" > "$1/version"; git -C "$1" add -A; git -C "$1" commit -qm "version $2" --allow-empty; git -C "$1" rev-parse HEAD
}
p=$(new_project update)
hook "$p" apply 'echo "applied $(cat '"$p"'/version) ref=$REF prev=$PREVIOUS_REF image=$DEPLOY_IMAGE" >> '"$p"'/applied.log'
git -C "$p" add -A; git -C "$p" commit -qm hooks
first=$(versioned "$p" one); second=$(versioned "$p" two)
expect "update to an older ref" 0 "$p/script/update" --ref "$first"
[ "$(git -C "$p" rev-parse HEAD)" = "$first" ] && ok "checkout moved to ref" || bad "checkout moved to ref"
expect "update with a prebuilt image passes it to apply" 0 "$p/script/update" --ref "$second" --image reg/app:sha
grep -q 'image=reg/app:sha' "$p/applied.log" && ok "apply saw DEPLOY_IMAGE" || bad "apply saw DEPLOY_IMAGE"
grep -q "prev=$first" "$p/applied.log" && ok "apply saw PREVIOUS_REF" || bad "apply saw PREVIOUS_REF"
expect "repeat invocation succeeds (idempotent)" 0 "$p/script/update" --ref "$second"
contains "already at" "repeat invocation reports no move"
expect "unresolvable ref is a preflight failure" 2 "$p/script/update" --ref nonexistent-ref
[ "$(git -C "$p" rev-parse HEAD)" = "$second" ] && ok "failed preflight changed nothing" || bad "failed preflight changed nothing"
expect "bad --wait is a usage error" 2 "$p/script/update" --wait abc
expect "unknown flag is a usage error" 2 "$p/script/update" --bogus
echo dirty >> "$p/version"
expect "dirty checkout is refused" 2 "$p/script/update" --ref "$first"
git -C "$p" checkout -q -- version

# failure and rollback: apply fails when the target is version "one"
p=$(new_project rollback)
hook "$p" apply 'if [ "$(cat '"$p"'/version)" = one ]; then exit 9; fi; echo ok >> '"$p"'/applied.log'
git -C "$p" add -A; git -C "$p" commit -qm hooks
bad_ref=$(versioned "$p" one); good=$(versioned "$p" two)
expect "failed apply exits 1" 1 "$p/script/update" --ref "$bad_ref"
[ "$(git -C "$p" rev-parse HEAD)" = "$good" ] && ok "failed apply rolled the checkout back" || bad "failed apply rolled the checkout back"
contains "failed at step: apply" "failure names the step"

p=$(new_project verify)
hook "$p" apply 'true'
hook "$p" verify 'if [ "$(cat '"$p"'/version)" = one ]; then exit 1; fi'
git -C "$p" add -A; git -C "$p" commit -qm hooks
bad_ref=$(versioned "$p" one); good=$(versioned "$p" two)
expect "failed health check exits 1" 1 "$p/script/update" --ref "$bad_ref" --wait 1
[ "$(git -C "$p" rev-parse HEAD)" = "$good" ] && ok "failed health check restored the previous commit" || bad "failed health check restored the previous commit"
contains "verify" "failure names the verify step"

p=$(new_project customrb)
hook "$p" apply 'true'; hook "$p" verify 'exit 1'; hook "$p" rollback 'echo custom-rollback >> '"$p"'/rb.log'
git -C "$p" add -A; git -C "$p" commit -qm hooks
other=$(versioned "$p" other)
git -C "$p" checkout -q --detach HEAD~1
expect "custom rollback hook is used" 1 "$p/script/update" --ref "$other" --wait 1
[ -f "$p/rb.log" ] && ok "rollback hook ran" || bad "rollback hook ran"

# --- ext/update takes over -----------------------------------------------------
p=$(new_project takeover)
hook "$p" update 'echo "owner-ran ref=$REF prev=$PREVIOUS_REF head=$(git -C '"$p"' rev-parse HEAD)" >> '"$p"'/owner.log'
hook "$p" apply 'echo apply-ran >> '"$p"'/owner.log'
hook "$p" pre-update 'echo pre >> '"$p"'/owner.log'
git -C "$p" add -A; git -C "$p" commit -qm hooks
cur=$(git -C "$p" rev-parse HEAD); tgt=$(versioned "$p" two); git -C "$p" checkout -q --detach "$cur"
expect "update hook takes over" 0 "$p/script/update" --ref "$tgt"
grep -q "owner-ran ref=$tgt prev=$cur head=$cur" "$p/owner.log" && ok "hook gets the full REF and the checkout is not moved by the standard" || bad "hook gets the full REF and the checkout is not moved"
grep -q apply-ran "$p/owner.log" && bad "apply must not run when update exists" || ok "apply does not run when update exists"
[ "$(head -1 "$p/owner.log")" = pre ] && ok "pre-update runs first" || bad "pre-update runs first"
hook "$p" update 'exit 5'; git -C "$p" add -A; git -C "$p" commit -qm failing
expect "failing update hook exits 1" 1 "$p/script/update" --ref HEAD

# --- STRTA_AS: git and env-file writes go through the owner prefix, hooks do not --------
p=$(new_project owner)
printf '#!/bin/sh\necho "$@" >> %s/as.log\nexec "$@"\n' "$work" > "$work/asshim"; chmod +x "$work/asshim"
printf 'STRTA_AS=%s\n' "$work/asshim" > "$p/script/ext/env"
hook "$p" apply 'echo "apply-ran" >> '"$p"'/apply.log'
echo 'K=' > "$p/.env.example"
git -C "$p" add -A; git -C "$p" commit -qm hooks; rm -f "$work/as.log"
tgt=$(versioned "$p" two)
expect "update works with STRTA_AS set" 0 "$p/script/update" --ref HEAD~1
grep -q '^git checkout' "$work/as.log" && grep -q '^git rev-parse' "$work/as.log" && grep -q '^git status' "$work/as.log" && ok "git calls went through STRTA_AS" || bad "git calls went through STRTA_AS"
! grep -q 'apply' "$work/as.log" && ok "hooks were not run through STRTA_AS" || bad "hooks were not run through STRTA_AS"
expect "setup creates env via STRTA_AS" 0 "$p/script/setup"
grep -q '^cp ' "$work/as.log" && ok "env file creation went through STRTA_AS" || bad "env file creation went through STRTA_AS"
expect "status works with STRTA_AS" 0 "$p/script/status"

# --- locking ------------------------------------------------------------------
p=$(new_project lock); hook "$p" apply 'sleep 3'; git -C "$p" add -A && git -C "$p" commit -qm hook
"$p/script/update" >/dev/null 2>&1 & holder=$!
sleep 1
expect "concurrent update is refused" 3 "$p/script/update"
wait "$holder" && ok "first run unaffected by the refused second" || bad "first run unaffected"
expect "lock is released after the run" 0 "$p/script/update"

# --- deploy -------------------------------------------------------------------
p=$(new_project deploy); first=$(git -C "$p" rev-parse HEAD~1)
expect "deploy without a ref is refused" 2 "$p/script/deploy"
hook "$p" pre-deploy 'exit 1'
expect "failed pre-deploy changes nothing" 2 "$p/script/deploy" "$first"
[ "$(git -C "$p" rev-parse HEAD)" != "$first" ] && ok "pre-deploy failure left checkout alone" || bad "pre-deploy failure left checkout alone"
rm "$p/script/ext/pre-deploy"; git -C "$p" add -A; git -C "$p" commit -qm x 2>/dev/null
hook "$p" status 'exit 1'; git -C "$p" add -A; git -C "$p" commit -qm y
expect "unhealthy final status fails the deploy" 1 "$p/script/deploy" HEAD
hook "$p" status 'echo healthy'; git -C "$p" add -A; git -C "$p" commit -qm z
expect "healthy deploy succeeds" 0 "$p/script/deploy" HEAD

# --- status -------------------------------------------------------------------
p=$(new_project status)
expect "status with no compose file and no hook is healthy" 0 "$p/script/status"
printf 'services:\n  app:\n    image: x\n' > "$p/compose.yaml"
expect "compose service without restart policy is unhealthy" 4 "$p/script/status"
contains "restart" "status names the restart-policy gap"
printf 'services:\n  app:\n    image: x\n    restart: unless-stopped\n' > "$p/compose.yaml"
expect "restart: unless-stopped passes" 0 "$p/script/status"
expect "status --json is machine-readable" 0 "$p/script/status" --json
contains '"healthy":true' "json reports healthy"

# --- test / help --------------------------------------------------------------
p=$(new_project testcmd)
expect "script/test without a hook says so" 2 "$p/script/test"
hook "$p" test 'echo tests-ran "$@"'
expect "script/test runs the hook with args" 0 "$p/script/test" -k foo
contains "tests-ran -k foo" "test hook received args"
for s in bootstrap setup update deploy status test; do expect "$s --help exits 0" 0 "$p/script/$s" --help; done

printf '\n%s passed, %s failed\n' "$pass" "$fail"
[ "$fail" = 0 ]
