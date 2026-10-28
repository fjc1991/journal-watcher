"""Signs a release on YOUR computer, so that installed copies of Journal Watcher believe only updates you approved.

    python app/sign_release.py keygen                       make your signing key (once). The public half is printed: put it in app/update_public_key.txt
    python app/sign_release.py sign --tag v1.2.0            fetch the release, check it, ask you, write appcast.json.sig (and upload it with --upload)
    python app/sign_release.py sign --dir some/folder       the same for files you downloaded yourself (appcast.json and the zip it names): works while the repository is private
    python app/sign_release.py verify --appcast A --sig S   check a signature against app/update_public_key.txt

Why by hand: the release workflow builds the app, but whoever can change that workflow (or take over the GitHub account) could make it build and sign anything. The key
stays on your computer; the app only believes a feed signed by it (see scripts/updater.py). The script shows you what you are signing (version, notes, the zip's address
and checksum, recomputed from the zip itself) and asks you to type the version. It cannot tell you that the zip contains what your source code says: that is for you to
trust about your own tag and workflow run.

The key is a 32-byte seed in ~/.journal-watcher/release-signing-key (base64, readable only by you). KEEP A BACKUP (a password manager's secure note is fine): without it, copies
already installed cannot be updated by themselves until people install a new app built with a new public key. Anyone who gets the file can sign updates.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SIG_PREFIX = b"journal-watcher-appcast-v1\n"          # the same line as updater.SIG_PREFIX (a test keeps them equal)
DEFAULT_KEY = os.path.expanduser("~/.journal-watcher/release-signing-key")
PUBLIC_FILE = os.path.join(HERE, "update_public_key.txt")
REPO = "fjc1991/journal-watcher"


def nacl_signing():
    try:
        from nacl import signing
    except ImportError:
        sys.exit("PyNaCl is needed: pip install pynacl  (it is in requirements.txt)")
    return signing


def load_key(path):
    try:
        seed = base64.b64decode(open(path, encoding="utf-8").read().strip(), validate=True)
    except (OSError, ValueError):
        sys.exit("Cannot read the signing key at %s. Make one with: python app/sign_release.py keygen" % path)
    if len(seed) != 32:
        sys.exit("The signing key at %s is not a 32-byte key." % path)
    return nacl_signing().SigningKey(seed)


def public_b64(sk):
    return base64.b64encode(bytes(sk.verify_key)).decode()


def sign_bytes(sk, data):
    return base64.b64encode(sk.sign(SIG_PREFIX + data).signature).decode()


def keygen(path):
    if os.path.exists(path):
        sys.exit("There is already a key at %s: not overwriting it. (Delete it yourself if you really want a new one.)" % path)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    sk = nacl_signing().SigningKey.generate()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(base64.b64encode(bytes(sk)).decode() + "\n")
    print("Your signing key is in %s (readable only by you)." % path)
    print("BACK IT UP now (for example as a secure note in your password manager): without it, installed copies cannot be updated by themselves.")
    print("\nThe public key, safe to share. Put this line in app/update_public_key.txt and commit it:\n\n%s\n" % public_b64(sk))
    return public_b64(sk)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "journal-watcher-sign-release"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def check_release(appcast_bytes, zip_path, tag=None, repo=REPO):
    """What the feed claims, against the zip it names; returns the parsed feed or raises SystemExit saying what does not match."""
    try:
        d = json.loads(appcast_bytes.decode("utf-8"))
    except ValueError:
        sys.exit("appcast.json is not valid JSON.")
    version = str(d.get("version") or "")
    if not re.fullmatch(r"\d+(\.\d+){1,3}", version):
        sys.exit("appcast.json names no valid version.")
    if tag and tag.lstrip("v") != version:
        sys.exit("The tag %s and the feed's version %s do not match." % (tag, version))
    if tag:
        want = "https://github.com/%s/releases/download/%s/Journal-Watcher-%s.zip" % (repo, tag, version)
        if d.get("url") != want:
            sys.exit("The feed points at %s, not at the release's own zip (%s). Not signing." % (d.get("url"), want))
    if os.path.getsize(zip_path) != int(d.get("size") or -1):
        sys.exit("The zip is %d bytes, the feed says %s. Not signing." % (os.path.getsize(zip_path), d.get("size")))
    if sha256_of(zip_path) != str(d.get("sha256") or "").lower():
        sys.exit("The zip's checksum is not the one in the feed. Not signing.")
    return d


def sign(args):
    sk = load_key(args.key)
    tmp = None
    try:
        if args.dir:
            appcast, zip_path = os.path.join(args.dir, "appcast.json"), None
            if not os.path.isfile(appcast):
                sys.exit("No appcast.json in %s." % args.dir)
            name = os.path.basename(str(json.load(open(appcast, encoding="utf-8")).get("url") or ""))
            zip_path = os.path.join(args.dir, name)
            if not (name and os.path.isfile(zip_path)):
                sys.exit("The zip named in the feed (%s) is not in %s." % (name or "?", args.dir))
            tag = args.tag
        else:
            if not args.tag:
                sys.exit("Say which release: --tag v1.2.0 (or --dir for files you downloaded).")
            tag = args.tag
            tmp = tempfile.mkdtemp(prefix="jw-sign-")
            appcast = os.path.join(tmp, "appcast.json")
            base = "https://github.com/%s/releases/download/%s/" % (args.repo, tag)
            get(base + "appcast.json", appcast)
            name = "Journal-Watcher-%s.zip" % tag.lstrip("v")
            zip_path = os.path.join(tmp, name)
            print("Downloading %s …" % name)
            get(base + name, zip_path)
        data = open(appcast, "rb").read()
        d = check_release(data, zip_path, tag, args.repo)
        print("\nRelease %s (%s)  %.0f MB" % (d["version"], d.get("released", "?"), os.path.getsize(zip_path) / 1048576))
        print("  zip checksum (computed here, equal to the feed's): %s" % d["sha256"])
        print("  what is new:")
        for n in d.get("notes") or []:
            print("    - " + str(n))
        if not args.yes:
            if input("\nType the version (%s) to sign this release, anything else to stop: " % d["version"]).strip() != d["version"]:
                sys.exit("Not signed.")
        sig = sign_bytes(sk, data)
        out = os.path.join(args.out, "appcast.json.sig")
        os.makedirs(args.out, exist_ok=True)
        with open(out, "w", encoding="ascii") as f:
            f.write(sig + "\n")
        # the signature must verify with the public key that is committed for the app, or the apps built from this tree would ignore it
        if os.path.exists(PUBLIC_FILE):
            keys = [l.split("#")[0].strip() for l in open(PUBLIC_FILE, encoding="utf-8").read().splitlines()]
            if public_b64(sk) not in [k for k in keys if k]:
                print("WARNING: your key is not in app/update_public_key.txt, so apps built from this tree will not accept this signature.")
        else:
            print("NOTE: app/update_public_key.txt does not exist yet, so apps built from this tree do not check signatures at all.")
        print("\nWrote %s" % out)
        if args.upload and tag:
            if not shutil.which("gh"):
                sys.exit("--upload needs the GitHub command-line tool (gh). Upload %s to the release by hand instead." % out)
            subprocess.run(["gh", "release", "upload", tag, out, "--clobber", "--repo", args.repo], check=True)
            print("Uploaded to release %s: apps will see the update within six hours (or at once through Help → Check for Updates…)." % tag)
        else:
            print("Now add it to the release as an extra file (GitHub → Releases → %s → Edit → attach appcast.json.sig), or run: gh release upload %s %s --clobber --repo %s"
                  % (tag or "<tag>", tag or "<tag>", out, args.repo))
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def verify(args):
    keys = [l.split("#")[0].strip() for l in open(args.pub, encoding="utf-8").read().splitlines()] if os.path.exists(args.pub) else [args.pub]
    data, sig = open(args.appcast, "rb").read(), open(args.sig, encoding="ascii").read().strip()
    signing = nacl_signing()
    from nacl.exceptions import BadSignatureError
    for k in [k for k in keys if k]:
        try:
            signing.VerifyKey(base64.b64decode(k)).verify(SIG_PREFIX + data, base64.b64decode(sig))
            print("Good signature (key %s…)." % k[:8])
            return
        except (BadSignatureError, ValueError):
            continue
    sys.exit("BAD signature.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen"); k.add_argument("--key", default=DEFAULT_KEY)
    s = sub.add_parser("sign"); s.add_argument("--tag"); s.add_argument("--dir"); s.add_argument("--repo", default=REPO); s.add_argument("--key", default=DEFAULT_KEY)
    s.add_argument("--out", default="."); s.add_argument("--upload", action="store_true"); s.add_argument("--yes", action="store_true", help="do not ask (tests)")
    v = sub.add_parser("verify"); v.add_argument("--appcast", required=True); v.add_argument("--sig", required=True); v.add_argument("--pub", default=PUBLIC_FILE, help="a file of keys, or one base64 key")
    a = ap.parse_args(argv)
    {"keygen": lambda: keygen(a.key), "sign": lambda: sign(a), "verify": lambda: verify(a)}[a.cmd]()


if __name__ == "__main__":
    main()
