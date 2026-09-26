"""Read a Discord data package from a folder OR a .zip — same API for both.

A "package" is the thing inside request_data.zip: Messages/, Account/, etc.
Discord sometimes zips it with a top-level wrapper folder; we detect that.
"""
import json, os, zipfile


class DirPkg:
    def __init__(self, root):
        self.kind = "dir"
        self.path = os.path.abspath(root)
        self._z = None

    def exists(self, rel):
        return os.path.exists(os.path.join(self.path, *rel.split("/")))

    def isdir(self, rel):
        return os.path.isdir(os.path.join(self.path, *rel.split("/")))

    def listdir(self, rel):
        p = os.path.join(self.path, *rel.split("/"))
        if not os.path.isdir(p):
            return []
        return sorted(os.listdir(p), key=str.lower)

    def subdirs(self, rel):
        return [n for n in self.listdir(rel) if self.isdir(rel + "/" + n)]

    def read(self, rel):
        with open(os.path.join(self.path, *rel.split("/")), "rb") as f:
            return f.read()

    def read_json(self, rel, default=None):
        if not self.exists(rel):
            return default
        return json.loads(self.read(rel).decode("utf-8"))


class ZipPkg:
    def __init__(self, path):
        self.kind = "zip"
        self.path = os.path.abspath(path)
        self._z = zipfile.ZipFile(self.path)
        self.names = [i.filename for i in self._z.infolist() if not i.is_dir()]
        # unwrap single top-level wrapper folder (some exports zip the folder itself)
        tops = {n.split("/")[0] for n in self.names if "/" in n}
        if len(tops) == 1 and all("/" in n for n in self.names):
            self.prefix = next(iter(tops)) + "/"
        else:
            self.prefix = ""

    def _nm(self, rel):
        return self.prefix + rel

    def exists(self, rel):
        n = self._nm(rel)
        return n in self.names or any(x.startswith(n + "/") for x in self.names)

    def isdir(self, rel):
        n = self._nm(rel)
        return any(x.startswith(n + "/") for x in self.names)

    def listdir(self, rel):
        n = self._nm(rel)
        pre = "" if rel in ("", ".") else n + "/"
        out = set()
        for x in self.names:
            if pre and not x.startswith(pre):
                continue
            rest = x[len(pre):]
            if "/" in rest:
                out.add(rest.split("/")[0])
            elif rest:
                out.add(rest)
        return sorted(out, key=str.lower)

    def subdirs(self, rel):
        return [d for d in self.listdir(rel) if self.isdir(rel + "/" + d)]

    def read(self, rel):
        return self._z.read(self._nm(rel))

    def read_json(self, rel, default=None):
        if not self.exists(rel):
            return default
        return json.loads(self.read(rel).decode("utf-8"))


def open_pkg_at(path):
    """open_pkg + unwrap a single 'package/' wrapper if present (folder or zip)."""
    pkg = open_pkg(path)
    if pkg is not None and not pkg.isdir("Messages") and pkg.isdir("package"):
        if pkg.kind == "zip":
            pkg.prefix += "package/"
        else:
            pkg.path = os.path.join(pkg.path, "package")
    return pkg


def open_pkg(path):
    """path may be a folder, a .zip, or an empty string (returns None if unusable)."""
    if not path:
        return None
    path = os.path.abspath(path)
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        if zipfile.is_zipfile(path):
            return ZipPkg(path)
        raise ValueError("that .zip could not be opened")
    if os.path.isdir(path):
        return DirPkg(path)
    raise ValueError("not a folder or .zip file")
