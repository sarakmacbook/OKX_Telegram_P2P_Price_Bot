"""Guard the installers against the "install.sh is an HTML page" failure mode.

``./install.sh: line 7: syntax error near unexpected token `newline'`` with the
offending line ``<!DOCTYPE html>`` always means the same thing: the file that was
downloaded is a GitHub *web page*, not a shell script.  Two ways to hit it — both
of them documentation/script bugs this suite pins down:

* downloading from ``github.com/OWNER/REPO/blob/main/x.sh`` (the HTML viewer)
* downloading from ``raw.githubusercontent.com`` with a *renamed* repo slug —
  raw URLs do not follow renames, they answer with a 404 page

The third group of tests exercises the installers' own download guard, which now
refuses such a payload instead of installing it.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["install.sh", "install-docker.sh", "install-local.sh", "uninstall.sh"]
DOCS = ["README.md"]
TEXT_FILES = [ROOT / name for name in SCRIPTS + DOCS]

RAW_URL_RE = re.compile(
    r"raw\.githubusercontent\.com/([\w.-]+)/([\w.-]+)/([\w.-]+)/([\w./-]+)"
)


def _texts() -> dict:
    return {path.name: path.read_text(encoding="utf-8") for path in TEXT_FILES}


def _canonical_slug() -> str:
    """owner/name of this repository, straight from the git remote."""
    remote = subprocess.run(["git", "-C", str(ROOT), "remote", "get-url", "origin"],
                            capture_output=True, text=True)
    url = remote.stdout.strip()
    match = re.search(r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url)
    if remote.returncode != 0 or not match:
        pytest.skip("no git remote to derive the repository slug from")
    return f"{match.group(1)}/{match.group(2)}"


# ── the scripts themselves ────────────────────────────────────────────────

@pytest.mark.parametrize("name", SCRIPTS)
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_installer_is_valid_bash(name):
    """A `bash -n` parse of every installer (an HTML page fails exactly here)."""
    path = ROOT / name
    assert path.read_text(encoding="utf-8").startswith("#!/usr/bin/env bash"), \
        f"{name} must start with a bash shebang — a saved web page would not"
    assert path.stat().st_mode & 0o111, f"{name} must stay executable"
    proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, f"{name} does not parse:\n{proc.stderr}"


@pytest.mark.parametrize("name", SCRIPTS)
def test_installer_declares_guarded_downloader(name):
    """Fetches must be checked for an HTML/empty body before being kept."""
    text = (ROOT / name).read_text(encoding="utf-8")
    if name == "uninstall.sh":          # touches no network
        assert "http" not in text
        return
    for helper in ("looks_like_web_page", "looks_like_gzip", "guard_download"):
        assert re.search(rf"^{helper}\(\) \{{", text, re.M), f"{name}: missing {helper}()"
    assert re.search(r'^\s*guard_download "\$dest" "\$url"', text, re.M) or \
        "guard_download \"$dest\" \"$url\"" in text, f"{name}: fetch() never calls guard_download"
    # the repository slug must be overridable after a rename, in one place
    assert 'REPO_SLUG="${P2P_REPO_SLUG:-' in text, f"{name}: REPO_SLUG is not rename-safe"


# ── the URLs we tell people to copy/paste ─────────────────────────────────

def test_no_download_from_github_blob_pages():
    """/blob/main/x.sh is an HTML page — never hand that to curl/wget (the DOCTYPE error)."""
    owner = _canonical_slug().split("/")[0]
    blob_re = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)/blob/")
    offenders = []
    for name, text in _texts().items():
        in_fence = False
        for line_no, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if name.endswith(".sh") and stripped.startswith("#"):
                continue                       # explanatory comment, not a command
            if stripped.startswith("|"):
                continue                       # table row documenting the mistake
            match = blob_re.search(line)
            if match and match.group(1) == owner and in_fence:
                offenders.append(f"{name}:{line_no}: {stripped[:110]}")
    assert not offenders, "downloads must use raw.githubusercontent.com, not /blob/:\n" + "\n".join(offenders)


def test_no_stale_repo_slug():
    """Every own-repo URL must use the current slug — renames break raw URLs silently."""
    slug = _canonical_slug()
    url_re = re.compile(r"(?:raw\.githubusercontent\.com|codeload\.github\.com|github\.com)"
                        r"/([\w.-]+)/([\w.-]+)")
    offenders = []
    for name, text in _texts().items():
        for line_no, line in enumerate(text.splitlines(), 1):
            for owner, repo in url_re.findall(line):
                found = f"{owner}/{repo}".removesuffix(".git")
                owner_slug = _canonical_slug().split("/")[0]
                if owner == owner_slug and found != slug:
                    offenders.append(f"{name}:{line_no}: …/{found} (expected …/{slug})")
    # …and the default each installer falls back to must be current as well
    for name in SCRIPTS:
        text = (ROOT / name).read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            match = re.search(r'REPO_SLUG="\$\{P2P_REPO_SLUG:-([^}"]+)\}"', line)
            if match and match.group(1) != slug:
                offenders.append(f"{name}:{line_no}: default slug {match.group(1)!r}")
    assert not offenders, "stale repository slug (expected {0}):\n".format(slug) + "\n".join(offenders)


def test_raw_urls_point_at_files_that_exist():
    """No 404 page in the docs: every referenced path must exist in the repo."""
    checked = 0
    missing = []
    for name, text in _texts().items():
        for line_no, line in enumerate(text.splitlines(), 1):
            for owner, repo, _ref, path in RAW_URL_RE.findall(line):
                checked += 1
                if not (ROOT / path.rstrip(".,)).")).exists():
                    missing.append(f"{name}:{line_no}: {owner}/{repo} → {path}")
    assert checked >= 8, "README lost its install one-liners?"
    assert not missing, "raw URL points at a file that is not in the repo:\n" + "\n".join(missing)


def test_documented_installers_all_exist():
    """Each `install*.sh` mentioned in the README must be a real file."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for name in set(re.findall(r"\binstall[\w-]*\.sh\b", readme)):
        assert (ROOT / name).is_file(), f"README documents {name}, but it is not in the repo"


# ── functional check of the guard ─────────────────────────────────────────

# ── the uninstaller: ask before deleting data ────────────────────────────

def test_uninstaller_asks_full_erase_or_keep_data():
    """uninstall.sh must offer both: complete erase and data-saving uninstall."""
    text = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert "--full" in text, "uninstall.sh: missing --full (erase everything)"
    assert "--keep-data" in text, "uninstall.sh: missing --keep-data (save the json data)"
    for data_file in ("config.json", "data.json"):
        assert data_file in text, f"uninstall.sh: never mentions {data_file}"
    # the interactive menu offers erase-everything / keep-data / cancel
    assert re.search(r"Choose \[1/2/3\]", text), "uninstall.sh: the 1/2/3 ask is gone"


def test_uninstaller_defaults_to_keeping_data_without_a_terminal():
    """Headless runs (curl | bash with no tty) must never destroy the json data."""
    text = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert re.search(r"No terminal[^\n]*defaulting to KEEPING", text), \
        "uninstall.sh: headless default must keep the data"
    # everything install.sh did is reversed either way
    assert "systemctl stop" in text and "systemctl disable" in text
    assert "rm -rf" in text and "pkill" in text and "crontab" in text


def test_install_uninstall_delegates_to_the_uninstaller():
    """`install.sh --uninstall` must run uninstall.sh so it asks the same question."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "uninstall.sh" in text, "install.sh --uninstall no longer uses uninstall.sh"


GUARD_HARNESS = """
set -u
err()  { echo "ERR: $*" >&2; }
warn() { echo "WARN: $*"; }
ok()   { echo "OK: $*"; }
need_cmd() { command -v "$1" >/dev/null 2>&1; }
RAW_URL="https://raw.githubusercontent.com/owner/repo/main"
@GUARDS@
fail=0
check() {  # check FILE URL EXPECT(PASS|FAIL)
  if guard_download "$1" "$2"; then got=PASS; else got=FAIL; fi
  if [[ "$got" != "$3" ]]; then echo "expected $3 for $1, got $got"; fail=1; fi
}
check "@HTML@"   "@RAW@/install.sh" FAIL        # GitHub HTML viewer page
check "@PLAIN404@" "@RAW@/install.sh" FAIL      # renamed repository
check "@EMPTY@"  "@RAW@/bot.py"      FAIL       # truncated download
check "@GOOD@"   "@RAW@/bot.py"      PASS       # a real file
check "@TARBALL@" "https://codeload.github.com/o/r/tar.gz/refs/heads/main" PASS
check "@FAKE_TARBALL@" "https://codeload.github.com/o/r/tar.gz/refs/heads/main" FAIL
exit $fail
"""


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_guard_download_rejects_web_pages(tmp_path):
    """The helper that keeps an HTML page from ever reaching the install dir."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    start = text.index("# ── download integrity guards")
    end = text.index("# download_to URL DEST")
    guards = text[start:end]
    assert "guard_download()" in guards

    html = tmp_path / "install.sh"
    html.write_text("\n\n\n\n  \n<!DOCTYPE html>\n<html lang=\"en\">\n<body>404</body>\n</html>\n")
    plain404 = tmp_path / "404.txt"
    plain404.write_text("404: Not Found\n")
    empty = tmp_path / "empty.py"
    empty.write_text("")
    good = tmp_path / "bot.py"
    good.write_text("#!/usr/bin/env python3\nprint('hi')\n")
    real = tmp_path / "repo.tar.gz"
    real.write_bytes(__import__("gzip").compress(b"tar content"))
    fake = tmp_path / "bad.tar.gz"
    fake.write_bytes(html.read_bytes())
    for path in (real, fake):
        assert path.stat().st_size > 0

    harness = tmp_path / "harness.sh"
    harness.write_text(
        GUARD_HARNESS
        .replace("@GUARDS@", guards)
        .replace("@RAW@", "https://raw.githubusercontent.com/owner/repo/main")
        .replace("@HTML@", str(html))
        .replace("@PLAIN404@", str(plain404))
        .replace("@EMPTY@", str(empty))
        .replace("@GOOD@", str(good))
        .replace("@TARBALL@", str(real))
        .replace("@FAKE_TARBALL@", str(fake))
    )
    proc = subprocess.run(["bash", str(harness)], capture_output=True, text=True)
    assert proc.returncode == 0, f"guard_download misjudged a payload:\n{proc.stdout}\n{proc.stderr}"
    assert "web page, not a file" in proc.stderr, "guard must explain what went wrong"


# ── the Docker image gets every module the bot imports ────────────────────
def _dockerfile_copied_python_files() -> set[str]:
    """The ``.py`` files the Dockerfile COPYs (line continuations joined)."""
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8").replace("\\\n", " ")
    copied: set[str] = set()
    for line in text.splitlines():
        parts = line.strip().split()
        if parts and parts[0].upper() == "COPY":
            copied.update(part.rsplit("/", 1)[-1] for part in parts[1:]
                          if part.endswith(".py"))
    return copied


def _local_imports(name: str) -> set[str]:
    """Local modules one file imports — at module level or lazily in a function."""
    text = (ROOT / f"{name}.py").read_text(encoding="utf-8")
    names = {match.group(1) for match in
             re.finditer(r"(?m)^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)", text)}
    return {imported for imported in names if (ROOT / f"{imported}.py").exists()}


def _runtime_modules(entry: str = "bot.py") -> set[str]:
    """Everything the bot may import while running, following local imports."""
    seen, queue = set(), [entry]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        queue.extend(_local_imports(name[:-3]) if name.endswith(".py") else [])
    return seen


def test_the_docker_image_copies_every_module_the_bot_imports():
    """A missing COPY is a runtime ``ModuleNotFoundError`` inside the container.

    ``runtime_config`` / ``database_adapters`` / ``telegram_store`` are imported
    lazily — the bot starts without them and only fails when an admin opens
    /database or a database was selected in the environment, which is exactly
    the kind of breakage that ships unnoticed.
    """
    needed = {name[:-3] if name.endswith(".py") else name for name in _runtime_modules("bot.py")}
    copied = {name[:-3] if name.endswith(".py") else name
              for name in _dockerfile_copied_python_files()}
    missing = sorted(needed - copied)

    assert not missing, (
        "Dockerfile does not COPY " + ", ".join(missing) +
        " — the image then fails with \"No module named …\" at runtime")
