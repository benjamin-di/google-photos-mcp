"""Install a verified pip wheel and preserve patched, offline venv creation."""
import hashlib
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request

import ensurepip

VERSION = "26.2.2"
SHA256 = "fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"
URL = (
    "https://files.pythonhosted.org/packages/3f/06/"
    "3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/"
    "pip-26.2.2-py3-none-any.whl"
)
with urllib.request.urlopen(URL, timeout=90) as response:
    wheel = response.read()
assert hashlib.sha256(wheel).hexdigest() == SHA256, "pip wheel integrity mismatch"
source = pathlib.Path(ensurepip.__file__)
text = source.read_text()
old = f'_PIP_VERSION = "{ensurepip.version()}"'
assert text.count(old) == 1, "unsupported ensurepip layout"
with tempfile.TemporaryDirectory() as temporary:
    staged = pathlib.Path(temporary) / f"pip-{VERSION}-py3-none-any.whl"
    staged.write_bytes(wheel)
    subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-index", str(staged)], check=True)
    bundled = source.parent / "_bundled"
    shutil.copyfile(staged, bundled / staged.name)
    source.write_text(text.replace(old, f'_PIP_VERSION = "{VERSION}"'))
    for previous in bundled.glob("pip-*.whl"):
        if previous.name != staged.name:
            previous.unlink()
    venv = pathlib.Path(temporary) / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    subprocess.run([str(venv / "bin/python"), "-c", f"import pip; assert pip.__version__ == '{VERSION}'"], check=True)
print("Verified installer and offline venv seed:", VERSION)
