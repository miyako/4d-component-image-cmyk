#!/usr/bin/env python3
"""Build checksum-pinned static codec sources; never use installed codec libraries."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import time
import urllib.error
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


def cmake_path_option(name, path):
    return "-D" + name + "=" + path.as_posix()


def regular_path(path):
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("dependency paths must not traverse symlinks")


def tree_digest(directory):
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        regular_path(path)
        if path.is_file():
            digest.update(path.relative_to(directory).as_posix().encode() + b"\0"
                          + path.read_bytes() + b"\0")
    return digest.hexdigest()


def static_inventory(prefix):
    files = [path for path in prefix.rglob("*")
             if path.is_file() and (path.suffix in {".a", ".lib", ".h"}
                                    or "include" in path.relative_to(prefix).parts)]
    result = {}
    for path in sorted(files):
        regular_path(path.parent)
        if path.is_symlink() and not path.resolve().is_relative_to(prefix.resolve()):
            raise ValueError("installed dependency link escapes its prefix")
        link = str(path.readlink()).encode() + b"\0" if path.is_symlink() else b""
        result[path.relative_to(prefix).as_posix()] = hashlib.sha256(link + path.read_bytes()).hexdigest()
    return result


def download_archive(dependency, partial):
    transient = {429, 500, 502, 503, 504}
    for attempt in range(3):
        try:
            print("Downloading pinned source: " + dependency["name"], flush=True)
            with urllib.request.urlopen(dependency["url"], timeout=120) as response:
                if not response.geturl().startswith("https://"):
                    raise ValueError("dependency redirect must remain HTTPS")
                with partial.open("wb") as output:
                    shutil.copyfileobj(response, output)
            return
        except urllib.error.HTTPError as error:
            if error.code not in transient or attempt == 2:
                raise ValueError(f"{dependency['name']} source download failed: HTTP {error.code}") from error
            print(f"{dependency['name']} transient HTTP {error.code}; retrying pinned URL", flush=True)
            time.sleep(attempt + 1)


def notices(root, source, write=False):
    config = json.loads((root / "template.json").read_text())
    directory = root / "component" / config["repository"] / "Resources/licenses"
    records = []
    for dependency in json.loads((root / "cli/dependencies.json").read_text())["dependencies"]:
        names = [dependency["notice"]]
        if dependency["name"] == "jpeg":
            names += ["README.ijg"]
        for name in names:
            data = (source[dependency["name"]] / name).read_bytes()
            destination = directory / (dependency["name"] + "-" + dependency["version"]
                                       + "-" + name.replace(".", "-") + ".txt")
            if write:
                directory.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
            if not destination.is_file() or destination.read_bytes() != data:
                raise ValueError("pinned redistribution notice missing or changed: " + str(destination))
            records.append({"path": destination.name, "sha256": hashlib.sha256(data).hexdigest(),
                            "dependency": dependency["name"], "version": dependency["version"],
                            "source_sha256": dependency["sha256"], "license": dependency["license"]})
    manifest = directory / "provenance.json"
    data = (json.dumps({"schema": 1, "notices": records}, indent=2) + "\n").encode()
    if write:
        manifest.write_bytes(data)
    if not manifest.is_file() or manifest.read_bytes() != data:
        raise ValueError("redistribution provenance differs from pinned sources")


def sources(root):
    lock = json.loads((root / "cli/dependencies.json").read_text())
    cache = root / "build/dependency-sources"
    regular_path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    result = {}
    for dependency in lock["dependencies"]:
        archive = cache / (dependency["name"] + "-" + dependency["sha256"] + ".archive")
        regular_path(archive)
        if not archive.exists():
            partial = archive.with_suffix(".partial")
            regular_path(partial)
            try:
                download_archive(dependency, partial)
                partial.replace(archive)
            finally:
                partial.unlink(missing_ok=True)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != dependency["sha256"]:
            raise ValueError("upstream checksum mismatch: " + dependency["name"])
        directory = cache / dependency["sha256"]
        regular_path(directory)
        identity = cache / (dependency["sha256"] + ".tree-sha256")
        regular_path(identity)
        if not directory.exists():
            staging = cache / (dependency["sha256"] + ".extracting")
            regular_path(staging)
            staging.mkdir()
            try:
                with tarfile.open(archive) as source:
                    members = source.getmembers()
                    names = set()
                    total = 0
                    for member in members:
                        path = Path(member.name)
                        if (path.is_absolute() or ".." in path.parts
                                or "\\" in member.name or ":" in member.name
                                or member.name in names
                                or not (member.isfile() or member.isdir())):
                            raise ValueError("unsafe dependency archive entry: " + member.name)
                        names.add(member.name)
                        total += member.size
                        if total > 512 * 1024 * 1024:
                            raise ValueError("dependency source archive exceeds extraction limit")
                    source.extractall(staging, members=members)
                staging.rename(directory)
                identity.write_text(tree_digest(directory) + "\n")
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        actual = tree_digest(directory)
        if not identity.is_file():
            # Re-extract instead of blessing an older unverified cache.
            shutil.rmtree(directory)
            return sources(root)
        if identity.read_text().strip() != actual:
            raise ValueError("modified extracted dependency source: " + dependency["name"])
        children = list(directory.iterdir())
        if len(children) != 1 or not children[0].is_dir():
            raise ValueError("upstream archive must have one source root")
        result[dependency["name"]] = children[0]
    return result


def build(root, target):
    root = Path(root).resolve()
    source = sources(root)
    notices(root, source)
    base = root / "build/dependencies" / target
    regular_path(base)
    prefix = base / "installed"
    fingerprint = hashlib.sha256((root / "cli/dependencies.json").read_bytes()
                                 + Path(__file__).read_bytes()).hexdigest()
    marker = base / "build.json"
    regular_path(marker)
    if marker.is_file():
        previous = json.loads(marker.read_text())
        if previous.get("input_sha256") == fingerprint:
            inventory = static_inventory(prefix)
            if not inventory or previous.get("static_files") != inventory:
                raise ValueError("cached static dependencies were modified or removed")
            return prefix, source
    windows = target == "windows-x64"
    common = ["-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=OFF",
              cmake_path_option("CMAKE_INSTALL_PREFIX", prefix),
              cmake_path_option("CMAKE_PREFIX_PATH", prefix),
              "-DCMAKE_MSVC_RUNTIME_LIBRARY=MultiThreaded",
              "-DCMAKE_POLICY_DEFAULT_CMP0091=NEW",
              "-DCMAKE_POLICY_VERSION_MINIMUM=3.10"]
    common += ["-A", "x64"] if windows else [
        "-DCMAKE_OSX_ARCHITECTURES=" + ("arm64" if target == "macos-arm64" else "x86_64"),
        "-DCMAKE_OSX_DEPLOYMENT_TARGET=11.0"]
    options = {
        "zlib": ["-DZLIB_BUILD_EXAMPLES=OFF"],
        "jpeg": ["-DENABLE_SHARED=OFF", "-DENABLE_STATIC=ON", "-DWITH_TURBOJPEG=OFF",
                 "-DWITH_SIMD=OFF", "-DWITH_CRT_DLL=OFF"],
        "png": ["-DPNG_SHARED=OFF", "-DPNG_STATIC=ON", "-DPNG_FRAMEWORK=OFF",
                "-DPNG_TESTS=OFF", "-DPNG_TOOLS=OFF"],
        "tiff": ["-Dtiff-tools=OFF", "-Dtiff-tests=OFF", "-Dtiff-contrib=OFF",
                 "-Dtiff-docs=OFF", "-Dlzma=OFF", "-Dzstd=OFF", "-Dwebp=OFF",
                 "-Djpeg=OFF", "-Dold-jpeg=OFF", "-Djbig=OFF", "-Dlerc=OFF",
                 "-Dlibdeflate=OFF"],
    }
    for name in options:
        if name in ("png", "tiff"):
            options[name] += [
                cmake_path_option("ZLIB_LIBRARY", prefix / "lib" / ("zlibstatic.lib" if windows else "libz.a")),
                cmake_path_option("ZLIB_INCLUDE_DIR", prefix / "include")]
        directory = base / (name + "-" + fingerprint[:12])
        working_source = base / ("source-" + name + "-" + fingerprint[:12])
        if not working_source.exists():
            shutil.copytree(source[name], working_source)
        commands = [
            ["cmake", "-S", str(working_source), "-B", str(directory), *common, *options[name]],
            ["cmake", "--build", str(directory), "--config", "Release", "--parallel", "4"],
            ["cmake", "--install", str(directory), "--config", "Release"],
        ]
        for command in commands:
            subprocess.run(command, check=True, timeout=600, stdout=subprocess.DEVNULL)
    marker.write_text(json.dumps({"input_sha256": fingerprint, "target": target,
                                  "static_files": static_inventory(prefix),
                                  "dependencies": json.loads((root / "cli/dependencies.json").read_text())},
                                 indent=2) + "\n")
    return prefix, source


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--target", required=True, choices=["macos-arm64", "macos-x64", "windows-x64"])
    parser.add_argument("--write-notices", action="store_true",
                        help="Generate notices from verified upstream archives before building")
    args = parser.parse_args()
    if args.write_notices:
        notices(args.root.resolve(), sources(args.root.resolve()), write=True)
    build(args.root, args.target)
