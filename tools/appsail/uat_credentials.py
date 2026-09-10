"""Generate or rotate the UAT preview credentials -- OUTSIDE the repository.

    python tools/appsail/uat_credentials.py --generate <dir-outside-the-repo>
    python tools/appsail/uat_credentials.py --rotate   <that-dir>
    python tools/appsail/uat_credentials.py --check    <that-dir>/uat-credentials.json

Fable 5.1. The hosted UAT preview must not run on the seeded `<user-id>!demo`
scheme: the sign-in page used to list the ids, so every password was one
guess away. This tool produces TWO files in a directory it refuses to place
inside the repository:

  uat-users.txt          the plaintext passwords, for the UAT coordinator's
                         eyes only. NEVER copied into the bundle, never
                         printed by this tool, never committed.
  uat-credentials.json   PBKDF2 salt+hash pairs only. This is the file that
                         ships in the AppSail bundle and that app/run.py
                         loads under CAPEX_PROFILE=uat-preview.

Rotation is: run --rotate, rebuild the bundle (tools/appsail/build_uat_bundle.py),
redeploy. There is no in-place rotation on the hosted service, deliberately:
the running instance holds no writable secret store, and a password that can
be changed through the preview's own API is a password that can be changed
by whoever finds a session.

Nothing here prints a password. The only thing written to stdout is the
directory, the user ids, and the SHA-256 of the credentials file, so a build
log can prove WHICH credential set a bundle carries without revealing it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

from app.backend import auth  # noqa: E402

PASSWORD_BYTES = 15          # token_urlsafe(15) -> 20 characters, ~120 bits
CREDENTIALS_NAME = "uat-credentials.json"
PLAINTEXT_NAME = "uat-users.txt"


class Refused(RuntimeError):
    pass


def _refuse_inside_repo(target: Path) -> None:
    try:
        target.resolve().relative_to(REPO.resolve())
    except ValueError:
        return
    raise Refused(f"{target} is inside the repository; credentials must live outside it.")


def _new_password() -> str:
    return secrets.token_urlsafe(PASSWORD_BYTES)


def generate(target_dir: Path) -> dict:
    _refuse_inside_repo(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    users: dict[str, dict[str, str]] = {}
    plaintext_lines = ["# CAPEX & WBS Control Hub -- UAT preview credentials",
                       "# Issued by tools/appsail/uat_credentials.py. Do not commit. Do not paste into chat.",
                       "# user_id<TAB>password", ""]
    for user_id, roles in auth.DEV_USERS:
        password = _new_password()
        # The seeded scheme is the one thing a generated password must never be.
        assert password != user_id + "!demo"
        salt, digest = auth.hash_password(password)
        users[user_id] = {"salt": salt, "hash": digest}
        plaintext_lines.append(f"{user_id}\t{password}\t# roles: {', '.join(roles)}")
        del password

    cred_path = target_dir / CREDENTIALS_NAME
    plain_path = target_dir / PLAINTEXT_NAME
    doc = {"format": "capex-uat-credentials/1", "pbkdf2_rounds": auth.PBKDF2_ROUNDS,
           "users": users}
    cred_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    plain_path.write_text("\n".join(plaintext_lines) + "\n", encoding="utf-8")
    for p in (cred_path, plain_path):
        try:
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass  # Windows ACLs; the directory is the user's own
    return {"dir": str(target_dir), "users": sorted(users),
            "credentials_sha256": hashlib.sha256(cred_path.read_bytes()).hexdigest()}


def check(cred_path: Path) -> dict:
    """Re-run the loader the application uses, then prove no entry is the
    derivable demo password. The second check costs one PBKDF2 per user and
    is the reason it lives here rather than in the boot path."""
    loaded = auth.load_uat_credentials(str(cred_path))
    derivable = [uid for uid, e in loaded.items()
                 if auth.verify_password(uid + "!demo", e["salt"], e["hash"])]
    if derivable:
        raise Refused(f"credentials for {derivable} are the derivable demo password")
    raw = cred_path.read_text(encoding="utf-8")
    if "password" in raw.lower():
        raise Refused("the credentials file mentions 'password'; only salt/hash belong in it")
    return {"file": str(cred_path), "users": sorted(loaded),
            "credentials_sha256": hashlib.sha256(cred_path.read_bytes()).hexdigest()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--generate", metavar="DIR", help="create a fresh credential set in DIR")
    g.add_argument("--rotate", metavar="DIR", help="replace the credential set in DIR")
    g.add_argument("--check", metavar="FILE", help="validate an existing credentials JSON")
    args = ap.parse_args(argv)
    try:
        if args.check:
            out = check(Path(args.check))
        else:
            out = generate(Path(args.generate or args.rotate))
    except (Refused, auth.UatCredentialsError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
